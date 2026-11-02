"""官网短剧制作分集及片段媒体；关键帧属于片段，不属于单镜。"""

import copy
import re

from .canvas import identifier
from .client import APIError
from .media import safe_url
from .script import ROOT, check_text, fingerprint
from .script_storyboard import ScriptStoryboard, entities
from .store import OperationUnknown, canonical

ROLES = {"image": "segment_keyframe", "video": "segment_video"}


def running(status):
    return isinstance(status, str) and (status in {"running", "generating", "processing"} or "waiting" in status)


def media_key(item):
    value = item.get("media_id") or item.get("generation_task_id")
    if value is None or isinstance(value, (bool, dict, list)) or not str(value):
        raise APIError("媒体历史缺少可核的media_id或generation_task_id")
    return str(value)


def persisted_generated_video(item):
    return item.get('task_status') is None and item.get('kind') == 'media' and bool(item.get('media_id')) and item.get('media_type') == 'video' and item.get('role') == ROLES['video'] and item.get('source') == 'ai_generate' and isinstance(item.get('url'), str) and item['url'].startswith('https://')


def prompt_content(text):
    if not isinstance(text, str):
        return text
    pattern = re.compile(r'^参考图(\d+)为(\[[^\]\n]+\][^\n]*)$', re.M)
    entries = pattern.findall(text)
    if not entries or len({number for number, _ in entries}) != len(entries):
        return text
    mapping = dict(entries)
    body = pattern.sub('', text).strip()
    if set(re.findall(r'@图片(\d+)', body)) - mapping.keys():
        return text
    # 官网可能同步重排参考编号；按实际指向比较，仍保护台词、切点及引用含义。
    body = re.sub(r'@图片(\d+)', lambda match: '@参考('+mapping[match[1]]+')', body)
    return {'references': sorted(mapping.values()), 'body': body}


def script_content(board):
    # 生成会改变媒体/任务状态，只比较文字、顺序、时长及完整场景镜头结构。
    keys = ("segment_id", "display_order", "title", "summary", "video_prompt_text", "keyframe_prompt_text", "estimated_duration_ms", "scenes")
    value = {"episode_id": board.get("episode_id"), "title": board.get("title"),
             "segments": [{k: copy.deepcopy(s[k]) for k in keys if k in s} for s in board["segments"]]}
    for segment in value['segments']:
        if 'video_prompt_text' in segment:
            segment['video_prompt_text'] = prompt_content(segment['video_prompt_text'])
    return value


def episode_content(project):
    return [{k: e.get(k) for k in ("episode_id", "episode_no", "title", "content")} for e in project["episodes"]]


