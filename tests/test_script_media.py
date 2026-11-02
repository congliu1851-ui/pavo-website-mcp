import asyncio
import copy
import json
import os
from pathlib import Path
import sys

import pytest
from mcp import Client as MCPClient, StdioServerParameters

from pavo_website_mcp.script import Script, fingerprint
from pavo_website_mcp.script_media import ScriptMedia
from pavo_website_mcp.script_storyboard import ScriptStoryboard
from pavo_website_mcp.client import APIError
from pavo_website_mcp.store import Store, OperationUnknown
from test_script import ScriptClient


class MediaClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.project.update(status="asset_ready", ratio="9:16")
        self.production_id = "200"
        self.enterable = False
        self.selectable = True
        self.board = {"episode_id": "200", "active_tasks": [], "segments": [{"segment_id": "400", "title": "保留段名", "summary": "保留概要",
            "selected_keyframe_id": "10", "selected_keyframe_url": "https://cdn.example/old.png", "selected_video_id": None, "selected_video_url": "",
            "estimated_duration_ms": 9000, "video_prompt_text": "【0s-4s】抬头。【4s-9s】低声说话。", "scenes": [
                {"scene_id": "500", "content_rich": "原场景", "shots": [{"shot_id": "600", "content_rich": "抬头。", "duration_sec": 4},
                {"shot_id": "601", "content_rich": "低声说话。", "duration_sec": 5}]}]}]}
        self.histories = {"segment_keyframe": {"items": [{"media_id": "10", "url": "https://cdn.example/old.png", "media_type": "image", "role": "segment_keyframe"}]}, "segment_video": {"items": []}}
        self.models = {"items": [{"model_code": "video1", "allowed": True, "is_online": True, "constraints": {
            "aspect_ratios": ["9:16"], "resolutions": ["hd"], "mode_types": ["first_frame"], "supported_duration_seconds": list(range(4, 13))}}]}
        self.no_upload = False
        self.no_select = False
        self.corrupt_text = False
        self.corrupt_other = False
        self.no_new_task = False
        self.reject = False
        self.next_task = 300
        self.no_delete = False
        self.backup_root = None
        self.backup_before_delete = False

    def request(self, method, path, payload=None, params=None, **kw):
        if path.endswith("/episodes/storyboard"):
            self.calls.append((method, path, copy.deepcopy(payload), params))
            return {"items": [{"episode_id": self.production_id, "episode_no": 1, "script_status": "completed",
                              "storyboard_phase": "pending", "shot_count": 0, "enterable": self.enterable,
                              "batch_generate_selectable": self.selectable}]}
        if path.endswith("/storyboard/detail"):
            if not self.enterable:
                raise AssertionError("首次pending分镜不能读取详情")
            self.calls.append((method, path, copy.deepcopy(payload), params))
            return copy.deepcopy(self.board)
        if path.endswith("/model_options"):
            self.calls.append((method, path, copy.deepcopy(payload), params))
            return copy.deepcopy(self.models)
        if path.endswith("/media") and method == "GET":
            self.calls.append((method, path, copy.deepcopy(payload), params))
            return copy.deepcopy(self.histories[params["role"]])
        if method == "DELETE" and "/segments/" in path:
            self.calls.append((method, path, copy.deepcopy(payload), params))
            if self.timeout:
                raise TimeoutError("删除结果未知")
            self.backup_before_delete = bool(self.backup_root and list(Path(self.backup_root).glob("storyboard-before-delete-*.json")))
            if not self.no_delete:
                self.board["segments"] = [s for s in self.board["segments"] if s["segment_id"] != path.rsplit("/", 1)[1]]
                for i, s in enumerate(self.board["segments"], 1):
                    s["display_order"] = i
            if self.corrupt_text:
                self.board["segments"][0]["scenes"][0]["shots"][0]["content_rich"] = "删除时不该改的正文"
            return {}
        if any(x in path for x in ["/media/upload", "/select", "/video/generate"]):
            self.calls.append((method, path, copy.deepcopy(payload), params))
            if self.timeout:
                raise TimeoutError("请求结果未知")
            if self.reject:
                raise APIError("明确拒绝", definite=True)
            if path.endswith("/media/upload"):
                if not self.no_upload:
                    self.histories[payload["role"]]["items"].append({"media_id": "20", **payload})
                return {"media_id": "20"}
            if path.endswith("/select"):
                mid = path.split("/")[-2]
                role, item = next((r, x) for r, h in self.histories.items() for x in h["items"] if x.get("media_id") == mid)
                prefix = "selected_keyframe_" if role == "segment_keyframe" else "selected_video_"
                if not self.no_select:
                    self.board["segments"][0].update({prefix + "id": mid, prefix + "url": item["url"]})
                if self.corrupt_text:
                    self.board["segments"][0]["scenes"][0]["shots"][0]["content_rich"] = "被意外改写"
                if self.corrupt_other:
                    self.board["segments"][0]["selected_video_url"] = "被意外替换"
                return {}
            tid = str(self.next_task)
            self.next_task += 1
            if not self.no_new_task:
                self.histories["segment_video"]["items"].append({"generation_task_id": tid, "task_status": "running", "url": "", "media_type": "video"})
            return {"task_id": tid, "status": "accepted"}
        return super().request(method, path, payload, params, **kw)


