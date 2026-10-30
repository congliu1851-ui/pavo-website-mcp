"""官网playlet剧本业务；与画布和聊天短剧代理分别处理。"""

import copy
import hashlib
import html
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET
import zipfile

from .canvas import identifier
from .client import APIError
from .media import safe_url
from .store import OperationUnknown, canonical
from .workflow import sha256

ROOT = "/api/v1/pixa/short-drama"
OUTLINE_FIELDS = {"story_type", "target_audience", "one_line_story", "story_summary", "world_setting", "character_biographies"}
STAGES = {"confirm_outline": "/outline/confirm", "extract_assets": "/asset-extract", "retry_outline": "/outline/retry",
          "retry_episodes": "/episodes/retry", "resume_episodes": "/episodes/resume",
          "generate_episode": None, "generate_storyboard": None, "finalize_assets": "/assets/finalize"}


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def check_text(text, label="剧本", maximum=100000):
    if not isinstance(text, str) or not text.strip():
        raise ValueError(label + "不能为空")
    if len(text.encode("utf-16-le")) // 2 > maximum:
        raise ValueError(f"{label}不能超过{maximum}个UTF-16字符单位，与官网计数一致")


def task_ids(data):
    result = []
    if isinstance(data, dict):
        if data.get("task_id") is not None:
            result.append(str(data["task_id"]))
        for key in ("task", "latest_task", "tasks", "active_tasks"):
            value = data.get(key)
            if isinstance(value, dict):
                result.extend(task_ids(value))
            elif isinstance(value, list):
                for item in value:
                    result.extend(task_ids(item))
    return list(dict.fromkeys(result))


