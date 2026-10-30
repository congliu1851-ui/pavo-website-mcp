import copy
import hashlib
import json
import socket

import httpx
import pytest

from pavo_website_mcp.canvas import Pavo, decode_data
from pavo_website_mcp.client import Client
from pavo_website_mcp.store import Store, OperationUnknown
from pavo_website_mcp.workflow import Workflow, safe_url
from test_canvas import FakeClient, node


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))])


def test_upload_presigned_no_bearer_and_no_signed_receipt(tmp_path):
    store = Store(tmp_path / "state")
    store.save_session("secret", {})
    calls = []
    def api(req):
        calls.append(str(req.url))
        assert req.headers["authorization"] == "Bearer secret"
        assert json.loads(req.content)["purpose"] == "ugc_image"
        return httpx.Response(200, json={"code": "000000", "data": {"upload_url": "https://storage.example/file?signature=private", "public_url": "https://cdn.example/file.png", "method": "PUT", "required_headers": {"Content-Type": "image/png"}}})
    def upload(req):
        calls.append(str(req.url))
        assert "authorization" not in req.headers
        assert req.headers["content-type"] == "image/png"
        assert req.read().startswith(b"\x89PNG")
        return httpx.Response(200)
    p = tmp_path / "测试.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 100)
    workflow = Workflow(Pavo(Client(store, httpx.MockTransport(api), httpx.MockTransport(upload)), store))
    result = workflow.upload_media(str(p), "upload1")
    assert result["sha256"] == hashlib.sha256(p.read_bytes()).hexdigest()
    assert result["public_url"] == "https://cdn.example/file.png"
    assert "signature" not in json.dumps(store.get_operation("upload1"))
    assert workflow.upload_media(str(p), "upload1") == result
    assert len(calls) == 2


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/a", "file:///etc/passwd", "https://user:password@cdn.example/a", "https://192.168.0.1/a", "https://cdn.example:8188/a"])
def test_unsafe_media_urls_refused(url):
    with pytest.raises(ValueError):
        safe_url(url)


class TaskClient(FakeClient):
    def __init__(self, task=None):
        super().__init__([node(data={"params": {"model": "live-video", "prompt": [{"type": "text", "content": "中文提示"}],
            "settings": {"ratio": "9:16", "resolution": "720p", "generateAudio": False},
            "duration": 5, "modeType": ["frames_to_video"], "power": 0}})])
        self.task = task or {"taskId": 123, "status": 2, "taskResult": json.dumps({"results": [{"url": "https://cdn.example/actual.mp4", "mime_type": "video/mp4"}]})}
        self.submits = []
        self.timeout = False
    def request(self, method, path, payload=None, params=None, **kw):
        if path.endswith("generation/create"):
            self.submits.append(payload)
            if self.timeout:
                raise TimeoutError("超时")
            return {"task_id": 123, "status": "pending"}
        if path.endswith("generation/progress"):
            return {"progresses": [copy.deepcopy(self.task)]}
        if path.endswith("media-assets/batch-create"):
            return {"saved": payload}
        return super().request(method, path, payload, params, **kw)


def test_task_submit_receipt_persisted_even_if_node_sync_fails(tmp_path):
    c, store = TaskClient(), Store(tmp_path)
    w = Workflow(Pavo(c, store))
    result = w.submit_task("p", "c", "v-abcdefghijkl", "gen1")
    assert result["task_id"] == "123"
    assert not c.batches  # 回写是单独工具，创建回执不受回写失败影响。
    assert w.submit_task("p", "c", "v-abcdefghijkl", "gen1") == result
    assert len(c.submits) == 1


@pytest.mark.parametrize('reference_kind,count,total_limit,role', [('image',6,8,'reference'),('video',1,8,'reference'),('image',5,4,'reference'),('image',6,8,'prompt')])
def test_reference_capability_violation_never_submits(tmp_path,reference_kind,count,total_limit,role):
    c=TaskClient()
    original=c.request
    def request(method,path,payload=None,params=None,**kw):
        result=original(method,path,payload,params,**kw)
        if path.endswith('model_options'):
            result['items'][0]['capability_spec']={'video':{'input_image':{'max_count':5},'max_total_count':total_limit}}
        return result
    c.request=request
    for i in range(count):
        key='ref-'+str(i)
        c.detail['node_list'].append(node(key,'upload',{'mediaType':reference_kind,'url':['https://cdn.example/reference']}))
        c.detail['connection_list'].append({'source':key,'target':'v-abcdefghijkl','role':role,'media_order':i})
    w=Workflow(Pavo(c,Store(tmp_path)))
    assert w.pavo.validate_canvas('p','c')['valid'] is False
    with pytest.raises(ValueError,match='参考|输入'):
        w.submit_task('p','c','v-abcdefghijkl','invalid-reference')
    assert c.submits==[]