@pytest.fixture
def media(tmp_path, monkeypatch):
    monkeypatch.setattr("pavo_website_mcp.script_media.safe_url", lambda url: None)
    return ScriptMedia(Script(MediaClient(), Store(tmp_path)))


def test_first_pending_generation_does_not_fetch_forbidden_detail(media):
    r = media.script.run_stage("100", "generate_storyboard", "first", "200")
    assert r["acknowledged"]
    assert not any(c[1].endswith("/storyboard/detail") for c in media.client.calls)
    assert [c[1] for c in media.client.calls if c[0] == "POST"] == ["/api/v1/pixa/short-drama/episodes/200/storyboard/generate"]


def test_native_production_id_is_not_guessed_from_text_title(media):
    media.client.production_id = "900"
    with pytest.raises(ValueError, match="制作分集"):
        media.script.run_stage("100", "generate_storyboard", "wrong", "200")
    media.script.run_stage("100", "generate_storyboard", "right", "900")
    assert [c[1] for c in media.client.calls if c[0] == "POST"] == ["/api/v1/pixa/short-drama/episodes/900/storyboard/generate"]


def test_pending_read_is_explicit_and_does_not_probe_detail(media):
    with pytest.raises(ValueError, match="尚不可进入"):
        media.script.get_storyboard("100", "200")
    assert not any(c[1].endswith("/storyboard/detail") for c in media.client.calls)
    assert media.list_episodes("100")["data"]["items"][0]["enterable"] is False


def test_nonselectable_production_episode_blocks_generation(media):
    media.client.selectable = False
    with pytest.raises(ValueError, match="未允许"):
        media.script.run_stage("100", "generate_storyboard", "disabled", "200")
    assert not any(c[0] == "POST" for c in media.client.calls)


def test_export_pending_uses_unique_episode_number_and_records_both_ids(media, tmp_path):
    media.client.project["episodes"] = media.client.project["episodes"][:1]
    media.client.production_id = "900"
    result = media.script.export("100", str(tmp_path / "export"), "json", True)
    data = json.loads(Path(result["path"]).read_text())
    assert data["storyboard_episode_map"]["200"] == {"text_episode_id": "200", "storyboard_episode_id": "900", "episode_no": 1, "matched_by": "unique_episode_no"}
    assert data["storyboards"]["900"]["detail_fetched"] is False
    assert "segments" not in data["storyboards"]["900"]
    assert not any(c[1].endswith("/storyboard/detail") for c in media.client.calls)


def test_existing_storyboard_still_requires_regenerate(media):
    media.client.enterable = True
    with pytest.raises(ValueError, match="已有分镜"):
        media.script.run_stage("100", "generate_storyboard", "existing-board", "200")
    assert not writes(media)


def setup_ready(media):
    media.client.enterable = True
    return media.get("100", "200", "400")["_mcp_fingerprints"]["media"]


def writes(media):
    return [c for c in media.client.calls if c[0] != "GET"]


def attach(media, op="attach", digest=None, **kwargs):
    return media.attach("100", "200", "400", "image", "https://cdn.example/new.png", digest or setup_ready(media), op, **kwargs)


def video(media, op="video", digest=None, **kwargs):
    args = {"model_code": "video1", "resolution": "hd", "expected_ratio": "9:16", "style": {"style_id": "style1"},
            "expected_sha256": digest or setup_ready(media), "operation_id": op, **kwargs}
    return media.submit_video("100", "200", "400", **args)