class Script:
    def __init__(self, client, store):
        self.client, self.store = client, store

    def list_styles(self):
        return self.client.request("GET", ROOT + "/styles")

    def list_projects(self, page=1, page_size=12, is_demo=False):
        if isinstance(page, bool) or not isinstance(page, int) or page < 1 or not isinstance(page_size, int) or not 1 <= page_size <= 100:
            raise ValueError("page须>=1，page_size为1至100")
        return self.client.request("GET", ROOT + "/projects", params={"page": page, "page_size": page_size, "is_demo": str(is_demo).lower()})

    def _project(self, project_id, include_content=True):
        project_id = identifier(str(project_id))
        raw = self.client.request("GET", ROOT + f"/projects/{project_id}/progress", params={"include_content": str(include_content).lower()})
        if not isinstance(raw, dict) or str(raw.get("project_id")) != project_id or not isinstance(raw.get("episodes"), list):
            raise APIError("剧本项目详情结构或项目ID不符，停止写入")
        return raw

    def get_project(self, project_id, include_content=True):
        raw = self._project(project_id, include_content)
        return {"project": raw, "fingerprints": {"outline": fingerprint(raw.get("outline")), "episodes": fingerprint(raw["episodes"])},
                "content_included": include_content, "project_url": f"https://app.pavo-ai.cn/playlet/review/{identifier(str(project_id))}",
                "concurrency_limit": "官网剧本接口未提供version CAS；指纹是写前检查，不能消除网页同时编辑的全部竞态"}

    @staticmethod
    def _episode(project, episode_id):
        for e in project["episodes"]:
            if str(e.get("episode_id")) == str(episode_id):
                return e
        raise ValueError("指定分集不属于该项目，不能读取或修改其他项目分集")

    def get_episode(self, project_id, episode_id):
        self._episode(self._project(project_id), episode_id)
        result = self.client.request("GET", ROOT + f"/episodes/{identifier(str(episode_id))}")
        if not isinstance(result, dict) or str(result.get("project_id")) != str(project_id):
            raise APIError("分集响应项目归属不符")
        return result

    def get_assets(self, project_id):
        self._project(project_id, False)
        return self.client.request("GET", ROOT + f"/projects/{identifier(str(project_id))}/assets")

    def get_storyboard(self, project_id, episode_id):
        self._episode(self._project(project_id, False), episode_id)
        return self.client.request("GET", ROOT + f"/episodes/{identifier(str(episode_id))}/storyboard/detail")

    def _style(self, ratio, style):
        if ratio not in {"9:16", "16:9"}:
            raise ValueError("剧本画幅须为9:16或16:9")
        if not isinstance(style, dict) or set(style) not in ({"style_id"}, {"custom_style_prompt"}):
            raise ValueError("风格须选择style_id或custom_style_prompt其中一项")
        if "style_id" in style:
            ids = {str(s.get("style_id")) for category in self.list_styles().get("categories", []) for s in category.get("styles", [])}
            if str(style["style_id"]) not in ids:
                raise ValueError("风格ID不在官网实时风格库")
        else:
            check_text(style["custom_style_prompt"], "自定义风格", 200)

    def _create(self, endpoint, payload, operation_id):
        def action(rid):
            self._style(payload["ratio"], payload["style"])
            resource = canonical([self.store.account_key(), "script-create", endpoint, fingerprint(payload)])
            with self.store.resource_lock(resource):
                owner = self.store.claim_owner(resource)
                if owner and owner != operation_id and self.store.get_operation(owner)["status"] not in {"SUCCESS", "FAILED"}:
                    raise OperationUnknown("相同剧本输入存在未知创建，禁止换号重投；先核官网真实项目回执")
                self.store.claim(resource, operation_id, owner)
                try:
                    result = self.client.request("POST", ROOT + "/projects/" + endpoint, payload)
                except APIError as e:
                    if e.definite:self.store.release_claim(resource, operation_id, owner)
                    raise
                if not isinstance(result, dict) or not result.get("project_id"):
                    raise APIError("创建剧本未返回真实project_id，结果未知，禁止重投")
                try:
                    pid = identifier(str(result["project_id"]))
                except ValueError:
                    raise APIError("创建剧本回执项目ID格式异常，结果未知") from None
                return {**result, "project_id": pid, "task_ids": task_ids(result),
                        "operation_id": operation_id, "project_url": f"https://app.pavo-ai.cn/playlet/review/{pid}"}
        return self.store.operation(operation_id, "script_create_" + endpoint, payload, action)

    def create_ai(self, requirements, planned_episode_count, ratio, style, operation_id):
        check_text(requirements, "创作要求")
        if isinstance(planned_episode_count, bool) or not isinstance(planned_episode_count, int) or not 1 <= planned_episode_count <= 100:
            raise ValueError("计划集数须为1至100的整数")
        return self._create("ai-generate", {"requirements": requirements, "planned_episode_count": planned_episode_count, "ratio": ratio, "style": style}, operation_id)

    def import_text(self, raw_text, title, ratio, style, operation_id, planned_episode_count):
        check_text(raw_text)
        check_text(title, "项目标题", 200)
        requirements = f"请根据以下剧情生成分集剧本，由你安排各集的情节划分。\n\n剧情标题：{title}\n\n剧情原文：\n{raw_text}"
        return self.create_ai(requirements, planned_episode_count, ratio, style, operation_id)

    @staticmethod
    def _read_file(path):
        p = Path(path).expanduser()
        if not p.is_absolute() or p.is_symlink() or not p.is_file() or not 0 < p.stat().st_size <= 32 * 1024**2:
            raise ValueError("剧本文件须为非空普通文件绝对路径，MCP本地上限32MiB")
        with p.open("rb") as f:
            content = f.read(32 * 1024**2 + 1)
        if not 0 < len(content) <= 32 * 1024**2:
            raise ValueError("剧本文件内容为空或超过32MiB")
        extension = p.suffix.lower()
        if extension == ".txt":
            try:
                text = content.decode("utf-8-sig")
            except UnicodeError:
                raise ValueError("TXT须为UTF-8编码，不能静默替换乱码") from None
            content_type = "text/plain"
        elif extension == ".docx":
            try:
                with zipfile.ZipFile(BytesIO(content)) as z:
                    if sum(i.file_size for i in z.infolist()) > 64 * 1024**2:
                        raise ValueError("DOCX解压后超过64MiB，拒绝处理")
                    xml = z.read("word/document.xml")
                    if b"<!DOCTYPE" in xml.upper():
                        raise ValueError("DOCX不接受带DOCTYPE的XML")
                    root = ET.fromstring(xml)
                    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
                    paragraphs = []
                    for paragraph in root.iter(ns + "p"):
                        parts = []
                        for e in paragraph.iter():
                            if e.tag == ns + "t":
                                parts.append(e.text or "")
                            elif e.tag == ns + "tab":
                                parts.append("\t")
                            elif e.tag in {ns + "br", ns + "cr"}:
                                parts.append("\n")
                        paragraphs.append("".join(parts))
                    text = "\n".join(paragraphs)
            except (zipfile.BadZipFile, KeyError, ET.ParseError):
                raise ValueError("该文件不是有效DOCX文档，不能仅修改扩展名上传") from None
            content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        else:
            raise ValueError("官网当前剧本文件入口支持TXT或DOCX")
        check_text(text)
        return p, text, content_type, content

    def upload_file(self, path, operation_id):
        p, _, content_type, content = self._read_file(path)
        digest, size = hashlib.sha256(content).hexdigest(), len(content)
        payload = {"path": str(p), "sha256": digest, "bytes": size}
        def action(rid):
            data = self.client.request("POST", "/api/v1/file/presigned-url", {"purpose": "chat_attachment", "content_type": content_type, "filename": p.name})
            if not isinstance(data, dict) or str(data.get("method", "")).upper() != "PUT":
                raise APIError("剧本预签名上传未返回PUT契约")
            safe_url(data.get("upload_url")); safe_url(data.get("public_url"))
            headers = data.get("required_headers") or {}
            if not any(k.lower() == "content-type" for k in headers) or any(k.lower() in {"authorization", "cookie", "proxy-authorization"} for k in headers):
                raise ValueError("剧本预签名头缺少类型或要求不安全的凭证转发")
            response = self.client.media.put(data["upload_url"], content=content, headers={**headers, "Content-Length": str(size)})
            if not 200 <= response.status_code < 300:
                raise APIError("剧本文件PUT未确认成功")
            inspected = self.client.request("POST", "/api/v1/file/inspect", {"content_type": content_type, "url": data["public_url"]})
            if not inspected:
                raise APIError("官网未确认剧本文件检查成功，不能生成可导入回执", definite=True)
            return {"file_name": p.name, "file_url": data["public_url"], "content_type": content_type, "sha256": digest, "bytes": size, "inspected": True}
        return self.store.operation(operation_id, "script_upload_file", payload, action)

    def import_file(self, upload_operation_id, ratio, style, operation_id, planned_episode_count):
        with self.store.auth_context():
            original = self.store.get_operation(upload_operation_id)
            if original["kind"] != "script_upload_file" or original["status"] != "SUCCESS":
                raise ValueError("需使用本账号已核验的剧本文件上传回执")
            file = original["result"]
            path = original["payload"].get("path")
            if not path:
                raise ValueError("上传回执缺少本地源文件路径，不能从任意地址读取剧情")
            _, text, _, content = self._read_file(path)
            if hashlib.sha256(content).hexdigest() != file.get("sha256") or len(content) != file.get("bytes"):
                raise ValueError("上传后源文件已改变，SHA或大小不符，停止生成")
            return self.import_text(text, Path(file["file_name"]).stem, ratio, style, operation_id, planned_episode_count)

    def _write(self, project_id, operation_id, kind, payload, action):
        project_id = identifier(str(project_id))
        def run(rid):
            with self.store.resource_lock(canonical([self.store.account_key(), "script-project", project_id])):
                project = self._project(project_id)
                if project.get("readonly") or project.get("project_type") == "demo":
                    raise ValueError("该剧本项目为只读或官方演示，禁止写入")
                if project.get("active_tasks"):
                    raise ValueError("剧本项目有运行任务，等待其结束后再写入")
                result = action(project)
                return result if isinstance(result, dict) else {"project_id": project_id, "acknowledged": True, "data": result}
        return self.store.operation(operation_id, kind, {"project_id": project_id, **payload}, run)

    def update_outline(self, project_id, patch, expected_sha256, operation_id):
        if not patch or set(patch) - OUTLINE_FIELDS:
            raise ValueError("大纲只支持已核字段，不接受状态、权限或任意字段修改")
        def action(project):
            old = project.get("outline")
            if fingerprint(old) != expected_sha256:
                raise ValueError("大纲已改变，请重新读取后核对修改")
            if not isinstance(old, dict) or old.get("editable") is False:
                raise ValueError("当前大纲尚不可编辑")
            body = {key: copy.deepcopy(old.get(key, [] if key == "character_biographies" else "")) for key in OUTLINE_FIELDS}
            body.update(copy.deepcopy(patch))
            for key, value in body.items():
                if key != "character_biographies" and not isinstance(value, str):
                    raise ValueError("大纲文本字段必须是字符串")
            if not isinstance(body["character_biographies"], list):
                raise ValueError("人物小传须为列表")
            if any(not isinstance(v, dict) or not isinstance(v.get("name"), str) or not isinstance(v.get("bio"), str) for v in body["character_biographies"]):
                raise ValueError("人物小传每项须提供name和bio文本")
            body["character_biographies"] = [{"name": v["name"], "bio": v["bio"]} for v in body["character_biographies"]]
            body["update_character_biographies"] = "character_biographies" in patch
            return self.client.request("PATCH", ROOT + f"/projects/{project_id}/outline", body)
        return self._write(project_id, operation_id, "script_update_outline", {"patch": patch, "expected_sha256": expected_sha256}, action)

    def update_episodes(self, project_id, episodes, new_episodes, expected_sha256, operation_id):
        def action(project):
            old = project["episodes"]
            if fingerprint(old) != expected_sha256:
                raise ValueError("分集已改变，请重新读取并核对")
            changes = {}
            for e in episodes:
                if not isinstance(e, dict) or set(e) - {"episode_id", "title", "content"} or not e.get("episode_id"):
                    raise ValueError("分集修改只接受episode_id、title、content")
                key = str(e["episode_id"])
                self._episode(project, key)
                if key in changes:
                    raise ValueError("分集修改列表含重复ID")
                changes[key] = e
            output = []
            for e in old:
                updated = {**e, **changes.get(str(e["episode_id"]), {})}
                check_text(updated.get("content"), "分集正文")
                check_text(updated.get("title"), "分集标题", 200)
                output.append({"episode_id": str(e["episode_id"]), "title": updated["title"], "content": updated["content"]})
            numbers = {e["episode_no"] for e in old}
            for e in new_episodes:
                if set(e) != {"episode_no", "title", "content"} or isinstance(e["episode_no"], bool) or not isinstance(e["episode_no"], int) or not 1 <= e["episode_no"] <= 100 or e["episode_no"] in numbers:
                    raise ValueError("新增分集须有唯一有效episode_no、title、content")
                numbers.add(e["episode_no"])
                check_text(e["title"], "新增分集标题", 200); check_text(e["content"], "新增分集正文")
            if len(numbers) > 100:
                raise ValueError("分集不能超过100集")
            return self.client.request("PUT", ROOT + f"/projects/{project_id}/episodes", {"episodes": output, "new_episodes": new_episodes})
        return self._write(project_id, operation_id, "script_update_episodes", {"episodes": episodes, "new_episodes": new_episodes, "expected_sha256": expected_sha256}, action)

    def run_stage(self, project_id, stage, operation_id, episode_id=None, regenerate=False):
        if stage not in STAGES:
            raise ValueError("不支持该剧本阶段；不会调用图片、视频或合成接口")
        if (stage in {"generate_episode", "generate_storyboard"}) != (episode_id is not None):
            raise ValueError("仅生成指定分集或分镜时必须指定episode_id")
        def action(project):
            status = project.get("status")
            if stage == "confirm_outline" and status != "outline_confirming":
                raise ValueError("当前项目不处于待确认大纲阶段")
            failed_stage = None
            if status == "failed":
                failed_stage = "retry_outline" if not project.get("outline") else "retry_episodes" if not project["episodes"] else "extract_assets"
                if stage != failed_stage:
                    raise ValueError("通用失败的实际阶段不符，请按官网大纲和分集状态选择恢复阶段")
            if stage == "extract_assets" and status not in {"outline_ready", "asset_extract_credits_locked"} and failed_stage != stage:
                raise ValueError("提取角色场景前必须完成剧本正文并处于outline_ready")
            if stage == "retry_outline" and status not in {"outline_failed", "outline_credits_locked"} and failed_stage != stage:
                raise ValueError("当前大纲不是明确失败或额度受限状态，不能重试")
            if stage == "retry_episodes" and status not in {"episodes_failed", "episodes_credits_locked", "script_generation_failed"} and failed_stage != stage:
                raise ValueError("当前分集不是明确失败或额度受限状态，不能重试")
            if stage == "resume_episodes" and status != "script_generating":
                raise ValueError("当前项目不处于可恢复的分集生成阶段")
            if episode_id is not None:
                e = self._episode(project, episode_id)
                identifier(str(episode_id))
                if stage == "generate_episode":
                    if e.get("script_status") == "generating":
                        raise ValueError("该分集仍在运行")
                    if e.get("script_status") == "completed" and not regenerate:
                        raise ValueError("该分集已完成，再次生成需明确regenerate=true")
                    if any(p.get("episode_no", 0) < e.get("episode_no", 0) and p.get("script_status") != "completed" for p in project["episodes"]):
                        raise ValueError("必须先完成前面的分集正文")
                    path = ROOT + f"/episodes/{episode_id}/generate"
                else:
                    if e.get("script_status") != "completed":
                        raise ValueError("生成分镜前须完成该集正文")
                    board = self.client.request("GET", ROOT + f"/episodes/{episode_id}/storyboard/detail")
                    if board.get("active_tasks"):
                        raise ValueError("该集分镜仍有运行任务")
                    if board.get("segments") and not regenerate:
                        raise ValueError("已有分镜，再次生成需明确regenerate=true")
                    path = ROOT + f"/episodes/{episode_id}/storyboard/generate"
            else:
                path = ROOT + f"/projects/{project_id}" + STAGES[stage]
            resource = canonical([self.store.account_key(), "script-stage", str(project_id), stage, episode_id])
            owner = self.store.claim_owner(resource)
            if owner and owner != operation_id:
                previous = self.store.get_operation(owner)
                if previous["status"] != "SUCCESS":
                    raise OperationUnknown("该阶段存在结果未知的提交，禁止换号重投")
                if not regenerate:
                    raise ValueError("该阶段已有提交回执，再次执行需明确regenerate=true并核对项目状态")
            self.store.claim(resource, operation_id, owner)
            try:
                data = self.client.request("POST", path, {"is_manual": True} if stage == "finalize_assets" else None)
            except APIError as e:
                if e.definite:
                    self.store.release_claim(resource, operation_id, owner)
                raise
            if data is not None and not isinstance(data, dict):
                raise APIError("剧本阶段响应结构无法识别，提交结果未知")
            ids = task_ids(data)
            if stage == "generate_episode" and not ids:
                raise APIError("生成分集未返回原生task_id，结果未知")
            return {"project_id": str(project_id), "episode_id": episode_id, "stage": stage, "task_ids": ids,
                    "acknowledged": True, "data": data, "monitor_tool": "pavo_script_get_project"}
        return self._write(project_id, operation_id, "script_run_stage", {"stage": stage, "episode_id": episode_id, "regenerate": regenerate}, action)

    def export(self, project_id, output_dir, format="markdown", include_storyboards=False):
        if format not in {"json", "markdown", "doc"}:
            raise ValueError("导出格式须为json、markdown或doc；doc为与官网相同的Word兼容HTML，并非DOCX")
        project = self._project(project_id)
        snapshot = {"project": project, "storyboards": {}}
        if include_storyboards:
            for e in project["episodes"]:
                snapshot["storyboards"][str(e["episode_id"])] = self.get_storyboard(project_id, str(e["episode_id"]))
        digest = fingerprint(snapshot)
        if format == "json":
            content = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
        else:
            lines = ["# " + str(project.get("projectName") or "剧本"), "", "项目状态：" + str(project.get("status", "未知")), ""]
            outline = project.get("outline") or {}
            for field, label in (("story_type", "题材"), ("target_audience", "目标读者"), ("one_line_story", "一句话故事"), ("story_summary", "故事梗概"), ("world_setting", "世界设定")):
                if outline.get(field):lines.extend(["## " + label, "", str(outline[field]), ""])
            if outline.get("character_biographies"):
                lines.extend(["## 人物小传", ""])
                lines.extend(str(v.get("name", "")) + "：" + str(v.get("bio", "")) for v in outline["character_biographies"])
            for e in project["episodes"]:
                lines.extend(["", f"## 第{e.get('episode_no')}集：{e.get('title', '')}", "", str(e.get("content") or "（暂无已生成正文）"), ""])
                if include_storyboards:
                    lines.extend(["### 原生分镜数据", "", "```json", json.dumps(snapshot["storyboards"][str(e["episode_id"])], ensure_ascii=False, indent=2), "```", ""])
            content = "\n".join(lines)
            if format == "doc":
                content = '\ufeff<!DOCTYPE html><html><head><meta charset="utf-8"><title>剧本导出</title></head><body><pre style="white-space:pre-wrap;font-family:宋体">' + html.escape(content) + "</pre></body></html>"
        out = Path(output_dir).expanduser()
        if not out.is_absolute() or out.is_symlink() or ".." in out.parts:
            raise ValueError("导出目录须为明确的绝对路径，不能使用符号链接或路径穿越")
        out.mkdir(parents=True, exist_ok=True)
        extension = {"json": "json", "markdown": "md", "doc": "doc"}[format]
        target = out / f"pavo-script-{identifier(str(project_id))}-{digest[:12]}.{extension}"
        raw = content.encode("utf-8")
        file_hash = hashlib.sha256(raw).hexdigest()
        fd, temp = tempfile.mkstemp(dir=out, prefix=".pavo-script-")
        reused = False
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw); f.flush(); os.fsync(f.fileno())
            try:
                os.link(temp, target)
            except FileExistsError:
                if target.is_symlink() or not target.is_file() or sha256(target) != file_hash:
                    raise ValueError("同名导出文件存在且内容不同，拒绝覆盖") from None
                reused = True
        finally:
            os.unlink(temp)
        return {"path": str(target), "bytes": len(raw), "sha256": file_hash, "snapshot_sha256": digest, "format": format, "reused": reused,
                "status": project.get("status"), "project_readonly": bool(project.get("readonly")), "complete_generation_claimed": False}