class ScriptMedia:
    def __init__(self, script):
        self.script, self.client, self.store = script, script.client, script.store

    def list_episodes(self, project_id):
        project_id = identifier(str(project_id))
        data = self.script._storyboard_episodes(self.script._project(project_id, False))
        return {"project_id": project_id, "data": data, "sha256": fingerprint(data)}

    def list_models(self, scene_code):
        if scene_code not in ROLES.values():
            raise ValueError("短剧模型类别须为segment_keyframe或segment_video")
        data = self.client.request("GET", "/api/v1/pixa/model_options", params={"scene_code": scene_code})
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise APIError("官网短剧模型列表结构无法识别")
        return data

    def _state(self, project, episode_id, segment_id):
        segment_id = identifier(str(segment_id))
        board = ScriptStoryboard(self.script)._read(project, episode_id)
        segments = entities(board)[0]
        if segment_id not in segments:
            raise ValueError("片段不属于指定项目及制作分集")
        histories = {}
        for role in ROLES.values():
            data = self.client.request("GET", ROOT + f"/segments/{segment_id}/media", params={"role": role})
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise APIError("官网片段媒体历史结构无法识别")
            keys = [media_key(x) for x in data["items"]]
            if len(keys) != len(set(keys)):
                raise APIError("媒体历史ID不唯一，停止选择或生成")
            histories[role] = data
        return {"board": board, "media": histories}

    @staticmethod
    def _result(project_id, episode_id, segment_id, state):
        return {"project_id": str(project_id), "episode_id": str(episode_id), "segment_id": str(segment_id),
                "segment": entities(state["board"])[0][str(segment_id)], "media": state["media"],
                "_mcp_fingerprints": {"media": fingerprint(state)}}

    def get(self, project_id, episode_id, segment_id):
        return self._result(project_id, episode_id, segment_id, self._state(self.script._project(project_id, False), episode_id, segment_id))

    def get_video_task(self, project_id, episode_id, segment_id, task_id):
        task_id = identifier(str(task_id))
        data = self.get(project_id, episode_id, segment_id)
        matches = [x for x in data["media"][ROLES["video"]]["items"] if str(x.get("generation_task_id")) == task_id]
        if len(matches) != 1:
            return {"task_id": task_id, "found": False, "ambiguous": len(matches) > 1, "terminal": False,
                    "succeeded": None, "retry_safe": False, "reason": "原生历史暂缺或不唯一，不能据此重提"}
        item = matches[0]
        status = item.get("task_status")
        failed = status == "failed"
        persisted = persisted_generated_video(item)
        success = (status == "success" or persisted) and bool(item.get("url"))
        return {"task_id": task_id, "found": True, "native_status": status, "media": item,
                "terminal": failed or status == "success" or persisted, "succeeded": True if success else False if failed else None,
                "completion_evidence": "persisted_native_generated_video" if persisted else "task_status" if status in {"success", "failed"} else None,
                "retry_safe": failed, "running": running(status),
                "status_limit": "按原生success/failed、运行态或精确task_id对应的ai_generate持久化video资源判定；仅URL不算成功，未知状态和缺失产物不自动重提，不调用画布get_task"}

    def _before(self, project, episode_id, segment_id, expected_sha256):
        state = self._state(project, episode_id, segment_id)
        if fingerprint(state) != expected_sha256:
            raise ValueError("分镜或媒体在读取后改变，请回读最新media指纹")
        if state["board"].get("active_tasks") or any(running(x.get("task_status")) for h in state["media"].values() for x in h["items"]):
            raise ValueError("分集或片段媒体仍有运行任务，禁止覆盖或重提")
        return state

    @staticmethod
    def _https(url):
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ValueError("媒体须为已上传的HTTPS公网稳定地址")
        safe_url(url)
        return url

    def _verify(self, project, before, after, segment_id, role):
        if script_content(before["board"]) != script_content(after["board"]):
            raise APIError("媒体操作意外改变分镜正文、顺序或时长")
        old_segments, new_segments = entities(before["board"])[0], entities(after["board"])[0]
        for sid, segment in old_segments.items():
            for kind, prefix in ((ROLES["image"], "selected_keyframe_"), (ROLES["video"], "selected_video_")):
                if sid == str(segment_id) and kind == role:
                    continue
                for k, v in segment.items():
                    if k.startswith(prefix) and new_segments[sid].get(k) != v:
                        raise APIError("媒体操作改变其它片段或另一个媒体槽")
        for kind, history in before["media"].items():
            actual = {media_key(x): x for x in after["media"][kind]["items"]}
            for item in history["items"]:
                current = actual.get(media_key(item))
                if current is None or any(current.get(k) != item.get(k) for k in ("url", "media_type", "role", "generation_task_id")):
                    raise APIError("既有媒体历史被删除或替换")
        if episode_content(project) != episode_content(self.script._project(project["project_id"], True)):
            raise APIError("媒体操作意外改变剧本正文或分集标题")

    def _command(self, project, episode_id, segment_id, operation_id, action):
        resource = canonical([self.store.account_key(), "script-segment-media", str(project["project_id"]), str(episode_id), str(segment_id)])
        owner = self.store.claim_owner(resource)
        if owner and owner != operation_id and self.store.get_operation(owner)["status"] not in {"SUCCESS", "FAILED"}:
            raise OperationUnknown("该片段存在未知媒体提交，禁止换号重投；先核原回执与官网媒体历史")
        self.store.claim(resource, operation_id, owner)
        acknowledged = False

        def send(method, path, body=None):
            nonlocal acknowledged
            result = self.client.request(method, path, body)
            acknowledged = True
            return result

        try:
            return action(send)
        except Exception as e:
            if acknowledged:
                raise APIError("媒体请求已提交但后续回读未完整确认；保持UNKNOWN，禁止重提，先核原回执与媒体历史") from e
            if isinstance(e, (ValueError, APIError)) and (isinstance(e, ValueError) or e.definite):
                self.store.release_claim(resource, operation_id, owner)
            raise

    @staticmethod
    def _available(item):
        return bool(item.get("url")) and not running(item.get("task_status")) and item.get("task_status") != "failed"

    def _select(self, send, segment_id, item):
        if not item.get("media_id") or not self._available(item):
            raise ValueError("只能选择已存在且非运行、非失败的完整媒体，不以task_id代替media_id")
        send("PUT", ROOT + f"/segments/{segment_id}/media/{identifier(str(item['media_id']))}/select")

    @staticmethod
    def _selected(state, segment_id, media_type, item):
        prefix = "selected_keyframe_" if media_type == "image" else "selected_video_"
        segment = entities(state["board"])[0][str(segment_id)]
        if str(segment.get(prefix + "id")) != str(item["media_id"]) or segment.get(prefix + "url") != item["url"]:
            raise APIError("媒体已保存但目标槽位绑定未确认")

    def attach(self, project_id, episode_id, segment_id, media_type, url, expected_sha256, operation_id, thumbnail_url="", first_frame_url=""):
        if media_type not in ROLES:
            raise ValueError("片段媒体类型须为image或video")
        self._https(url)
        role = ROLES[media_type]
        body = {"media_type": media_type, "role": role, "url": url}
        if media_type == "image":
            body["thumbnail_url"] = self._https(thumbnail_url or url)
        else:
            body["first_frame_url"] = self._https(first_frame_url)
        payload = {"episode_id": str(episode_id), "segment_id": str(segment_id), "media": body, "expected_sha256": expected_sha256}

        def action(project):
            before = self._before(project, episode_id, segment_id, expected_sha256)
            def write(send):
                existing = [x for x in before["media"][role]["items"] if x.get("url") == url]
                if not existing:
                    send("POST", ROOT + f"/segments/{segment_id}/media/upload", body)
                    uploaded = self._state(project, episode_id, segment_id)
                    existing = [x for x in uploaded["media"][role]["items"] if x.get("url") == url]
                if len(existing) != 1:
                    raise APIError("上传回读未找到唯一目标媒体，停止重复上传")
                self._select(send, segment_id, existing[0])
                after = self._state(project, episode_id, segment_id)
                self._selected(after, segment_id, media_type, existing[0])
                self._verify(project, before, after, segment_id, role)
                return {**self._result(project_id, episode_id, segment_id, after), "media_id": str(existing[0]["media_id"]), "verified": True, "generation_submitted": False}
            return self._command(project, episode_id, segment_id, operation_id, write)
        return self.script._write(project_id, operation_id, "script_segment_attach_media", payload, action)

    def select(self, project_id, episode_id, segment_id, media_id, expected_sha256, operation_id):
        media_id = identifier(str(media_id))
        payload = {"episode_id": str(episode_id), "segment_id": str(segment_id), "media_id": media_id, "expected_sha256": expected_sha256}
        def action(project):
            before = self._before(project, episode_id, segment_id, expected_sha256)
            matches = [(kind, x) for kind, h in before["media"].items() for x in h["items"] if str(x.get("media_id")) == media_id]
            if len(matches) != 1:
                raise ValueError("media_id不属于本片段的唯一媒体记录")
            role, item = matches[0]
            media_type = next(k for k, v in ROLES.items() if v == role)
            if not self._available(item):
                raise ValueError("不能选择运行中或失败的媒体")
            def write(send):
                self._select(send, segment_id, item)
                after = self._state(project, episode_id, segment_id)
                self._selected(after, segment_id, media_type, item)
                self._verify(project, before, after, segment_id, role)
                return {**self._result(project_id, episode_id, segment_id, after), "verified": True, "generation_submitted": False}
            return self._command(project, episode_id, segment_id, operation_id, write)
        return self.script._write(project_id, operation_id, "script_segment_select_media", payload, action)

    def submit_video(self, project_id, episode_id, segment_id, model_code, resolution, expected_ratio, style, expected_sha256, operation_id, regenerate=False, reference_image_url=None):
        if not isinstance(model_code, str) or not model_code.strip() or not isinstance(resolution, str) or not resolution:
            raise ValueError("必须明确model_code与resolution，不猜模型默认值")
        if reference_image_url is not None:
            self._https(reference_image_url)
        payload = {"episode_id": str(episode_id), "segment_id": str(segment_id), "model_code": model_code, "resolution": resolution,
                   "expected_ratio": expected_ratio, "style": style, "expected_sha256": expected_sha256,
                   "regenerate": regenerate, "reference_image_url": reference_image_url}
        def action(project):
            if project.get("ratio") != expected_ratio:
                raise ValueError("项目实际ratio与expected_ratio不符；原生视频请求没有独立ratio字段")
            self.script._style(expected_ratio, style)
            before = self._before(project, episode_id, segment_id, expected_sha256)
            segment = entities(before["board"])[0][str(segment_id)]
            check_text(segment.get("video_prompt_text"), "片段派生视频提示词")
            if not reference_image_url:
                anchors = [x for x in before["media"][ROLES["image"]]["items"] if str(x.get("media_id")) == str(segment.get("selected_keyframe_id")) and x.get("url") == segment.get("selected_keyframe_url") and self._available(x)]
                if len(anchors) != 1:
                    raise ValueError("须先关联并选择可核的片段关键帧，或明确提供reference_image_url")
            videos = before["media"][ROLES["video"]]["items"]
            if videos and not regenerate:
                raise ValueError("已有视频历史，再次生成须明确regenerate=true并核原任务终态")
            if any(x.get("generation_task_id") is not None and x.get("task_status") not in {"success", "failed"} and not persisted_generated_video(x) for x in videos):
                raise ValueError("旧视频状态未知，不能重提")
            model = next((x for x in self.list_models(ROLES["video"])["items"] if x.get("model_code") == model_code), None)
            if not model or model.get("allowed") is not True or model.get("is_online") is False:
                raise ValueError("所选模型不在账号实时可用短剧视频列表")
            constraints = model.get("constraints") or {}
            if not {"first_frame", "reference_image"} & set(constraints.get("mode_types") or []):
                raise ValueError("实时模型未确认支持关键帧图片输入，不能按single_frame提交")
            if expected_ratio not in constraints.get("aspect_ratios", []) or resolution not in constraints.get("resolutions", []):
                raise ValueError("实际比例或分辨率不符合实时模型约束")
            shots = [s for c in segment.get("scenes", []) for s in c.get("shots", [])]
            if any(isinstance(s.get("duration_sec"), bool) or not isinstance(s.get("duration_sec"), int) or not 1 <= s["duration_sec"] <= 15 for s in shots):
                raise ValueError("单镜时长须为1至15秒整数，请先核分镜")
            duration = sum(s["duration_sec"] for s in shots)
            durations = constraints.get("supported_duration_seconds")
            if not duration or segment.get("estimated_duration_ms") != duration * 1000:
                raise ValueError("片段镜头总时长与estimated_duration_ms不一致")
            if durations is not None:
                valid = duration in durations
            else:
                valid = constraints.get("min_duration_seconds", float("inf")) <= duration <= constraints.get("max_duration_seconds", -1)
            if not valid:
                raise ValueError("片段总时长超出实时模型范围，应先按剧情拆段，不截断对白")
            body = {"model_code": model_code, "resolution": resolution, "keyframe_production_mode": "single_frame", **style}
            if reference_image_url is not None:
                body["upload_reference_image_url"] = reference_image_url
            old_tasks = {str(x.get("generation_task_id")) for x in videos if x.get("generation_task_id") is not None}
            def write(send):
                response = send("POST", ROOT + f"/segments/{segment_id}/video/generate", body)
                if not isinstance(response, dict):
                    raise APIError("视频提交回执结构无法识别")
                after = self._state(project, episode_id, segment_id)
                candidates = [x for x in after["media"][ROLES["video"]]["items"] if x.get("generation_task_id") is not None and str(x["generation_task_id"]) not in old_tasks]
                returned = response.get("task_id") or response.get("generation_task_id")
                if returned is not None:
                    candidates = [x for x in candidates if str(x["generation_task_id"]) == str(returned)]
                if response.get("status") == "already_running" or len(candidates) != 1:
                    raise APIError("提交后未核到唯一新任务，保持UNKNOWN，不换号重投")
                self._verify(project, before, after, segment_id, ROLES["video"])
                return {**self._result(project_id, episode_id, segment_id, after), "task_id": str(candidates[0]["generation_task_id"]),
                        "response": response, "generation_submitted": True, "monitor_tool": "pavo_script_get_segment_video_task",
                        "duration_sec": duration, "ratio": expected_ratio, "model_code": model_code, "resolution": resolution}
            return self._command(project, episode_id, segment_id, operation_id, write)
        return self.script._write(project_id, operation_id, "script_segment_submit_video", payload, action)