def test_native_attach_select_and_replay_preserve_other_media(media):
    digest = setup_ready(media)
    original = copy.deepcopy(media.client.board)
    r = attach(media, digest=digest)
    assert r["verified"] and r["segment"]["selected_keyframe_id"] == "20"
    assert r["segment"]["selected_video_url"] == original["segments"][0]["selected_video_url"]
    assert [x[1] for x in writes(media)] == ["/api/v1/pixa/short-drama/segments/400/media/upload", "/api/v1/pixa/short-drama/segments/400/media/20/select"]
    assert writes(media)[0][2] == {"media_type": "image", "role": "segment_keyframe", "url": "https://cdn.example/new.png", "thumbnail_url": "https://cdn.example/new.png"}
    assert attach(media, digest=digest) == r
    assert len(writes(media)) == 2


def test_attach_known_url_reuses_history_without_duplicate_upload(media):
    digest = setup_ready(media)
    media.attach("100", "200", "400", "image", "https://cdn.example/old.png", digest, "existing")
    assert len(writes(media)) == 1 and writes(media)[0][0] == "PUT"


def test_native_video_upload_requires_explicit_frame_and_preserves_keyframe(media):
    digest = setup_ready(media)
    with pytest.raises(ValueError, match="HTTPS"):
        media.attach("100", "200", "400", "video", "https://cdn.example/clip.mp4", digest, "missing")
    r = media.attach("100", "200", "400", "video", "https://cdn.example/clip.mp4", digest, "clip", first_frame_url="https://cdn.example/frame.png")
    assert r["segment"]["selected_keyframe_id"] == "10"
    assert writes(media)[0][2]["role"] == "segment_video"
    assert writes(media)[0][2]["first_frame_url"] == "https://cdn.example/frame.png"


@pytest.mark.parametrize("url", ["http://cdn.example/new.png", "file:///tmp/x.png", ""])
def test_attach_rejects_non_https_before_write(media, url):
    with pytest.raises(ValueError):
        media.attach("100", "200", "400", "image", url, setup_ready(media), "bad")
    assert not writes(media)


def test_wrong_episode_segment_or_media_owner_does_not_write(media):
    digest = setup_ready(media)
    for episode, segment in [("999", "400"), ("200", "999")]:
        with pytest.raises(ValueError):
            media.get("100", episode, segment)
    with pytest.raises(ValueError, match="media_id"):
        media.select("100", "200", "400", "999", digest, "foreign")
    assert not writes(media)


def test_stale_fingerprint_and_readonly_block_write(media):
    digest = setup_ready(media)
    media.client.board["segments"][0]["title"] = "别人改过"
    with pytest.raises(ValueError, match="改变"):
        attach(media, digest=digest)
    media.client.project["readonly"] = True
    with pytest.raises(ValueError, match="只读"):
        attach(media, op="readonly")
    assert not writes(media)


@pytest.mark.parametrize("flag", ["no_upload", "no_select", "corrupt_text", "corrupt_other", "timeout"])
def test_unknown_partial_or_mutated_result_blocks_new_operation_id(media, flag):
    setup_ready(media)
    setattr(media.client, flag, True)
    with pytest.raises(OperationUnknown):
        attach(media, op="unknown1")
    count = len(writes(media))
    setattr(media.client, flag, False)
    with pytest.raises(OperationUnknown):
        attach(media, op="unknown2")
    assert len(writes(media)) == count
    assert media.store.get_operation("unknown1")["status"] == "UNKNOWN"


def test_definite_rejection_does_not_poison_new_corrected_request(media):
    setup_ready(media)
    media.client.reject = True
    with pytest.raises(APIError):
        attach(media, op="rejected")
    assert media.store.get_operation("rejected")["status"] == "FAILED"
    media.client.reject = False
    assert attach(media, op="corrected")["verified"]


def test_select_known_media_exact_native_path(media):
    r = media.select("100", "200", "400", "10", setup_ready(media), "select")
    assert r["verified"]
    assert writes(media)[0][:3] == ("PUT", "/api/v1/pixa/short-drama/segments/400/media/10/select", None)


def test_video_submission_uses_native_payload_and_real_history_task(media):
    digest = setup_ready(media)
    r = video(media, digest=digest)
    assert r["task_id"] == "300" and r["duration_sec"] == 9 and r["generation_submitted"]
    post = writes(media)[0]
    assert post[1] == "/api/v1/pixa/short-drama/segments/400/video/generate"
    assert post[2] == {"model_code": "video1", "resolution": "hd", "style_id": "style1", "keyframe_production_mode": "single_frame"}
    assert not any("canvas" in c[1] for c in media.client.calls)
    assert video(media, digest=digest) == r and len(writes(media)) == 1


