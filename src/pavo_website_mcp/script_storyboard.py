"""沿官网分镜编辑合同保存文字、时长及必要的拆段结构。"""

import copy
import json
import os
from pathlib import Path
import re
import tempfile

from .canvas import identifier
from .client import APIError
from .script import ROOT, check_text, fingerprint
from .store import OperationUnknown, canonical

MENTION = re.compile(r"@\[([^\]\n]+)\]\(sd-asset://(character|scene|prop)/([^()\s]+)\)")


def entities(board):
    segments, scenes, shots = {}, {}, {}
    for segment in board["segments"]:
        segments[str(segment["segment_id"])] = segment
        for scene in segment.get("scenes", []):
            scenes[str(scene["scene_id"])] = scene
            for shot in scene.get("shots", []):
                shots[str(shot["shot_id"])] = shot
    return segments, scenes, shots


def refs(text):
    return {(m[2], m[3]) for m in MENTION.finditer(text)}


def ref_keys(values):
    return {(r["relation_type"], str(r.get("character_state_id") or r.get("scene_asset_id") or r.get("prop_state_id") or r.get("prop_asset_id"))) for r in values}


def protected(board):
    value = copy.deepcopy(board)
    value.pop("_mcp_fingerprints", None)
    for segment in value["segments"]:
        for key in ("video_prompt_text", "estimated_duration_ms"):
            segment.pop(key, None)
        for scene in segment.get("scenes", []):
            for key in ("content_plain", "asset_refs", "linked_scene_asset_id"):
                scene.pop(key, None)
            for shot in scene.get("shots", []):
                for key in ("content_plain", "asset_refs"):
                    shot.pop(key, None)
    return value


