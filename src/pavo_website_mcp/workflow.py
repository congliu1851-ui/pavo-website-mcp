"""媒体传输、生成生命周期及资产。生成与后处理分别留存回执。"""

import hashlib
import json
import mimetypes
import os
from pathlib import Path
import tempfile
from urllib.parse import urljoin, urlsplit

from .canvas import decode_data, identifier
from .client import APIError
from .media import safe_url
from .store import OperationUnknown, canonical


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def read_download_receipt(path):
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError("下载回执路径不是普通文件")
    try:
        result = json.loads(path.read_text())
        return result if isinstance(result, dict) else None
    except (ValueError, UnicodeError):
        return None


def inspect_media(path):
    p = Path(path).expanduser()
    if not p.is_absolute() or p.is_symlink() or not p.is_file() or p.stat().st_size == 0:
        raise ValueError("上传须指定非空普通媒体文件的绝对路径，不接受符号链接")
    content_type = mimetypes.guess_type(p.name)[0] or ""
    media_type = content_type.split("/")[0]
    if media_type not in {"image", "video", "audio"} or content_type in {"image/svg+xml"}:
        raise ValueError("上传只接受图片、视频和音频媒体文件，不接受SVG脚本或未知类型")
    return p, content_type, media_type


class Workflow:
    def __init__(self, pavo):
        self.pavo, self.client, self.store = pavo, pavo.client, pavo.store

    def upload_media(self, path, operation_id):
        p, content_type, media_type = inspect_media(path)
        size, digest = p.stat().st_size, sha256(p)
        payload = {"path": str(p), "sha256": digest, "bytes": size, "content_type": content_type}
        def action(rid):
            data = self.client.request("POST", "/api/v1/file/presigned-url", {"purpose": "ugc_" + media_type, "content_type": content_type, "filename": p.name})
            if not isinstance(data, dict) or str(data.get("method", "")).upper() != "PUT":
                raise APIError("预签名上传响应缺少PUT契约")
            safe_url(data.get("upload_url"))
            safe_url(data.get("public_url"))
            headers = data.get("required_headers") or {}
            if not any(k.lower() == "content-type" for k in headers):
                raise APIError("预签名上传响应缺少Content-Type")
            if any(k.lower() in {"authorization", "cookie", "proxy-authorization"} for k in headers):
                raise ValueError("预签名上传不能要求转发登录凭证")
            with p.open("rb") as f:
                response = self.client.media.put(data["upload_url"], content=f, headers={**headers, "Content-Length": str(size)})
            if not 200 <= response.status_code < 300:
                raise APIError(f"媒体PUT未确认成功，HTTP {response.status_code}")
            if p.stat().st_size != size or sha256(p) != digest:
                raise APIError("上传期间本地文件发生变化，需要人工核对")
            return {"public_url": data["public_url"], "content_type": content_type, "media_type": media_type, "filename": p.name, "bytes": size, "sha256": digest}
        return self.store.operation(operation_id, "upload_media", payload, action)

    def submit_task(self, project_uuid, canvas_uuid, node_key, operation_id, regenerate=False):
        identifier(project_uuid), identifier(canvas_uuid), identifier(node_key)
        payload = {"project_uuid": project_uuid, "canvas_uuid": canvas_uuid, "node_key": node_key, "regenerate": regenerate}
        resource = canonical([self.store.account_key(), project_uuid, canvas_uuid, node_key])
        def action(rid):
            with self.store.resource_lock(resource):
                previous = self.store.claim_owner(resource)
                replace = None
                if previous and previous != operation_id and regenerate:
                    old = self.store.get_operation(previous)
                    if old["status"] != "SUCCESS" or not old["result"].get("task_id"):
                        raise OperationUnknown("原生成结果未知，禁止再次生成")
                    if not self.get_task(old["result"]["task_id"])["terminal"]:
                        raise ValueError("原生成任务仍未结束")
                    replace = previous
                detail = self.pavo.get_project(project_uuid, canvas_uuid)
                node = self.pavo.find_node(detail, node_key)
                data, kind = decode_data(node["data"]), str(node["node_type"])
                if kind not in {"text", "image", "video", "audio"} or data.get("isExecutable") is False:
                    raise ValueError("该节点不可生成")
                active = str(data.get("task_id", "-1"))
                if active not in {"", "-1", "None"}:
                    raise ValueError("官网节点已有任务ID，先核对原任务，不能重复生成")
                model = data.get("params", {}).get("model")
                if not model:
                    raise ValueError("提交前必须明确选择账号实时模型")
                before = canonical(data.get("params", {}))
                self.pavo.configure_model(data, kind, model)
                if canonical(data.get("params", {})) != before:
                    raise ValueError("官网节点参数不完整或已过期，请先更新模型配置并重新预检")
                valid = self.pavo.validate_canvas(project_uuid, canvas_uuid)
                if not valid["valid"]:
                    raise ValueError("画布未通过验证：" + "；".join(valid["errors"]))
                self.store.claim(resource, operation_id, replace)
                try:
                    created = self.client.request("POST", f"/api/v1/pixa/canvas/project/{project_uuid}/generation/create", {"nodeKey": node_key, "requestId": rid, "canvasUuid": canvas_uuid})
                    if isinstance(created, dict) and (created.get("status") == "failed" or created.get("errorCode")):
                        raise APIError("官网明确拒绝创建生成任务", definite=True)
                except APIError as e:
                    if e.definite:
                        self.store.release_claim(resource, operation_id, replace)
                    raise
                if not isinstance(created, dict):
                    raise APIError("创建任务回执不完整，提交结果未知")
                task_id = created.get("task_id") or created.get("taskId")
                if task_id is None or isinstance(task_id, (dict, list, bool)) or not str(task_id).strip():
                    raise APIError("创建任务未返回真实任务ID，提交结果未知")
                return {**created, "task_id": str(task_id), "project_uuid": project_uuid, "canvas_uuid": canvas_uuid,
                        "node_key": node_key, "operation_id": operation_id, "request_id": rid, "params_sha256": hashlib.sha256(before.encode()).hexdigest(),
                        "canvas_url": detail["canvas_url"], "previous_task_id": old["result"]["task_id"] if replace else None}
        return self.store.operation(operation_id, "submit_task", payload, action)

    def get_task(self, task_id):
        task_id = identifier(str(task_id))
        result = self.client.request("POST", "/api/v1/pixa/canvas/generation/progress", {"taskIds": [task_id]})
        task = next((t for t in (result or {}).get("progresses", []) if str(t.get("taskId")) == task_id), None)
        if task is None:
            return {"task_id": task_id, "found": False, "terminal": False, "succeeded": False, "results": []}
        raw = task.get("taskResult") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = {}
        results = raw.get("results", []) if isinstance(raw, dict) else []
        state = str(task.get("status"))
        failed = state == "3" or bool(task.get("errorCode")) or str(task.get("statusText", "")).lower() in {"failed", "error", "canceled", "cancelled"}
        terminal = failed or state == "2"
        return {**task, "task_id": task_id, "found": True, "terminal": terminal, "succeeded": terminal and not failed,
                "results": results if isinstance(results, list) else []}

    def sync_task_result(self, submission_operation_id, operation_id):
        return self.store.operation(operation_id, "sync_task_result", {"submission_operation_id": submission_operation_id},
            lambda rid: self._sync_task_result(submission_operation_id, "sync-" + hashlib.sha256(operation_id.encode()).hexdigest()))

    def _sync_task_result(self, submission_operation_id, operation_id):
        original = self.store.get_operation(submission_operation_id)
        if original["kind"] != "submit_task" or original["status"] != "SUCCESS":
            raise ValueError("需先取得本地成功保存的生成提交回执；UNKNOWN不得直接回写")
        r = original["result"]
        resource = canonical([self.store.account_key(), r["project_uuid"], r["canvas_uuid"], r["node_key"]])
        with self.store.resource_lock(resource):
            if self.store.claim_owner(resource) != submission_operation_id:
                raise ValueError("节点已有更新的生成提交，不能回写旧任务")
            task = self.get_task(r["task_id"])
            if not task["found"]:
                raise ValueError("任务历史暂缺，不能清空已有节点结果")
            n = self.pavo.get_node(r["project_uuid"], r["canvas_uuid"], r["node_key"])
            data = n["data"]
            active = str(data.get("task_id", "-1"))
            copied = str(data.get("copyTaskId", "-1"))
            if active not in {"", "-1", r["task_id"]} or copied not in {"", "-1", r["task_id"], r.get("previous_task_id")}:
                raise ValueError("该节点已关联其他生成任务，不能回写覆盖")
            params_hash = hashlib.sha256(canonical(data.get("params", {})).encode()).hexdigest()
            if params_hash != r["params_sha256"]:
                raise ValueError("提交后节点参数已改变，不能回写旧结果")
            patch = {"copyTaskId": r["task_id"], "task_id": r["task_id"], "status": "generating", "progress": task.get("progressPercent", 0)}
            if task["terminal"]:
                patch.update(task_id="-1", status="completed" if task["succeeded"] else "failed", progress=-1, generation_status=task.get("status"))
                if task["succeeded"]:
                    if str(n["node_type"]) == "text":
                        texts = [i.get("text_content", i.get("textContent")) for i in task["results"] if isinstance(i, dict) and i.get("mime_type", i.get("mimeType")) == "text/markdown"]
                        if not texts:
                            raise ValueError("成功任务没有可识别的文字结果")
                        patch.update(content=texts[0], mode="authoring")
                    else:
                        urls = [i["url"] for i in task["results"] if isinstance(i, dict) and i.get("url")]
                        if not urls:
                            raise ValueError("成功任务没有真实媒体结果")
                        patch["url"] = urls
                else:
                    patch["generation_error_code"] = task.get("errorCode") or "FAILED"
            return self.pavo.update_node(r["project_uuid"], r["canvas_uuid"], r["node_key"], operation_id, data_patch=patch, expected_data=data)

    def _download_one(self, url, destination):
        current = url
        for _ in range(6):
            safe_url(current)
            with self.client.media.stream("GET", current, headers={"Accept-Encoding": "identity"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("下载重定向缺少地址")
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise APIError(f"下载返回HTTP {response.status_code}")
                declared = response.headers.get("content-length")
                declared = int(declared) if declared is not None else None
                fd, tmp = tempfile.mkstemp(prefix=".pavo-", suffix=".part", dir=destination.parent)
                total, digest = 0, hashlib.sha256()
                try:
                    with os.fdopen(fd, "wb") as f:
                        for chunk in response.iter_bytes(256 * 1024):
                            total += len(chunk)
                            if total > 20 * 1024**3:
                                raise ValueError("单文件超过20GiB，请人工核对")
                            digest.update(chunk)
                            f.write(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                    if total == 0 or (declared is not None and total != declared):
                        raise ValueError("下载为空或长度不匹配")
                    # 硬链接是同文件系统的原子不覆盖落盘，避免检查与替换之间的竞态。
                    reused = False
                    try:
                        os.link(tmp, destination)
                    except FileExistsError:
                        if destination.is_symlink() or not destination.is_file() or sha256(destination) != digest.hexdigest() or destination.stat().st_size != total:
                            raise ValueError("目标文件已存在且与真实任务下载不一致，拒绝覆盖") from None
                        reused = True
                    return {"path": str(destination), "bytes": total, "sha256": digest.hexdigest(), "reused": reused}
                finally:
                    os.unlink(tmp)
        raise ValueError("下载重定向次数过多")

    def download_task_results(self, task_id, output_dir):
        task = self.get_task(task_id)
        if not task["succeeded"]:
            raise ValueError("只有原生成功的任务才能下载，历史暂缺或失败不能当成成功")
        out = Path(output_dir).expanduser()
        if not out.is_absolute() or out.is_symlink():
            raise ValueError("输出目录须为明确的绝对目录，不能为符号链接")
        out.mkdir(parents=True, exist_ok=True)
        out = out.resolve()
        files, errors = [], []
        for i, item in enumerate(task["results"]):
            try:
                if not isinstance(item, dict) or not item.get("url"):
                    raise ValueError("该结果不含媒体地址")
                url = item["url"]
                extension = Path(urlsplit(url).path).suffix.lower()
                if extension not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".webm", ".mp3", ".wav", ".flac", ".m4a", ".ogg"}:
                    extension = mimetypes.guess_extension(item.get("mime_type", item.get("mimetype", ""))) or ".bin"
                destination = out / f"pavo-{identifier(str(task_id))}-{i + 1:02d}{extension}"
                receipt_path = out / (destination.name + ".pavo.json")
                if destination.exists() or destination.is_symlink():
                    if destination.is_symlink() or not destination.is_file() or receipt_path.is_symlink():
                        raise ValueError("目标文件已存在，拒绝覆盖")
                    if receipt_path.is_file():
                        receipt = read_download_receipt(receipt_path)
                        if receipt:
                            if receipt.get("task_id") != str(task_id) or receipt.get("sha256") != sha256(destination) or destination.stat().st_size == 0:
                                raise ValueError("现有文件与下载回执不符，拒绝覆盖")
                            files.append({"path": str(destination), "bytes": destination.stat().st_size, "sha256": receipt["sha256"], "reused": True})
                            continue
                result = self._download_one(url, destination)
                receipt = {"task_id": str(task_id), "index": i, **result}
                previous = read_download_receipt(receipt_path)
                if previous and (previous.get("sha256") != result["sha256"] or previous.get("task_id") != str(task_id)):
                    raise ValueError("下载回执路径已占用且内容不同")
                fd, temporary = tempfile.mkstemp(dir=out, prefix=".pavo-receipt-")
                try:
                    with os.fdopen(fd, "w") as f:
                        f.write(canonical(receipt))
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(temporary, receipt_path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                files.append(result)
            except Exception as e:
                errors.append({"index": i, "error": str(e) if isinstance(e, (ValueError, APIError)) else "文件传输或落盘失败，未重新生成"})
        return {"task_id": str(task_id), "files": files, "errors": errors, "complete": bool(files) and not errors}

    def save_node_asset(self, project_uuid, canvas_uuid, node_key, operation_id, resource_index=0, name=""):
        identifier(project_uuid), identifier(canvas_uuid), identifier(node_key)
        if not isinstance(resource_index, int) or isinstance(resource_index, bool) or resource_index < 0:
            raise ValueError("resource_index须为非负整数")
        payload = {"canvasUuid": canvas_uuid, "items": [{"nodeKey": node_key, "resourceIndex": resource_index, "name": name}]}
        def action(rid):
            node = self.pavo.get_node(project_uuid, canvas_uuid, node_key)
            urls = node["data"].get("url", [])
            if not isinstance(urls, list) or resource_index >= len(urls) or not urls[resource_index]:
                raise ValueError("节点指定资源不存在，请先回写真实生成结果或上传素材")
            return self.client.request("POST", f"/api/v1/pixa/canvas/project/{project_uuid}/media-assets/batch-create", payload)
        return self.store.operation(operation_id, "save_node_asset", {"project_uuid": project_uuid, **payload}, action)

    def list_assets(self, category="images", page=1, page_size=20):
        if category not in {"images", "videos"} or not 1 <= page or not 1 <= page_size <= 100:
            raise ValueError("资产category须为images或videos，page>=1，page_size为1至100")
        return self.client.request("GET", "/api/v1/visuals", params={"category": category, "page": page, "page_size": page_size})

    def list_artifacts(self, project_uuid, canvas_uuid, category="all", page=1, page_size=20):
        identifier(project_uuid), identifier(canvas_uuid)
        if category not in {"all", "images", "videos"} or not 1 <= page or not 1 <= page_size <= 100:
            raise ValueError("历史产物分页或category无效")
        return self.client.request("GET", f"/api/v1/pixa/canvas/project/{project_uuid}/artifacts", params={"canvas_uuid": canvas_uuid, "category": category, "page": page, "page_size": page_size})