def test_reference_at_model_limit_can_submit(tmp_path):
    c=TaskClient();original=c.request
    def request(method,path,payload=None,params=None,**kw):
        result=original(method,path,payload,params,**kw)
        if path.endswith('model_options'):
            result['items'][0]['capability_spec']={'video':{'input_image':{'max_count':5},'max_total_count':8}}
        return result
    c.request=request
    for i in range(5):
        key='ref-'+str(i)
        c.detail['node_list'].append(node(key,'upload',{'mediaType':'image','url':['https://cdn.example/reference']}))
        c.detail['connection_list'].append({'source':key,'target':'v-abcdefghijkl','role':'reference','media_order':i})
    result=Workflow(Pavo(c,Store(tmp_path))).submit_task('p','c','v-abcdefghijkl','valid-reference')
    assert result['task_id']=='123' and len(c.submits)==1


def test_unknown_submit_not_retried_with_new_operation_id(tmp_path):
    c, store = TaskClient(), Store(tmp_path)
    c.timeout = True
    w = Workflow(Pavo(c, store))
    with pytest.raises(OperationUnknown):
        w.submit_task("p", "c", "v-abcdefghijkl", "gen1")
    with pytest.raises(OperationUnknown):
        w.submit_task("p", "c", "v-abcdefghijkl", "gen2")
    assert len(c.submits) == 1


def test_task_status_does_not_promote_url_to_success(tmp_path):
    c = TaskClient({"taskId": "123", "status": 3, "taskResult": {"results": [{"url": "https://cdn.example/a.mp4"}]}})
    w = Workflow(Pavo(c, Store(tmp_path)))
    assert w.get_task("123")["succeeded"] is False
    with pytest.raises(ValueError, match="成功"):
        w.download_task_results("123", str(tmp_path / "out"))


def test_download_atomic_nonempty_sha_and_no_overwrite(tmp_path):
    c = TaskClient()
    c.media = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"real video bytes")))
    w = Workflow(Pavo(c, Store(tmp_path / "state")))
    result = w.download_task_results("123", str(tmp_path / "out"))
    item = result["files"][0]
    assert item["sha256"] == hashlib.sha256(b"real video bytes").hexdigest()
    assert item["bytes"] == 16
    again = w.download_task_results("123", str(tmp_path / "out"))
    assert again["files"][0]["reused"]
    from pathlib import Path
    Path(item["path"]).write_bytes(b"other user file")
    conflict = w.download_task_results("123", str(tmp_path / "out"))
    assert conflict["errors"]
    assert Path(item["path"]).read_bytes() == b"other user file"


def test_download_redirect_to_private_rejected(tmp_path):
    c = TaskClient()
    c.media = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(302, headers={"Location": "http://127.0.0.1/a"})))
    result = Workflow(Pavo(c, Store(tmp_path / "state"))).download_task_results("123", str(tmp_path / "out"))
    assert result["errors"]
    assert not result["files"]


def test_sync_and_save_asset_exact_fields(tmp_path):
    c, store = TaskClient(), Store(tmp_path)
    w = Workflow(Pavo(c, store))
    submitted = w.submit_task("p", "c", "v-abcdefghijkl", "gen1")
    w.sync_task_result("gen1", "sync1")
    data = decode_data(c.batches[-1]["nodes"]["update"][0]["data"])
    assert data["url"] == ["https://cdn.example/actual.mp4"]
    assert data["copyTaskId"] == submitted["task_id"]
    c.detail["node_list"][0]["data"] = json.dumps(data)
    saved = w.save_node_asset("p", "c", "v-abcdefghijkl", "asset1", name="联调视频")
    assert saved["saved"] == {"canvasUuid": "c", "items": [{"nodeKey": "v-abcdefghijkl", "resourceIndex": 0, "name": "联调视频"}]}


def test_sync_refuses_edit_between_check_and_batch(tmp_path):
    c, store = TaskClient(), Store(tmp_path)
    w = Workflow(Pavo(c, store))
    w.submit_task("p", "c", "v-abcdefghijkl", "gen1")
    original_request = c.request
    reads = 0
    def changed(method, path, payload=None, params=None, **kw):
        nonlocal reads
        if path.endswith("/detail"):
            reads += 1
            if reads == 2:
                data = decode_data(c.detail["node_list"][0]["data"])
                data.update(task_id="999", copyTaskId="999")
                c.detail["node_list"][0]["data"] = json.dumps(data)
                c.detail["version"] = 8
        return original_request(method, path, payload, params, **kw)
    c.request = changed
    with pytest.raises(ValueError, match="改变|冲突"):
        w.sync_task_result("gen1", "sync1")
    assert not c.batches