def test_video_reference_numbers_can_reorder_without_changing_meaning(media, monkeypatch):
    before = '参考图1为[场景A]的环境参考。\n参考图2为[角色A]的角色四视图。\n\n【剧情】\n【0 s - 9 s】@图片2看向@图片1，说：“原台词。”'
    after = '参考图1为[角色A]的角色四视图。\n参考图2为[场景A]的环境参考。\n\n【剧情】\n【0 s - 9 s】@图片1看向@图片2，说：“原台词。”'
    media.client.board['segments'][0]['video_prompt_text'] = before
    request = media.client.request
    def reordered(method, path, *args, **kwargs):
        result = request(method, path, *args, **kwargs)
        if method == 'POST' and path.endswith('/video/generate'):
            media.client.board['segments'][0]['video_prompt_text'] = after
        return result
    monkeypatch.setattr(media.client, 'request', reordered)
    result = video(media, op='reference-reorder')
    assert result['task_id'] == '300'
    assert media.store.get_operation('reference-reorder')['status'] == 'SUCCESS'
    assert len(writes(media)) == 1


@pytest.mark.parametrize('after', [
    '参考图1为[角色A]的角色四视图。\n参考图2为[场景A]的环境参考。\n\n【剧情】\n【0 s - 9 s】@图片2看向@图片1，说：“原台词。”',
    '参考图1为[场景A]的环境参考。\n参考图2为[角色A]的角色四视图。\n\n【剧情】\n【0 s - 9 s】@图片2看向@图片1，说：“被改的台词。”',
    '参考图1为[场景A]的环境参考。\n参考图2为[角色A]的角色四视图。\n\n【剧情】\n【0 s - 8 s】@图片2看向@图片1，说：“原台词。”',
])
def test_reordered_prompt_still_protects_references_dialogue_and_cuts(media, monkeypatch, after):
    media.client.board['segments'][0]['video_prompt_text'] = '参考图1为[场景A]的环境参考。\n参考图2为[角色A]的角色四视图。\n\n【剧情】\n【0 s - 9 s】@图片2看向@图片1，说：“原台词。”'
    request = media.client.request
    def corrupted(method, path, *args, **kwargs):
        result = request(method, path, *args, **kwargs)
        if method == 'POST' and path.endswith('/video/generate'):
            media.client.board['segments'][0]['video_prompt_text'] = after
        return result
    monkeypatch.setattr(media.client, 'request', corrupted)
    with pytest.raises(OperationUnknown):
        video(media, op='corrupt-derived-prompt')
    assert media.store.get_operation('corrupt-derived-prompt')['status'] == 'UNKNOWN'
    assert len(writes(media)) == 1


@pytest.mark.parametrize("kwargs", [{"model_code": ""}, {"model_code": "invented"}, {"resolution": "uhd"}, {"expected_ratio": "16:9"}, {"style": {"style_id": "invented"}}])
def test_explicit_model_ratio_resolution_style_validation(media, kwargs):
    with pytest.raises(ValueError):
        video(media, **kwargs)
    assert not writes(media)


def test_duration_model_and_keyframe_must_be_verified(media):
    setup_ready(media)
    media.client.models["items"][0]["constraints"]["supported_duration_seconds"] = [4]
    with pytest.raises(ValueError, match="总时长"):
        video(media)
    media.client.models["items"][0]["constraints"]["supported_duration_seconds"] = [9]
    media.client.board["segments"][0]["selected_keyframe_id"] = "wrong"
    with pytest.raises(ValueError, match="关键帧"):
        video(media, op="missing-keyframe")
    assert not writes(media)


def test_video_missing_new_task_stays_unknown_and_cannot_repost(media):
    setup_ready(media)
    media.client.no_new_task = True
    with pytest.raises(OperationUnknown):
        video(media, op="missing-task")
    with pytest.raises(OperationUnknown):
        video(media, op="new-id")
    assert len(writes(media)) == 1


@pytest.mark.parametrize("status,terminal,succeeded,retry", [("running", False, None, False), ("waiting_2", False, None, False), ("success", True, True, False), ("failed", True, False, True), ("unknown", False, None, False)])
def test_exact_task_native_status_not_canvas_or_old_url(media, status, terminal, succeeded, retry):
    setup_ready(media)
    media.client.histories["segment_video"]["items"] = [{"generation_task_id": "300", "task_status": status, "url": "https://cdn.example/result.mp4"}]
    r = media.get_video_task("100", "200", "400", "300")
    assert (r["terminal"], r["succeeded"], r["retry_safe"]) == (terminal, succeeded, retry)
    absent = media.get_video_task("100", "200", "400", "999")
    assert absent["found"] is False and absent["succeeded"] is None and not absent["retry_safe"]
    assert not writes(media) and not any("canvas" in c[1] for c in media.client.calls)


