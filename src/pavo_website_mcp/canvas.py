"""官网画布协议、实时模型约束和可预览的批量变更。"""

import base64
import copy
import hashlib
import json
import math
import re
import time
import uuid
from urllib.parse import urlencode

from .client import APIError
from .store import canonical

PREFIX = {"text": "t", "image": "i", "video": "v", "audio": "a", "group": "g"}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("项目、画布、节点和模型标识必须是有效的原生标识")
    return value


def decode_data(data):
    if isinstance(data, str):
        data = json.loads(data)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("节点data必须为JSON对象或对象字符串")
    return copy.deepcopy(data)


def merge(target, patch):
    result = copy.deepcopy(target)
    for key, value in patch.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def set_prompt(data, kind, text):
    if not isinstance(text, str):
        raise ValueError("提示词必须是文字")
    params = data.setdefault("params", {})
    old = params.get("prompt") or []
    if not isinstance(old, list):
        raise ValueError("现有提示词结构无法识别，不能覆盖")
    segments, inserted = [], False
    for part in old:
        if not isinstance(part, dict):
            raise ValueError("现有提示词片段无法识别，不能覆盖")
        if part.get("type") == "text":
            if not inserted:
                segments.append({"type": "text", "content": text})
                inserted = True
        else:
            segments.append(part)
    if not inserted:
        segments.insert(0, {"type": "text", "content": text})
    params["prompt"] = segments
    if kind == "text":
        data.update(content=text, mode="authoring")
        params.setdefault("modeCode", "text_common")


def stable_key(seed, kind, media_type="image"):
    prefix = PREFIX.get(media_type if kind == "upload" else kind)
    if not prefix:
        raise ValueError("首版支持text、image、video、audio、upload及group节点")
    return prefix + "-" + base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest()).decode()[:12]


def write_node(node):
    p, m = node.get("position", {}), node.get("measured", {})
    data = decode_data(node.get("data"))
    return {"nodeKey": node["node_key"], "type": str(node["node_type"]), "name": node.get("name", ""),
            "position": {"positionX": str(p.get("position_x", 100)), "positionY": str(p.get("position_y", 80))},
            "measured": {"width": str(m.get("width", 280)), "height": str(m.get("height", 280))},
            "parentKey": node.get("parent_key", ""), "data": canonical(data)}


def project_url(detail, project_uuid, canvas_uuid):
    pid = detail.get("current_canvas", detail.get("canvas", {})).get("project_id") or detail.get("project_meta", detail.get("project", {})).get("id")
    if pid is None or not re.fullmatch(r"\d+", str(pid)):
        return None
    return f"https://app.pavo-ai.cn/canvas/{pid}?" + urlencode({"project_uuid": project_uuid, "canvas_uuid": canvas_uuid})


def check_graph(nodes, connections):
    keys = [n["node_key"] for n in nodes]
    if len(set(keys)) != len(keys):
        raise ValueError("画布存在重复node_key")
    incoming, graph = {k: 0 for k in keys}, {k: [] for k in keys}
    for e in connections:
        s, t = e.get("source", e.get("source_node_key")), e.get("target", e.get("target_node_key"))
        if s not in graph or t not in graph:
            raise ValueError("连线引用了不存在的节点")
        graph[s].append(t)
        incoming[t] += 1
    stack = [k for k, count in incoming.items() if count == 0]
    visited = 0
    while stack:
        k = stack.pop()
        visited += 1
        for t in graph[k]:
            incoming[t] -= 1
            if incoming[t] == 0:
                stack.append(t)
    if visited != len(keys):
        raise ValueError("参考依赖形成循环")


def changed_paths(before, after, prefix=()):
    if isinstance(before, dict) and isinstance(after, dict):
        for key in before.keys() | after.keys():
            yield from changed_paths(before.get(key), after.get(key), (*prefix, key))
    elif before != after:
        yield prefix


def get_path(value, path):
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


