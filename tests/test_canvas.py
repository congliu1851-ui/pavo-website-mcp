import copy
import json

import pytest

from pavo_website_mcp.canvas import Pavo, decode_data
from pavo_website_mcp.client import APIError
from pavo_website_mcp.store import Store


def node(key="v-abcdefghijkl", kind="video", data=None):
    return {"node_key": key, "node_type": kind, "name": "原节点", "position": {"position_x": "10", "position_y": "20"},
            "measured": {"width": "280", "height": "280"}, "data": json.dumps(data or {"params": {}}, ensure_ascii=False)}


MODELS = {"items": [{"model_code": "live-video", "allowed": True, "is_online": True, "constraints": {
    "aspect_ratios": ["9:16"], "resolutions": ["720p"], "mode_types": ["frames_to_video"],
    "supported_duration_seconds": [5, 10], "supports_audio_generation": False}}]}

SPECS = {"version": "live-1", "nodeConfigs": {"guide": {"video": [{"code": "live-first-last", "name": "首尾帧", "node_type": "video", "extra": {
    "node_list": [
        {"node_key": "start-real", "action_type": "input", "node_type": "image", "name": "首帧", "data": {}},
        {"node_key": "end-real", "action_type": "input", "node_type": "image", "name": "尾帧", "data": {}},
        {"node_key": "self_node", "node_type": "video", "data": {"params": {"skill": "actual-skill", "prompt": "模板提示"}}}],
    "connection_list": [{"source": "start-real", "target": "self_node", "role": "reference"}, {"source": "end-real", "target": "self_node", "role": "reference"}]
}}]}}}


class FakeClient:
    def __init__(self, nodes=()):
        self.detail = {"version": "7", "node_list": list(nodes), "connection_list": [], "current_canvas": {"canvas_uuid": "c", "project_id": 12}}
        self.batches = []
        self.conflict = None
    def request(self, method, path, payload=None, params=None, **kw):
        if path.endswith("/detail"):
            return copy.deepcopy(self.detail)
        if path.endswith("model_options"):
            return copy.deepcopy(MODELS)
        if path.endswith("tool-specs"):
            return copy.deepcopy(SPECS)
        if path.endswith("nodes/batch"):
            self.batches.append(payload)
            if len(self.batches) == 1 and self.conflict:
                self.detail["version"] = 8
                self.conflict(self.detail)
                raise APIError("版本冲突", status=409, definite=True)
            return {"version": 9}
        raise AssertionError((method, path))


def test_prompt_update_preserves_nested_and_unknown(tmp_path):
    old = {"unknown": [1], "params": {"count": 2, "prompt": [{"type": "text", "content": "旧"}, {"type": "skill", "code": "s"}], "settings": {"ratio": "9:16", "custom": 1}}}
    client = FakeClient([node(data=old)])
    p = Pavo(client, Store(tmp_path))
    result = p.update_node("p", "c", "v-abcdefghijkl", "edit1", prompt="雪落，小仙子抬头", params={"settings": {"resolution": "720p"}})
    data = decode_data(client.batches[0]["nodes"]["update"][0]["data"])
    assert data["unknown"] == [1]
    assert data["params"]["settings"] == {"ratio": "9:16", "custom": 1, "resolution": "720p"}
    assert data["params"]["prompt"] == [{"type": "text", "content": "雪落，小仙子抬头"}, {"type": "skill", "code": "s"}]
    assert client.batches[0]["version"] == 7
    assert result["version"] == 9


def test_model_constraints_refuse_user_incompatible_duration(tmp_path):
    p = Pavo(FakeClient(), Store(tmp_path))
    with pytest.raises(ValueError, match="duration"):
        p.create_node("p", "c", "video", "bad", model="live-video", params={"duration": 3})