def test_persisted_native_generated_video_is_complete_without_task_status(media):
    setup_ready(media)
    media.client.histories['segment_video']['items'] = [{'kind': 'media', 'media_id': '20', 'media_type': 'video', 'role': 'segment_video', 'generation_task_id': '300', 'source': 'ai_generate', 'url': 'https://cdn.example/result.mp4', 'first_frame_url': 'https://cdn.example/first.jpg'}]
    result = media.get_video_task('100', '200', '400', '300')
    assert result['native_status'] is None
    assert result['terminal'] and result['succeeded'] is True and not result['retry_safe']
    assert result['completion_evidence'] == 'persisted_native_generated_video'
    assert not writes(media)


def test_regenerate_can_follow_a_completed_native_media_resource(media):
    setup_ready(media)
    media.client.histories['segment_video']['items'] = [{'kind': 'media', 'media_id': '20', 'media_type': 'video', 'role': 'segment_video', 'generation_task_id': '299', 'source': 'ai_generate', 'url': 'https://cdn.example/old.mp4'}]
    result = video(media, op='after-native-resource', regenerate=True)
    assert result['task_id'] == '300' and len(writes(media)) == 1


@pytest.mark.parametrize('patch', [{'kind': 'task'}, {'media_id': ''}, {'source': 'upload'}, {'role': 'other'}, {'task_status': 'failed'}])
def test_url_or_unrelated_resource_does_not_fake_native_completion(media, patch):
    setup_ready(media)
    item = {'kind': 'media', 'media_id': '20', 'media_type': 'video', 'role': 'segment_video', 'generation_task_id': '300', 'source': 'ai_generate', 'url': 'https://cdn.example/result.mp4', **patch}
    media.client.histories['segment_video']['items'] = [item]
    result = media.get_video_task('100', '200', '400', '300')
    assert result['succeeded'] is not True


def test_running_or_unknown_previous_video_cannot_be_retried(media):
    setup_ready(media)
    for status in ["running", None, "unknown"]:
        media.client.histories["segment_video"]["items"] = [{"generation_task_id": "old", "task_status": status, "url": ""}]
        with pytest.raises(ValueError):
            video(media, op="retry-" + str(status), regenerate=True)
    assert not writes(media)


def test_failed_native_task_can_only_be_retried_explicitly(media):
    video(media, op="first-video")
    media.client.histories["segment_video"]["items"][0]["task_status"] = "failed"
    with pytest.raises(ValueError, match="regenerate"):
        video(media, op="unconfirmed-retry")
    r = video(media, op="confirmed-retry", regenerate=True)
    assert r["task_id"] == "301" and len(writes(media)) == 2


def test_text_only_model_cannot_ignore_keyframe_input(media):
    media.client.models["items"][0]["constraints"]["mode_types"] = ["text_to_video"]
    with pytest.raises(ValueError, match="关键帧图片输入"):
        video(media)
    assert not writes(media)


def test_stdio_exposes_explicit_media_schema_without_real_auth(tmp_path):
    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-m", "pavo_website_mcp.server"], env={**os.environ, "PAVO_STATE_DIR": str(tmp_path / "isolated-state")})
        async with MCPClient(params) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            names = {"pavo_script_list_storyboard_episodes", "pavo_script_list_media_models", "pavo_script_get_segment_media",
                     "pavo_script_attach_segment_media", "pavo_script_select_segment_media", "pavo_script_submit_segment_video", "pavo_script_get_segment_video_task", "pavo_script_delete_storyboard_segment"}
            assert names <= tools.keys()
            assert tools["pavo_script_get_segment_video_task"].annotations.read_only_hint
            required = tools["pavo_script_submit_segment_video"].input_schema["required"]
            assert {"model_code", "resolution", "expected_ratio", "style", "operation_id", "expected_sha256"} <= set(required)
            assert not tools["pavo_script_attach_segment_media"].annotations.read_only_hint
            assert tools["pavo_script_delete_storyboard_segment"].annotations.destructive_hint
            assert {"replacement_segment_ids", "backup_dir"} <= set(tools["pavo_script_delete_storyboard_segment"].input_schema["required"])
            invalid = await client.call_tool("pavo_script_list_media_models", {"scene_code": "canvas_video"})
            assert invalid.is_error
            assert "segment" in json.dumps(invalid.model_dump(), ensure_ascii=False)
    asyncio.run(exercise())