class Pavo:
    def __init__(self, client, store):
        self.client, self.store = client, store

    def list_projects(self):
        return self.client.request("GET", "/api/v1/pixa/canvas/folder/entries")

    def create_project(self, title, operation_id, cover_url=""):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("项目标题不能为空")
        payload = {"title": title.strip(), "cover_url": cover_url}
        def action(rid):
            result = self.client.request("POST", "/api/v1/pixa/canvas/project/create", payload)
            if not result or not result.get("project_uuid") or not result.get("canvas_uuid"):
                raise APIError("创建项目回执不完整，需要核对官网")
            return {**result, "canvas_url": project_url(result, result["project_uuid"], result["canvas_uuid"])}
        return self.store.operation(operation_id, "create_project", payload, action)

    def get_project(self, project_uuid, canvas_uuid):
        identifier(project_uuid), identifier(canvas_uuid)
        result = self.client.request("GET", "/api/v1/pixa/canvas/project/detail", params={"uuid": project_uuid, "canvas_uuid": canvas_uuid})
        if not isinstance(result, dict) or "version" not in result or not isinstance(result.get("node_list"), list) or not isinstance(result.get("connection_list"), list):
            raise APIError("画布详情结构与已核协议不符，停止写入")
        if result.get("current_canvas", {}).get("canvas_uuid") != canvas_uuid:
            raise ValueError("官网返回的当前画布与指定canvas_uuid不同，停止写入")
        return {**result, "canvas_url": project_url(result, project_uuid, canvas_uuid)}

    def list_nodes(self, project_uuid, canvas_uuid):
        return self.get_project(project_uuid, canvas_uuid)["node_list"]

    @staticmethod
    def find_node(detail, node_key):
        for n in detail["node_list"]:
            if n["node_key"] == node_key:
                return n
        raise ValueError("找不到指定node_key，请先读取画布节点")

    def get_node(self, project_uuid, canvas_uuid, node_key):
        n = self.find_node(self.get_project(project_uuid, canvas_uuid), node_key)
        return {**n, "data": decode_data(n.get("data"))}

    def list_connections(self, project_uuid, canvas_uuid):
        return self.get_project(project_uuid, canvas_uuid)["connection_list"]

    def list_models(self, scene_code):
        if scene_code not in {"canvas_image", "canvas_video", "canvas_audio"}:
            raise ValueError("scene_code须为canvas_image、canvas_video或canvas_audio；文字模型请读取tool-specs")
        data = self.client.request("GET", "/api/v1/pixa/model_options", params={"scene_code": scene_code})
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise APIError("实时模型列表结构无法识别")
        return data

    def get_model(self, scene_code, model_code):
        for model in self.list_models(scene_code)["items"]:
            if model.get("model_code") == model_code:
                return model
        raise ValueError("账号实时模型列表中不存在该model_code")

    def get_tool_specs(self, code=None):
        specs = self.client.request("GET", "/api/v1/pixa/canvas/tool-specs")
        if not isinstance(specs, dict):
            raise APIError("模板定义结构无法识别")
        if code is None:
            return specs
        matches = []
        for kind, scenes in specs.get("nodeConfigs", {}).items():
            if not isinstance(scenes, dict):
                continue
            for values in scenes.values():
                if isinstance(values, list):
                    matches.extend({**v, "kind": kind, "tool_spec_version": specs.get("version")} for v in values if v.get("code") == code)
        if len(matches) != 1:
            raise ValueError("实时模板code不存在或不唯一")
        return matches[0]

    def configure_model(self, data, kind, model_code):
        if kind == "text":
            models = self.get_tool_specs().get("textModels", [])
            model = next((m for m in models if m.get("modelCode") == model_code), None)
            if not model or model.get("isOnline") is False or model.get("allowed") is False:
                raise ValueError("实时文字模型不可用")
            data.setdefault("params", {})["model"] = model_code
            return model
        model = self.get_model("canvas_" + kind, model_code)
        if model.get("allowed") is False or model.get("is_online") is False:
            raise ValueError("当前账号无法使用该模型或模型未上线")
        constraints = model.get("constraints")
        if not isinstance(constraints, dict):
            raise ValueError("模型没有可识别的实时约束，停止配置")
        params = data.setdefault("params", {})
        params["model"] = model_code
        settings = params.setdefault("settings", {})
        for key, constraint in (("ratio", "aspect_ratios"), ("resolution", "resolutions")):
            options = constraints.get(constraint) or []
            if options:
                if key in settings and settings[key] not in options:
                    raise ValueError(f"settings.{key} 不满足实时模型约束")
                settings.setdefault(key, options[0])
        if settings.get("generateAudio") and not constraints.get("supports_audio_generation"):
            raise ValueError("该模型不支持generateAudio")
        settings.setdefault("generateAudio", False)
        modes = constraints.get("mode_types") or []
        if modes:
            if params.get("mode") and params["mode"] not in modes:
                raise ValueError("params.mode不满足实时模型输入模式")
            current = params.get("modeType", modes)
            current = [current] if isinstance(current, str) else current
            if not isinstance(current, list) or not current or any(m not in modes for m in current):
                raise ValueError("modeType不满足实时模型约束")
            params["modeType"] = current
        params.setdefault("power", 0)
        if kind == "image":
            count = params.setdefault("count", 1)
            if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= (constraints.get("max_batch_images") or 1):
                raise ValueError("count不满足实时模型约束")
        if kind == "video":
            durations = constraints.get("supported_duration_seconds") or ([constraints["min_duration_seconds"]] if constraints.get("min_duration_seconds") else [])
            if durations:
                if params.get("duration", durations[0]) not in durations:
                    raise ValueError("duration不满足实时模型约束")
                params.setdefault("duration", durations[0])
        return model

    def _validate_references(self, detail, node, model):
        kind = str(node["node_type"])
        spec = model.get("capability_spec", {}).get(kind)
        if spec is None:
            return
        if not isinstance(spec, dict):
            raise ValueError("模型媒体输入契约无法识别")
        counts = {}
        for edge in detail["connection_list"]:
            if edge.get("target", edge.get("target_node_key")) != node["node_key"]:
                continue
            source = self.find_node(detail, edge.get("source", edge.get("source_node_key")))
            media = str(source["node_type"])
            if media == "text":
                continue
            if media == "upload":
                media = decode_data(source["data"]).get("mediaType")
            rule = spec.get("input_" + str(media))
            if not isinstance(rule, dict):
                raise ValueError(f"模型不支持{media}类型的参考输入")
            counts[media] = counts.get(media, 0) + 1
            maximum = rule.get("max_count")
            if isinstance(maximum, int) and maximum >= 0 and counts[media] > maximum:
                raise ValueError(f"{media}参考输入数量超过模型上限{maximum}")
        maximum = spec.get("max_total_count")
        if isinstance(maximum, int) and maximum >= 0 and sum(counts.values()) > maximum:
            raise ValueError(f"参考输入总数超过模型上限{maximum}")

    def create_node(self, project_uuid, canvas_uuid, node_type, operation_id, name="", prompt=None, model=None, params=None, media_type="image", url=None, x=None, y=None, dry_run=False):
        op = {"op": "create", "type": node_type, "name": name, "prompt": prompt, "model": model,
              "params": params or {}, "media_type": media_type, "url": url, "x": x, "y": y}
        return self.apply_canvas(project_uuid, canvas_uuid, [op], operation_id, dry_run)

    def update_node(self, project_uuid, canvas_uuid, node_key, operation_id, name=None, prompt=None, model=None, params=None, data_patch=None, dry_run=False, expected_data=None):
        op = {"op": "update", "node_key": node_key, "name": name, "prompt": prompt, "model": model,
              "params": params or {}, "data": data_patch or {}, "expected_data": expected_data}
        return self.apply_canvas(project_uuid, canvas_uuid, [op], operation_id, dry_run)

    def connect_references(self, project_uuid, canvas_uuid, target_node_key, references, operation_id, dry_run=False):
        if not references:
            raise ValueError("参考输入不能为空")
        ops = []
        for i, ref in enumerate(references):
            if ref.get("role", "reference") not in {"reference", "prompt"}:
                raise ValueError("首尾帧角色需要官网实时模板，请使用pavo_apply_template并按真实输入key绑定")
            ops.append({**ref, "op": "connect", "target": target_node_key, "media_order": ref.get("media_order", i)})
        return self.apply_canvas(project_uuid, canvas_uuid, ops, operation_id, dry_run)

    def _plan(self, detail, operations, seed):
        original = {n["node_key"]: n for n in detail["node_list"]}
        nodes = copy.deepcopy(original)
        edges = copy.deepcopy(detail["connection_list"])
        creates, updates, added_edges, aliases = [], set(), [], {}
        def resolve(ref):
            key = aliases.get(ref[1:]) if isinstance(ref, str) and ref.startswith("$") else ref
            if key not in nodes:
                raise ValueError("节点引用不存在；别名必须先创建再引用")
            return key
        for index, op in enumerate(operations):
            action = op.get("op")
            if action in {"create", "update"}:
                if action == "create":
                    kind, media = op.get("type"), op.get("media_type", "image")
                    key = stable_key(f"{seed}:{index}", kind, media)
                    if key in nodes:
                        raise ValueError("待创建节点已存在，需要核对历史操作")
                    name = op.get("name") or {"text": "文字", "image": "图片", "video": "视频", "audio": "音频", "upload": "上传素材", "group": "分组"}.get(kind)
                    x = op.get("x") if op.get("x") is not None else 100 + 350 * len(nodes)
                    y = op.get("y") if op.get("y") is not None else 80
                    if any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in (x, y)):
                        raise ValueError("节点坐标必须是有限数字")
                    data = {"node_key": key, "title": name, "name": name, "isExecutable": kind in {"text", "image", "video", "audio"}, "progress": -1, "task_id": "-1"}
                    if kind == "text":
                        data.update(content="", mode="default", params={"modeCode": "text_common"})
                    if kind == "upload":
                        if media not in {"image", "video", "audio"}:
                            raise ValueError("上传节点媒体类型无效")
                        data["mediaType"] = media
                        if op.get("url"):
                            if not isinstance(op["url"], str) or not op["url"].startswith("https://"):
                                raise ValueError("素材节点url须为HTTPS稳定地址")
                            data["url"] = [op["url"]]
                    node = {"node_key": key, "node_type": kind, "name": name, "data": data,
                            "position": {"position_x": str(x), "position_y": str(y)}, "measured": {"width": "280", "height": "280"}}
                    nodes[key] = node
                    creates.append(key)
                    if op.get("as"):
                        if op["as"] in aliases:
                            raise ValueError("节点别名重复")
                        aliases[op["as"]] = key
                else:
                    key = resolve(op.get("node_key"))
                    node = nodes[key]
                    kind, data = str(node["node_type"]), decode_data(node["data"])
                    if op.get("expected_data") is not None and data != op["expected_data"]:
                        raise ValueError("节点在核验后已改变，停止回写并返回冲突")
                    updates.add(key)
                data = merge(data, op.get("data") or {})
                data["node_key"] = key
                if op.get("name") is not None:
                    if op["name"]:
                        data.update(title=op["name"], name=op["name"])
                        node["name"] = op["name"]
                data["params"] = merge(data.get("params", {}), op.get("params") or {})
                if op.get("prompt") is not None:
                    set_prompt(data, kind, op["prompt"])
                model = op.get("model") or data.get("params", {}).get("model")
                if model and kind in {"text", "image", "video", "audio"}:
                    self.configure_model(data, kind, model)
                node["data"] = data
            elif action == "connect":
                s, t = resolve(op.get("source")), resolve(op.get("target"))
                if any(e.get("source", e.get("source_node_key")) == s and e.get("target", e.get("target_node_key")) == t for e in edges):
                    raise ValueError("参考连线已存在，不能重复创建")
                order = op.get("media_order", 0)
                if not isinstance(order, int) or isinstance(order, bool) or order < 0:
                    raise ValueError("media_order须为非负整数")
                edge = {"connection_id": "e-" + hashlib.sha256(f"{seed}:edge:{index}".encode()).hexdigest()[:16],
                        "source": s, "target": t, "source_handle": op.get("source_handle", "source"),
                        "target_handle": op.get("target_handle", "target"), "source_port_type": op.get("source_port_type", ""),
                        "target_port_type": op.get("target_port_type", ""), "role": op.get("role", "reference"),
                        "media_order": order, "connection_type": op.get("connection_type", "default"), "color_key": op.get("color_key", ""),
                        "selectable": True, "deletable": True}
                added_edges.append(edge)
                edges.append(edge)
                for key, field, value in ((s, "target", t), (t, "source", s)):
                    d = decode_data(nodes[key]["data"])
                    current = d.get(field, [])
                    if not isinstance(current, list):
                        raise ValueError("现有节点参考派生字段无法识别")
                    d[field] = list(dict.fromkeys([*current, value]))
                    nodes[key]["data"] = d
                    updates.add(key)
            else:
                raise ValueError("批量操作仅支持create、update和connect")
        check_graph(list(nodes.values()), edges)
        changes = {}
        for key in updates - set(creates):
            old = {**original[key], "data": decode_data(original[key]["data"])}
            new = {**nodes[key], "data": decode_data(nodes[key]["data"])}
            changes[key] = [(path, get_path(old, path)) for path in changed_paths(old, new)]
        return {"nodes": {"create": [write_node(nodes[k]) for k in creates], "update": [write_node(nodes[k]) for k in sorted(updates - set(creates))], "delete": []},
                "connections": {"create": added_edges, "delete": []}, "node_keys": creates, "aliases": aliases}, changes

    def apply_canvas(self, project_uuid, canvas_uuid, operations, operation_id, dry_run=False):
        identifier(project_uuid), identifier(canvas_uuid)
        if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
            raise ValueError("每批operations须为1至100项")
        payload = {"project_uuid": project_uuid, "canvas_uuid": canvas_uuid, "operations": operations}
        def execute(rid, preview=False):
            detail = self.get_project(project_uuid, canvas_uuid)
            plan, touched = self._plan(detail, operations, operation_id)
            if preview:
                return {"dry_run": True, "valid": True, "version": int(detail["version"]), **plan}
            for attempt in range(2):
                body = {"canvasUuid": canvas_uuid, "nodes": plan["nodes"], "connections": plan["connections"],
                        "version": int(detail["version"]), "requestId": rid, "sessionId": str(uuid.uuid4()), "timestamp": int(time.time() * 1000)}
                try:
                    result = self.client.request("POST", f"/api/v1/pixa/canvas/project/{project_uuid}/nodes/batch", body)
                    return {"version": result.get("version"), "node_keys": plan["node_keys"], "aliases": plan["aliases"], "request_id": rid,
                            "canvas_url": detail["canvas_url"], "connections": plan["connections"]["create"]}
                except APIError as e:
                    if not e.version_conflict or attempt:
                        raise
                    fresh = self.get_project(project_uuid, canvas_uuid)
                    for key, paths in touched.items():
                        current = self.find_node(fresh, key)
                        current = {**current, "data": decode_data(current["data"])}
                        if any(get_path(current, path) != old for path, old in paths):
                            raise ValueError("目标字段已被其他编辑修改，返回版本冲突，请重新预览")
                    if plan["connections"]["create"] and fresh["connection_list"] != detail["connection_list"]:
                        raise ValueError("参考连线已被其他编辑修改，返回版本冲突")
                    detail = fresh
                    plan, touched = self._plan(detail, operations, operation_id)
        if dry_run:
            return execute("preview", True)
        return self.store.operation(operation_id, "apply_canvas", payload, execute)

    def validate_canvas(self, project_uuid, canvas_uuid):
        detail = self.get_project(project_uuid, canvas_uuid)
        errors, warnings = [], []
        try:
            check_graph(detail["node_list"], detail["connection_list"])
            for node in detail["node_list"]:
                data = decode_data(node["data"])
                kind = str(node["node_type"])
                model = data.get("params", {}).get("model")
                if model and kind in {"text", "image", "video", "audio"}:
                    configured = self.configure_model(data, kind, model)
                    self._validate_references(detail, node, configured)
                elif kind in {"image", "video", "audio"}:
                    warnings.append({"node_key": node["node_key"], "message": "占位节点未选择模型，提交前必须选择"})
        except ValueError as e:
            errors.append(str(e))
        return {"valid": not errors, "errors": errors, "warnings": warnings, "version": detail["version"], "canvas_url": detail["canvas_url"]}

    def apply_template(self, project_uuid, canvas_uuid, code, inputs, operation_id, model=None, prompt=None, dry_run=False):
        spec = self.get_tool_specs(code)
        if spec["kind"] != "guide":
            raise ValueError("首版模板搭图仅支持官网guide；skill可通过明确的提示词片段配置")
        extra = spec.get("extra", {})
        template_nodes = extra.get("node_list", [])
        required = {n["node_key"]: n for n in template_nodes if n.get("action_type") == "input"}
        if set(inputs) != set(required):
            raise ValueError("模板输入必须精确提供实时node_list中action_type=input的node_key")
        detail = self.get_project(project_uuid, canvas_uuid)
        mapping, ops, run_alias = dict(inputs), [], None
        for key, source in inputs.items():
            n = self.find_node(detail, source)
            kind = str(n["node_type"])
            actual = decode_data(n["data"]).get("mediaType") if kind == "upload" else kind
            if actual != required[key].get("node_type"):
                raise ValueError("模板输入媒体类型与实际节点不匹配")
        for i, n in enumerate(template_nodes):
            if n.get("action_type") == "input":
                continue
            alias = "template_" + str(i)
            mapping[n["node_key"]] = "$" + alias
            d = decode_data(n.get("data"))
            params = {k: copy.deepcopy(v) for k, v in (d.get("params") or {}).items() if v is not None and v != ""}
            skill, configured_prompt = params.pop("skill", None), params.pop("prompt", None)
            params.pop("url", None)
            if d.get("settings"):
                params["settings"] = merge(params.get("settings", {}), {k: v for k, v in d["settings"].items() if v is not None and v != ""})
            if skill:
                params["prompt"] = [{"type": "skill", "code": skill}]
            configured_model = params.pop("model", None)
            configured_model = (model if n["node_type"] == spec["node_type"] else None) or configured_model
            if not configured_model and n.get("node_type") in {"image", "video", "audio"}:
                raise ValueError("模板生成节点需要明确选择实时模型")
            ops.append({"op": "create", "type": n["node_type"], "as": alias, "name": n.get("name") or spec["name"],
                        "params": params, "model": configured_model, "prompt": prompt if prompt is not None else configured_prompt,
                        "data": {"pavo_shortcut": {"code": code, "kind": "guide", "tool_spec_version": spec.get("tool_spec_version")}}})
            run_alias = alias
        connections = extra.get("connection_list")
        if not connections:
            # 官网当前guide省略连接，用显式self_node及action_type=input的原生顺序组装。
            if "self_node" not in mapping:
                raise ValueError("实时模板既无连接又无self_node，不能推测输入拓扑")
            connections = [{"source": n["node_key"], "target": "self_node", "role": "reference", "media_order": i}
                           for i, n in enumerate(n for n in template_nodes if n.get("action_type") == "input")]
            connections += [{"source": "self_node", "target": n["node_key"], "role": "prompt"}
                            for n in template_nodes if n.get("action_type") == "output"]
        for i, edge in enumerate(connections):
            s = edge.get("source") or edge.get("source_node_key")
            t = edge.get("target") or edge.get("target_node_key")
            if s not in mapping or t not in mapping:
                raise ValueError("模板连线引用未知输入")
            ops.append({"op": "connect", "source": mapping[s], "target": mapping[t], "role": edge.get("role") or "reference", "media_order": edge.get("media_order", i),
                        **{k: edge[k] for k in ("source_handle", "target_handle", "source_port_type", "target_port_type", "connection_type", "color_key") if k in edge}})
        result = self.apply_canvas(project_uuid, canvas_uuid, ops, operation_id, dry_run)
        return {**result, "run_node_key": result["aliases"].get(run_alias), "template_code": code}
