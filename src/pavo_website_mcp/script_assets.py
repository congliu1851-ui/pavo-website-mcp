"""剧本角色、场景、道具及其状态；只保存资料，不调用媒体生成接口。"""

import copy

from .canvas import identifier
from .client import APIError
from .media import safe_url
from .script import ROOT, check_text, fingerprint
from .store import OperationUnknown, canonical

KINDS = {"character": ("characters", "character_id", "name"), "scene": ("scenes", "scene_id", "title"), "prop": ("props", "prop_id", "title")}
STATE_FIELDS = {"state_id", "parent_id", "state_name", "sort_order", "is_default", "has_art_versions", "voice_mode", "engine_voice_id",
    "voice_upload_url", "description", "voice_description", "model_code", "main_image", "main_image_status", "main_image_trace_id",
    "three_view_image", "three_view_image_status", "three_view_image_trace_id", "origin_source", "source_master_state_id",
    "reference_images", "episode_nos", "episode_binding_editable", "aspect_ratio"}
EDIT_FIELDS = {"state_name", "description", "episode_nos", "reference_images"}


class ScriptAssets:
    def __init__(self, script):
        self.script, self.client, self.store = script, script.client, script.store

    @staticmethod
    def kind(kind):
        if kind not in KINDS:
            raise ValueError("asset_type须为character、scene或prop")
        return KINDS[kind]

    def get(self, project_id):
        data = self.script.get_assets(project_id)
        if not isinstance(data, dict) or any(not isinstance(data.get(v[0]), list) for v in KINDS.values()):
            raise APIError("官网角色场景道具结构无法识别")
        hashes = {}
        for plural, key, _ in KINDS.values():
            hashes[plural] = {str(a[key]): {"asset": fingerprint(a), "states": {str(s["state_id"]): fingerprint(s) for s in a.get("states", [])}} for a in data[plural]}
        return {**data, "_mcp_fingerprints": hashes}

    def _asset(self, project_id, kind, asset_id):
        plural, key, _ = self.kind(kind)
        identifier(str(asset_id))
        data = self.client.request("GET", ROOT + f"/projects/{project_id}/assets")
        for a in data.get(plural, []):
            if str(a.get(key)) == str(asset_id):
                return a
        raise ValueError("该类型资产不属于指定项目，禁止跨项目或跨类型修改")

    def _command(self, resource, operation_id, method, path, body, normalize):
        owner = self.store.claim_owner(resource)
        if owner and owner != operation_id and self.store.get_operation(owner)["status"] not in {"SUCCESS", "FAILED"}:
            raise OperationUnknown("相同资产资料存在未知提交，不能换操作号重复创建或保存")
        self.store.claim(resource, operation_id, owner)
        try:
            data = self.client.request(method, path, body)
        except APIError as e:
            if e.definite:
                self.store.release_claim(resource, operation_id, owner)
            raise
        return normalize(data)

    @staticmethod
    def _state_body(state, project):
        body = {k: copy.deepcopy(v) for k, v in state.items() if k in STATE_FIELDS}
        style = state.get("style") or {k: state[k] for k in ("style_id", "custom_style_prompt") if state.get(k)} or project.get("style") or {}
        if style.get("custom_style_prompt"):
            body["custom_style_prompt"] = style["custom_style_prompt"]
        elif style.get("style_id"):
            body["style_id"] = str(style["style_id"])
        return body

    @staticmethod
    def _response_id(value):
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError
            return identifier(str(value))
        except ValueError:
            raise APIError("官网资产回执ID格式异常，结果未知；先回读资料库") from None

    @classmethod
    def _response_state_id(cls, data, kind, state_name, expected=None):
        raw = data if isinstance(data, dict) else {}
        entity = raw.get(kind) or raw
        record = entity.get("state") or raw.get("state") or {}
        name = str(record.get("state_name") or "").strip()
        actual = record.get("state_id") if not name or name == state_name.strip() else None
        if actual is None:
            states = entity.get("states") or []
            matches = [s for s in states if expected is not None and str(s.get("state_id")) == str(expected)]
            if not matches:
                matches = [s for s in states if str(s.get("state_name", "")).strip() == state_name.strip()]
            if not matches and len(states) == 1 and (expected is not None or not states[0].get("state_name")):
                matches = states
            if len(matches) == 1:
                actual = matches[0].get("state_id")
        if actual is None:
            for candidate in (entity, raw):
                name = str(candidate.get("state_name") or "").strip()
                if expected is not None or not name or name == state_name.strip():
                    actual = candidate.get("state_id")
                    if actual is not None:
                        break
        if actual is None:
            if expected is not None:
                return str(expected)  # 官网对已有状态允许空data确认；输入ID已核归属。
            raise APIError("保存状态未返回可确认的原生状态ID，结果未知；先回读资产资料库")
        actual = cls._response_id(actual)
        if expected is not None and actual != str(expected):
            raise APIError("官网状态回执ID与请求不符，结果未知；先回读资产资料库")
        return actual

    @staticmethod
    def _validate_patch(patch, project):
        if not isinstance(patch, dict) or not patch or set(patch) - EDIT_FIELDS:
            raise ValueError("状态只允许state_name、description、episode_nos、reference_images，不允许改生成状态或任意字段")
        if "state_name" in patch:
            check_text(patch["state_name"], "状态名称", 200)
        if "description" in patch:
            value = patch["description"]
            if not isinstance(value, str):
                raise ValueError("资产描述必须是文字")
            if value:
                check_text(value, "资产描述")
        if "episode_nos" in patch:
            numbers = patch["episode_nos"]
            available = {e["episode_no"] for e in project["episodes"]}
            if not isinstance(numbers, list) or any(isinstance(n, bool) or not isinstance(n, int) or n not in available for n in numbers) or len(set(numbers)) != len(numbers):
                raise ValueError("分集绑定必须为本项目现有且不重复的集号列表")
        if "reference_images" in patch:
            urls = patch["reference_images"]
            if not isinstance(urls, list):
                raise ValueError("参考图必须是稳定地址列表")
            for url in urls:
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise ValueError("参考图须为HTTPS公网稳定地址")
                safe_url(url)

    def create(self, project_id, asset_type, name, operation_id, description="", state_name="默认"):
        plural, key, label = self.kind(asset_type)
        check_text(name, "资产名称", 200)
        patch = {"state_name": state_name, "description": description}
        payload = {"asset_type": asset_type, "name": name, **patch}
        def action(project):
            self._validate_patch(patch, project)
            state = {"parent_id": "", "state_name": state_name, "description": description, "sort_order": 0,
                     "is_default": True, "reference_images": [], "episode_nos": [], "episode_binding_editable": True}
            if asset_type == "character":
                state.update(voice_mode="ai_auto", engine_voice_id="")
            body = {label: name, **self._state_body(state, project)}
            resource = canonical([self.store.account_key(), "script-asset-create", str(project_id), fingerprint(payload)])
            def normalize(data):
                entity = data.get(asset_type) if isinstance(data, dict) else None
                if not isinstance(entity, dict) or not entity.get(key):
                    raise APIError("创建资产未返回原生资产ID，结果未知；请先回读资料库")
                asset_id = self._response_id(entity[key])
                return {"project_id": str(project_id), "asset_type": asset_type, "asset_id": asset_id,
                        "state_id": self._response_state_id(data, asset_type, state_name), "data": data}
            return self._command(resource, operation_id, "POST", ROOT + f"/projects/{project_id}/assets/{plural}", body, normalize)
        return self.script._write(project_id, operation_id, "script_asset_create", payload, action)

    def rename(self, project_id, asset_type, asset_id, name, expected_sha256, operation_id):
        plural, _, label = self.kind(asset_type)
        check_text(name, "资产名称", 200)
        def action(project):
            asset = self._asset(project_id, asset_type, asset_id)
            if fingerprint(asset) != expected_sha256:
                raise ValueError("资产已改变，请回读最新资料和指纹后修改")
            return self.client.request("PATCH", ROOT + f"/projects/{project_id}/assets/{plural}/{asset_id}", {label: name})
        return self.script._write(project_id, operation_id, "script_asset_rename", {"asset_type": asset_type, "asset_id": asset_id, "name": name, "expected_sha256": expected_sha256}, action)

    def save_state(self, project_id, asset_type, asset_id, patch, operation_id, state_id=None, expected_sha256=None):
        plural, _, _ = self.kind(asset_type)
        if state_id is None and expected_sha256 is not None:
            raise ValueError("新增状态不能提供已有状态指纹，请省略expected_sha256")
        payload = {"asset_type": asset_type, "asset_id": asset_id, "patch": patch, "state_id": state_id, "expected_sha256": expected_sha256}
        def action(project):
            asset = self._asset(project_id, asset_type, asset_id)
            self._validate_patch(patch, project)
            if state_id is not None:
                identifier(str(state_id))
                state = next((s for s in asset.get("states", []) if str(s.get("state_id")) == str(state_id)), None)
                if state is None:
                    raise ValueError("该状态不属于指定资产，不能跨资产编辑")
                if fingerprint(state) != expected_sha256:
                    raise ValueError("状态已改变，请回读最新状态指纹")
                if "episode_nos" in patch and state.get("episode_binding_editable") is False:
                    raise ValueError("该状态的分集绑定不可编辑")
                merged = {**state, **patch, "parent_id": str(asset_id), "state_id": str(state_id)}
            else:
                if not patch.get("state_name") or "description" not in patch:
                    raise ValueError("新增状态须明确state_name和description")
                merged = {"parent_id": str(asset_id), "sort_order": len(asset.get("states", [])), "is_default": not bool(asset.get("states")),
                          "reference_images": [], "episode_nos": [], "episode_binding_editable": True, **patch}
            body = self._state_body(merged, project)
            if asset.get("origin_source") == "extracted" and not merged.get("episode_binding_editable", True):
                body.pop("episode_nos", None)
            resource = canonical([self.store.account_key(), "script-asset-state", str(project_id), fingerprint(payload)])
            def normalize(data):
                actual = self._response_state_id(data, asset_type, merged["state_name"], state_id)
                return {"project_id": str(project_id), "asset_type": asset_type, "asset_id": str(asset_id), "state_id": actual, "data": data}
            return self._command(resource, operation_id, "POST", ROOT + f"/projects/{project_id}/assets/{plural}/{asset_id}/states", body, normalize)
        return self.script._write(project_id, operation_id, "script_asset_save_state", payload, action)