def migrated_board(media, tmp_path):
    media.client.enterable = True
    target = media.client.board["segments"][0]
    target.update(selected_keyframe_id=None, selected_keyframe_url="", display_order=1)
    replacement = copy.deepcopy(target)
    replacement.update(segment_id="401", title="已核替代片段", display_order=2)
    replacement["scenes"][0]["scene_id"] = "501"
    for i, shot in enumerate(replacement["scenes"][0]["shots"], 602):
        shot["shot_id"] = str(i)
        shot["display_order"] = i - 601
    media.client.board["segments"].append(replacement)
    media.client.histories = {"segment_keyframe": {"items": []}, "segment_video": {"items": []}}
    media.client.backup_root = tmp_path / "backup"
    return ScriptStoryboard(media.script)


def delete_migrated(board, media, op="delete-old", **patch):
    args = {"project_id": "100", "episode_id": "200", "segment_id": "400", "replacement_segment_ids": ["401"],
            "expected_sha256": fingerprint(media.client.board), "backup_dir": str(media.client.backup_root), "operation_id": op, **patch}
    return board.delete_segment(**args)


def test_delete_migrated_segment_backs_up_before_native_delete(media, tmp_path):
    board = migrated_board(media, tmp_path)
    before = copy.deepcopy(media.client.board)
    digest = fingerprint(before)
    r = delete_migrated(board, media)
    assert r["verified"] and r["deleted_segment_id"] == "400" and r["replacement_segment_ids"] == ["401"]
    assert media.client.backup_before_delete
    backup = json.loads(Path(r["backup"]["path"]).read_text())
    assert backup["storyboard"] == before and backup["storyboard_sha256"] == digest
    assert writes(media) == [("DELETE", "/api/v1/pixa/short-drama/segments/400", None, None)]
    assert r["storyboard"]["segments"][0]["segment_id"] == "401"
    assert delete_migrated(board, media, expected_sha256=digest) == r
    assert len(writes(media)) == 1


@pytest.mark.parametrize("replacement", [[], ["400"], ["401", "401"], ["999"]])
def test_delete_requires_real_same_episode_replacements(media, tmp_path, replacement):
    board = migrated_board(media, tmp_path)
    with pytest.raises(ValueError):
        delete_migrated(board, media, replacement_segment_ids=replacement)
    assert not writes(media)


def test_delete_rejects_empty_replacement_selected_or_history_media(media, tmp_path):
    board = migrated_board(media, tmp_path)
    replacement = media.client.board["segments"][1]
    original = copy.deepcopy(replacement)
    replacement["scenes"][0]["shots"] = []
    with pytest.raises(ValueError, match="非空"):
        delete_migrated(board, media, op="empty")
    media.client.board["segments"][1] = original
    media.client.board["segments"][0]["selected_keyframe_url"] = "https://cdn.example/old.png"
    with pytest.raises(ValueError, match="已选"):
        delete_migrated(board, media, op="selected")
    media.client.board["segments"][0]["selected_keyframe_url"] = ""
    media.client.histories["segment_video"]["items"] = [{"generation_task_id": "300", "task_status": "failed"}]
    with pytest.raises(ValueError, match="历史"):
        delete_migrated(board, media, op="history")
    assert not writes(media)


@pytest.mark.parametrize("flag", ["no_delete", "corrupt_text", "timeout"])
def test_delete_unknown_result_blocks_new_id(media, tmp_path, flag):
    board = migrated_board(media, tmp_path)
    setattr(media.client, flag, True)
    with pytest.raises(OperationUnknown):
        delete_migrated(board, media, op="unknown-delete")
    count = len(writes(media))
    setattr(media.client, flag, False)
    with pytest.raises((OperationUnknown, ValueError)):
        delete_migrated(board, media, op="cannot-repost")
    assert len(writes(media)) == count
    assert media.store.get_operation("unknown-delete")["status"] == "UNKNOWN"


def test_delete_requires_latest_fingerprint_and_absolute_backup(media, tmp_path):
    board = migrated_board(media, tmp_path)
    with pytest.raises(ValueError, match="改变"):
        delete_migrated(board, media, op="stale", expected_sha256="old")
    with pytest.raises(ValueError, match="绝对"):
        delete_migrated(board, media, op="bad-backup", backup_dir="relative")
    assert not writes(media)