class ScriptStoryboard:
    def __init__(self, script):
        self.script, self.client, self.store = script, script.client, script.store

    def _read(self, project, episode_id):
        episode = self.script._storyboard_episode(project, episode_id)
        if episode.get("enterable") is not True:
            raise ValueError("该制作分集尚不可进入分镜详情，请先读取pavo_script_list_storyboard_episodes")
        board = self.client.request("GET", ROOT + f"/episodes/{identifier(str(episode_id))}/storyboard/detail")
        if not isinstance(board, dict) or str(board.get("episode_id")) != str(episode_id) or not isinstance(board.get("segments"), list):
            raise APIError("分镜响应分集归属或结构不符，停止写入")
        return board

    @staticmethod
    def _fingerprints(board):
        return {"storyboard": fingerprint(board), **{name: {key: fingerprint(value) for key, value in records.items()} for name, records in zip(("segments", "scenes", "shots"), entities(board))}}

    def get(self, project_id, episode_id):
        board = self._read(self.script._project(project_id, False), episode_id)
        return {**board, "_mcp_fingerprints": self._fingerprints(board)}

    def _command(self, project_id, episode_id, operation_id, method, path, body, verify):
        resource = canonical([self.store.account_key(), "script-storyboard", str(project_id), str(episode_id)])
        owner = self.store.claim_owner(resource)
        if owner and owner != operation_id and self.store.get_operation(owner)["status"] not in {"SUCCESS", "FAILED"}:
            raise OperationUnknown("该集分镜有未知写入，禁止换操作号重投；先查询原回执并回读官网")
        self.store.claim(resource, operation_id, owner)
        try:
            response = self.client.request(method, path, body)
        except APIError as e:
            if e.definite:
                self.store.release_claim(resource, operation_id, owner)
            raise
        # HTTP确认后的一切核验失败均保留UNKNOWN，不能把部分成功误记为可重试失败。
        try:
            board = self._read(self.script._project(project_id, False), episode_id)
            verify(board, response)
        except Exception as e:
            raise APIError("分镜请求已提交，但回读未确认完整结果；禁止重投，先核官网及原操作回执") from e
        return {"project_id": str(project_id), "episode_id": str(episode_id), "verified": True, "generation_submitted": False,
                "data": response, "storyboard": board, "_mcp_fingerprints": self._fingerprints(board)}

    def _before(self, project, episode_id, expected_sha256):
        board = self.script._storyboard_preflight(lambda: self._read(project, episode_id))
        if board.get("active_tasks"):
            raise ValueError("该集分镜有运行任务，等待结束后再编辑")
        if fingerprint(board) != expected_sha256:
            raise ValueError("分镜在读取后已改变，请回读最新指纹")
        return board

    def _validate_mentions(self, project_id, texts):
        assets = self.script._storyboard_preflight(lambda: self.client.request("GET", ROOT + f"/projects/{project_id}/assets"))
        allowed = {(kind, str(state["state_id"])) for plural, kind in (("characters", "character"), ("scenes", "scene"), ("props", "prop"))
                   for asset in assets[plural] for state in asset.get("states", [])}
        for text in texts:
            check_text(text, "分镜内容")
            if "sd-asset://" in MENTION.sub("", text):
                raise ValueError("素材引用须为完整的@[名称](sd-asset://类型/状态ID)标签")
            if not refs(text) <= allowed:
                raise ValueError("分镜引用的素材状态不属于本项目或类型不符")

    @staticmethod
    def _verify_update(expected, after, shots, scenes):
        if protected(after) != protected(expected):
            raise APIError("分镜文字/时长未完整保存或受保护字段改变")
        affected = {str(p["shot_id"]) for p in shots}
        changed_scenes = {str(p["scene_id"]) for p in scenes}
        for scene in entities(after)[1].values():
            scene_affected = str(scene["scene_id"]) in changed_scenes
            for shot in scene.get("shots", []):
                if str(shot["shot_id"]) in affected:
                    scene_affected = True
                    # 官网编辑保存仅按本镜标签重建引用，初次生成时的继承scene不能强加回来。
                    if ref_keys(shot.get("asset_refs", [])) != refs(shot["content_rich"]):
                        raise APIError("分镜素材派生引用与编辑后的内容不符")
            if scene_affected:
                wanted = refs(scene["content_rich"]) | set().union(*(refs(s["content_rich"]) for s in scene.get("shots", [])))
                if ref_keys(scene.get("asset_refs", [])) != wanted:
                    raise APIError("场景汇总素材引用与镜头内容不符")
        for segment in after["segments"]:
            segment_shots = [shot for scene in segment.get("scenes", []) for shot in scene.get("shots", [])]
            if any(str(shot["shot_id"]) in affected for shot in segment_shots):
                total = sum(shot["duration_sec"] for shot in segment_shots)
                prompt = segment.get("video_prompt_text")
                if segment.get("estimated_duration_ms") != total * 1000 or not isinstance(prompt, str):
                    raise APIError("分镜派生段时长未同步")
                cuts = list(re.finditer(r"【\s*(\d+)\s*s\s*-\s*(\d+)\s*s\s*】", prompt))
                if len(cuts) != len(segment_shots):
                    raise APIError("派生视频提示词切点数量未同步")
                start = 0
                for i, (shot, cut) in enumerate(zip(segment_shots, cuts)):
                    end = start + shot["duration_sec"]
                    if (int(cut[1]), int(cut[2])) != (start, end):
                        raise APIError("派生视频提示词切点时长未同步")
                    body = prompt[cut.end():cuts[i+1].start() if i+1 < len(cuts) else len(prompt)]
                    body = re.sub(r"@图片\d+\([^)]*\)", "", body)
                    body = re.sub(r"\s+", "", MENTION.sub("", body))
                    wanted = re.sub(r"\s+", "", MENTION.sub("", shot["content_rich"]))
                    if not wanted or wanted not in body:
                        raise APIError("派生视频提示词正文与镜头新描述未同步")
                    start = end

    def update(self, project_id, episode_id, shots, scenes, expected_sha256, operation_id):
        if not isinstance(shots, list) or not isinstance(scenes, list) or not (shots or scenes):
            raise ValueError("至少提供一条镜头或场景编辑")
        if any(not isinstance(p, dict) or not p or set(p) - {"shot_id", "content_rich", "duration_sec"} or "shot_id" not in p or len(p) < 2 for p in shots):
            raise ValueError("镜头编辑仅支持shot_id、content_rich、duration_sec，不修改标题、媒体或绑定字段")
        if any(not isinstance(p, dict) or set(p) != {"scene_id", "content_rich"} for p in scenes):
            raise ValueError("场景编辑仅支持scene_id和content_rich")
        for patch in scenes:
            text = patch["content_rich"]
            check_text(text, "场景素材字段")
            mentions = list(MENTION.finditer(text))
            if len(mentions) != 1 or mentions[0][2] != "scene" or MENTION.sub("", text).strip():
                raise ValueError("场景素材字段须只含一个完整场景标签；场景细节写入镜头正文，缺少地点时先完善场景资产")
        for p in shots:
            if "duration_sec" in p and (isinstance(p["duration_sec"], bool) or not isinstance(p["duration_sec"], int) or not 1 <= p["duration_sec"] <= 15):
                raise ValueError("单镜时长须为1至15秒整数，与官网编辑器一致；单段生成上限另按模型核对")
        payload = {"episode_id": str(episode_id), "shots": shots, "scenes": scenes, "expected_sha256": expected_sha256}
        def action(project):
            before = self._before(project, episode_id, expected_sha256)
            expected = copy.deepcopy(before)
            _, old_scenes, old_shots = entities(before)
            _, new_scenes, new_shots = entities(expected)
            body = {"shots": [], "scenes": []}
            for patches, originals, targets, key, fields in ((shots, old_shots, new_shots, "shot_id", ("content_rich", "duration_sec")), (scenes, old_scenes, new_scenes, "scene_id", ("content_rich",))):
                ids = [str(p[key]) for p in patches]
                if len(ids) != len(set(ids)):
                    raise ValueError("同一镜头或场景不能重复编辑")
                for patch in patches:
                    ident = identifier(str(patch[key]))
                    if ident not in originals:
                        raise ValueError("指定镜头或场景不属于该分集，禁止跨项目或跨集编辑")
                    targets[ident].update({k: v for k, v in patch.items() if k != key})
                    body["shots" if key == "shot_id" else "scenes"].append({key: ident, **{k: targets[ident][k] for k in fields}})
            owners = {str(c["scene_id"]): str(g["segment_id"]) for g in before["segments"] for c in g.get("scenes", [])}
            shot_owners = {str(s["shot_id"]): owners[str(c["scene_id"])] for g in before["segments"] for c in g.get("scenes", []) for s in c.get("shots", [])}
            target_segments = {shot_owners[str(p["shot_id"])] for p in shots} | {owners[str(p["scene_id"])] for p in scenes}
            if len(target_segments) != 1:
                raise ValueError("单次保存仅支持同一片段的镜头及场景，与官网编辑器合同一致；整集须逐段保存")
            self._validate_mentions(project_id, [p["content_rich"] for p in body["shots"] + body["scenes"]])
            def verify(after, response):
                if not isinstance(response, dict) or any(not x.get("success") for key in ("results", "scene_results") for x in response.get(key, [])):
                    raise APIError("官网分镜批量保存存在部分失败")
                self._verify_update(expected, after, shots, scenes)
            return self._command(project_id, episode_id, operation_id, "PATCH", ROOT + "/shots", body, verify)
        return self.script._write(project_id, operation_id, "script_storyboard_update", payload, action)

    def create(self, project_id, episode_id, display_order, expected_sha256, operation_id, segment_id=None):
        if isinstance(display_order, bool) or not isinstance(display_order, int) or display_order < 1:
            raise ValueError("display_order须为正整数")
        kind = "shot" if segment_id is not None else "segment"
        payload = {"episode_id": str(episode_id), "segment_id": segment_id, "display_order": display_order, "expected_sha256": expected_sha256}
        def action(project):
            before = self._before(project, episode_id, expected_sha256)
            old_segments, _, old_shots = entities(before)
            if segment_id is not None and str(segment_id) not in old_segments:
                raise ValueError("指定片段不属于该分集")
            records = old_segments.values() if segment_id is None else [s for c in old_segments[str(segment_id)].get("scenes", []) for s in c.get("shots", [])]
            if display_order > max((x["display_order"] for x in records), default=0) + 1:
                raise ValueError("display_order超出现有末尾的下一位置")
            endpoint = f"/episodes/{episode_id}/segments" if segment_id is None else f"/segments/{identifier(str(segment_id))}/shots"
            def verify(after, response):
                segments, _, shots_after = entities(after)
                old, new = (old_segments, segments) if segment_id is None else (old_shots, shots_after)
                added = set(new) - set(old)
                if len(added) != 1 or not isinstance(response, dict) or str(response.get(kind + "_id")) not in added:
                    raise APIError("新增分镜回执ID或新增数量不符")
                expected = copy.deepcopy(after)
                if segment_id is None:
                    expected["segments"] = [s for s in expected["segments"] if str(s["segment_id"]) not in added]
                else:
                    for s in expected["segments"]:
                        for c in s.get("scenes", []):
                            c["shots"] = [x for x in c.get("shots", []) if str(x["shot_id"]) not in added]
                if self._structure_protected(expected) != self._structure_protected(before):
                    raise APIError("新增分镜时原有资料或媒体改变")
                # 官网插入会重排display_order；仅允许既有对象顺序保持不变。
                if self._order(expected) != self._order(before):
                    raise APIError("新增分镜打乱了既有对象顺序")
            return self._command(project_id, episode_id, operation_id, "POST", ROOT + endpoint, {"display_order": display_order}, verify)
        return self.script._write(project_id, operation_id, "script_storyboard_create_" + kind, payload, action)

    @staticmethod
    def _structure_protected(board):
        value = protected(board)
        for segment in value["segments"]:
            segment.pop("display_order", None)
            for scene in segment.get("scenes", []):
                for shot in scene.get("shots", []):
                    shot.pop("display_order", None)
        return value

    @staticmethod
    def _order(board):
        return [(str(segment["segment_id"]), [(str(c["scene_id"]), [str(s["shot_id"]) for s in sorted(c.get("shots", []), key=lambda x: x["display_order"])]) for c in segment.get("scenes", [])]) for segment in sorted(board["segments"], key=lambda x: x["display_order"])]

    def delete_shot(self, project_id, episode_id, shot_id, expected_sha256, operation_id):
        payload = {"episode_id": str(episode_id), "shot_id": str(shot_id), "expected_sha256": expected_sha256}
        def action(project):
            before = self._before(project, episode_id, expected_sha256)
            _, scenes, shots = entities(before)
            if str(shot_id) not in shots:
                raise ValueError("指定镜头不属于该分集")
            source = next(c for c in scenes.values() if any(str(s["shot_id"]) == str(shot_id) for s in c.get("shots", [])))
            if len(source["shots"]) <= 1:
                raise ValueError("不能删除场景最后一个镜头；先完成拆段内容迁移并回读核验")
            expected = copy.deepcopy(before)
            for segment in expected["segments"]:
                for scene in segment.get("scenes", []):
                    scene["shots"] = [s for s in scene.get("shots", []) if str(s["shot_id"]) != str(shot_id)]
            def verify(after, response):
                if str(shot_id) in entities(after)[2] or self._structure_protected(after) != self._structure_protected(expected) or self._order(after) != self._order(expected):
                    raise APIError("删除镜头回读不符或影响了其它内容、媒体")
            return self._command(project_id, episode_id, operation_id, "DELETE", ROOT + f"/shots/{identifier(str(shot_id))}", None, verify)
        return self.script._write(project_id, operation_id, "script_storyboard_delete_shot", payload, action)

    def delete_segment(self, project_id, episode_id, segment_id, replacement_segment_ids, expected_sha256, backup_dir, operation_id):
        segment_id = identifier(str(segment_id))
        if not isinstance(replacement_segment_ids, list) or not replacement_segment_ids:
            raise ValueError("删除旧段须明确至少一个同集替代片段")
        replacements = [identifier(str(x)) for x in replacement_segment_ids]
        if len(replacements) != len(set(replacements)) or segment_id in replacements:
            raise ValueError("替代片段须唯一且不能包含待删旧段")
        payload = {"episode_id": str(episode_id), "segment_id": segment_id, "replacement_segment_ids": replacements,
                   "expected_sha256": expected_sha256, "backup_dir": backup_dir}
        def action(project):
            before = self._before(project, episode_id, expected_sha256)
            segments = entities(before)[0]
            if segment_id not in segments or not set(replacements) <= set(segments):
                raise ValueError("旧段或替代片段不属于本项目的同一制作分集")
            for sid in replacements:
                shots = [s for c in segments[sid].get("scenes", []) for s in c.get("shots", [])]
                if not shots or any(not isinstance(s.get("content_rich"), str) or not s["content_rich"].strip() for s in shots):
                    raise ValueError("替代片段必须有已保存的非空镜头内容；调用方须先核完整台词与动作")
            target = segments[segment_id]
            if any(v for k, v in target.items() if k.startswith(("selected_keyframe_", "selected_video_"))):
                raise ValueError("旧段存在已选关键帧或视频，禁止自动迁移删除")
            for role in ("segment_keyframe", "segment_video"):
                history = self.client.request("GET", ROOT + f"/segments/{segment_id}/media", params={"role": role})
                if not isinstance(history, dict) or not isinstance(history.get("items"), list):
                    raise APIError("旧段媒体历史结构不明，禁止删除")
                if history["items"]:
                    raise ValueError("旧段存在媒体历史，禁止自动迁移删除，即使当前未选中")
            try:
                backup = self._backup_board(before, project_id, episode_id, backup_dir)
            except OSError:
                raise ValueError("完整分镜备份未写入确认，未发送删除请求") from None
            expected = copy.deepcopy(before)
            expected["segments"] = [s for s in expected["segments"] if str(s["segment_id"]) != segment_id]
            def verify(after, response):
                actual = entities(after)[0]
                if segment_id in actual or not set(replacements) <= set(actual):
                    raise APIError("旧段删除或替代片段保留未确认")
                if self._structure_protected(after) != self._structure_protected(expected) or self._order(after) != self._order(expected):
                    raise APIError("删除旧段影响其它段结构、顺序、正文或媒体")
                for sid, old in entities(expected)[0].items():
                    old, new = copy.deepcopy(old), copy.deepcopy(actual[sid])
                    old.pop("display_order", None)
                    new.pop("display_order", None)
                    if old != new:
                        raise APIError("删除旧段改变其它段内容或派生视频提示词")
                after_project = self.script._project(project_id, True)
                text_fields = ("episode_id", "episode_no", "title", "content")
                if [{k: e.get(k) for k in text_fields} for e in project["episodes"]] != [{k: e.get(k) for k in text_fields} for e in after_project["episodes"]]:
                    raise APIError("删除旧段意外改变剧本正文或分集标题")
            result = self._command(project_id, episode_id, operation_id, "DELETE", ROOT + f"/segments/{segment_id}", None, verify)
            return {**result, "backup": backup, "deleted_segment_id": segment_id, "replacement_segment_ids": replacements}
        return self.script._write(project_id, operation_id, "script_storyboard_delete_segment", payload, action)

    @staticmethod
    def _backup_board(board, project_id, episode_id, backup_dir):
        out = Path(backup_dir).expanduser()
        if not out.is_absolute() or out.is_symlink() or ".." in out.parts:
            raise ValueError("备份目录须为明确的绝对目录，不能使用符号链接或路径穿越")
        out.mkdir(parents=True, exist_ok=True)
        digest = fingerprint(board)
        target = out / f"storyboard-before-delete-{identifier(str(episode_id))}-{digest[:12]}.json"
        raw = (json.dumps({"project_id": str(project_id), "episode_id": str(episode_id), "storyboard_sha256": digest, "storyboard": board}, ensure_ascii=False, indent=2) + "\n").encode()
        fd, temp = tempfile.mkstemp(dir=out, prefix=".storyboard-backup-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(temp, target)
            except FileExistsError:
                if target.is_symlink() or not target.is_file() or target.read_bytes() != raw:
                    raise ValueError("同名备份存在但内容不同，禁止覆盖及删除旧段") from None
        finally:
            os.unlink(temp)
        return {"path": str(target), "storyboard_sha256": digest, "bytes": len(raw)}