def test_definite_rejection_releases_claim(tmp_path):
    from pavo_website_mcp.client import APIError
    c, store = TaskClient(), Store(tmp_path)
    original = c.request
    def reject(method, path, payload=None, params=None, **kw):
        if path.endswith("generation/create"):
            raise APIError("官网明确拒绝", definite=True)
        return original(method, path, payload, params, **kw)
    c.request = reject
    w = Workflow(Pavo(c, store))
    with pytest.raises(APIError):
        w.submit_task("p", "c", "v-abcdefghijkl", "reject1")
    c.request = original
    assert w.submit_task("p", "c", "v-abcdefghijkl", "accepted2")["task_id"] == "123"


def test_download_file_without_sidecar_recovers_by_exact_redownload(tmp_path):
    c = TaskClient()
    c.media = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"real video bytes")))
    w = Workflow(Pavo(c, Store(tmp_path / "state")))
    first = w.download_task_results("123", str(tmp_path / "out"))
    from pathlib import Path
    destination = Path(first["files"][0]["path"])
    Path(str(destination) + ".pavo.json").unlink()
    second = w.download_task_results("123", str(tmp_path / "out"))
    assert second["complete"]
    assert second["files"][0]["sha256"] == first["files"][0]["sha256"]


def test_regenerate_lineage_and_sync_idempotency(tmp_path):
    c, store = TaskClient(), Store(tmp_path)
    original = c.request
    count = 122
    def actual(method, path, payload=None, params=None, **kw):
        nonlocal count
        if path.endswith("generation/create"):
            count += 1
            c.task["taskId"] = count
            return {"taskId": count}
        result = original(method, path, payload, params, **kw)
        if path.endswith("nodes/batch"):
            for item in payload["nodes"]["update"]:
                n = next(n for n in c.detail["node_list"] if n["node_key"] == item["nodeKey"])
                n["data"] = item["data"]
        return result
    c.request = actual
    w = Workflow(Pavo(c, store))
    assert w.submit_task("p", "c", "v-abcdefghijkl", "g1")["task_id"] == "123"
    first_sync = w.sync_task_result("g1", "s1")
    assert w.sync_task_result("g1", "s1") == first_sync
    assert w.submit_task("p", "c", "v-abcdefghijkl", "g2", regenerate=True)["task_id"] == "124"
    w.sync_task_result("g2", "s2")
    assert decode_data(c.detail["node_list"][0]["data"])["copyTaskId"] == "124"
    with pytest.raises(ValueError, match="旧任务"):
        w.sync_task_result("g1", "late-s1")


@pytest.mark.parametrize("broken", ["", '{"sha256":'])
def test_partial_receipt_recovers_only_identical_media(tmp_path, broken):
    c = TaskClient()
    c.media = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=b"real video bytes")))
    w = Workflow(Pavo(c, Store(tmp_path / "state")))
    r = w.download_task_results("123", str(tmp_path / "out"))
    from pathlib import Path
    receipt = Path(r["files"][0]["path"] + ".pavo.json")
    receipt.write_text(broken)
    assert w.download_task_results("123", str(tmp_path / "out"))["complete"]
    assert json.loads(receipt.read_text())["task_id"] == "123"


def test_failed_regeneration_restores_previous_lineage(tmp_path):
    from pavo_website_mcp.client import APIError
    c, store = TaskClient(), Store(tmp_path)
    original = c.request
    rejected = False
    def req(method, path, payload=None, params=None, **kw):
        if path.endswith("generation/progress"):
            # 官网保留前序任务历史；不能让夹具只返回后来任务而伪造历史暂缺。
            history = copy.deepcopy(c.task)
            history["taskId"] = payload["taskIds"][0]
            return {"progresses": [history]}
        if path.endswith("generation/create"):
            if rejected:
                raise APIError("明确拒绝", definite=True)
            return {"taskId": c.task["taskId"]}
        result = original(method, path, payload, params, **kw)
        if path.endswith("nodes/batch"):
            c.detail["node_list"][0]["data"] = payload["nodes"]["update"][0]["data"]
        return result
    c.request = req
    w = Workflow(Pavo(c, store))
    w.submit_task("p", "c", "v-abcdefghijkl", "g1")
    w.sync_task_result("g1", "s1")
    rejected = True
    with pytest.raises(APIError):
        w.submit_task("p", "c", "v-abcdefghijkl", "g2", regenerate=True)
    rejected = False
    c.task["taskId"] = 124
    assert w.submit_task("p", "c", "v-abcdefghijkl", "g3", regenerate=True)["previous_task_id"] == "123"
    w.sync_task_result("g3", "s3")
    assert decode_data(c.detail["node_list"][0]["data"])["copyTaskId"] == "124"