def test_template_binds_exact_live_keys_and_preserves_order(tmp_path):
    c = FakeClient([node("i-first0000000", "upload", {"mediaType": "image", "url": ["https://cdn.example/first.png"]}),
                    node("i-last00000000", "upload", {"mediaType": "image", "url": ["https://cdn.example/last.png"]})])
    p = Pavo(c, Store(tmp_path))
    result = p.apply_template("p", "c", "live-first-last", {"start-real": "i-first0000000", "end-real": "i-last00000000"}, "template1", model="live-video", prompt="首尾过渡")
    batch = c.batches[0]
    assert [e["source"] for e in batch["connections"]["create"]] == ["i-first0000000", "i-last00000000"]
    assert [e["media_order"] for e in batch["connections"]["create"]] == [0, 1]
    assert len(batch["nodes"]["create"]) == 1
    assert result["run_node_key"]
    data = decode_data(batch["nodes"]["create"][0]["data"])
    assert {"type": "skill", "code": "actual-skill"} in data["params"]["prompt"]
    with pytest.raises(ValueError, match="输入"):
        p.apply_template("p", "c", "live-first-last", {"first": "i-first0000000"}, "wrong", model="live-video")


def test_dry_run_no_mutation_and_key_stable(tmp_path):
    c = FakeClient()
    p = Pavo(c, Store(tmp_path))
    args = ("p", "c", [{"op": "create", "type": "text", "prompt": "中文"}], "new1")
    preview = p.apply_canvas(*args, dry_run=True)
    assert not c.batches
    result = p.apply_canvas(*args)
    assert result["node_keys"] == preview["node_keys"]
    assert p.apply_canvas(*args) == result
    assert len(c.batches) == 1


def test_version_conflict_rebase_unrelated_but_refuse_same_field(tmp_path):
    c = FakeClient([node(data={"params": {"prompt": []}, "custom": 1})])
    c.conflict = lambda d: d["node_list"][0].update(data=json.dumps({"params": {"prompt": []}, "custom": 2}))
    Pavo(c, Store(tmp_path / "ok")).update_node("p", "c", "v-abcdefghijkl", "u", prompt="新提示")
    assert len(c.batches) == 2
    assert decode_data(c.batches[1]["nodes"]["update"][0]["data"])["custom"] == 2
    c = FakeClient([node()])
    c.conflict = lambda d: d["node_list"][0].update(data=json.dumps({"params": {"prompt": [{"type": "text", "content": "别人正在编辑"}]}}))
    with pytest.raises(ValueError, match="冲突"):
        Pavo(c, Store(tmp_path / "bad")).update_node("p", "c", "v-abcdefghijkl", "u", prompt="新提示")
    assert len(c.batches) == 1


def test_connections_reject_cycles_and_fake_first_last(tmp_path):
    c = FakeClient([node("i-first0000000", "image"), node()])
    p = Pavo(c, Store(tmp_path))
    with pytest.raises(ValueError, match="模板"):
        p.connect_references("p", "c", "v-abcdefghijkl", [{"source": "i-first0000000", "role": "first_frame"}], "r1")
    c.detail["connection_list"] = [{"source_node_key": "v-abcdefghijkl", "target_node_key": "i-first0000000"}]
    with pytest.raises(ValueError, match="循环"):
        p.connect_references("p", "c", "v-abcdefghijkl", [{"source": "i-first0000000"}], "r2")


def test_live_template_empty_connection_list_uses_explicit_input_order(tmp_path, monkeypatch):
    specs = copy.deepcopy(SPECS)
    template = specs["nodeConfigs"]["guide"]["video"][0]
    template["extra"]["connection_list"] = []
    template["extra"]["node_list"][-1]["node_key"] = "self_node"
    template["extra"]["node_list"][-1]["data"] = {"params": {"mode": "frames_to_video", "model": "live-video"}, "settings": {"duration": "", "ratio": "9:16"}}
    c = FakeClient([node("i-first0000000", "upload", {"mediaType": "image"}), node("i-last00000000", "upload", {"mediaType": "image"})])
    request = c.request
    c.request = lambda method, path, payload=None, params=None, **kw: copy.deepcopy(specs) if path.endswith("tool-specs") else request(method, path, payload, params, **kw)
    p = Pavo(c, Store(tmp_path))
    result = p.apply_template("p", "c", "live-first-last", {"start-real": "i-first0000000", "end-real": "i-last00000000"}, "live-empty", model="live-video")
    edges = c.batches[0]["connections"]["create"]
    assert [e["source"] for e in edges] == ["i-first0000000", "i-last00000000"]
    assert [e["media_order"] for e in edges] == [0, 1]
    assert all(e["target"] == result["run_node_key"] for e in edges)
    assert "duration" not in decode_data(c.batches[0]["nodes"]["create"][0]["data"])["params"]["settings"]
