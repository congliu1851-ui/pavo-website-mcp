"""生产业务API；媒体传输另用无授权上下文。"""

import base64
import json
import re
import time

import httpx

from .media import PublicTransport


class APIError(RuntimeError):
    def __init__(self, message, code="", status=None, definite=False):
        super().__init__(message)
        self.code, self.status, self.definite = code, status, definite

    @property
    def version_conflict(self):
        return self.status == 409 or self.code == "VERSION_CONFLICT"


def business_error_hint(message):
    """只输出固定类别，不把服务端可能回显的正文、URL或凭证写入工具及台账。"""
    if not isinstance(message, str):
        return ""
    text = message.lower()
    for pattern, label in (
        (r"请求.{0,6}频繁|频率限制|rate.?limit|too many requests", "请求限流"),
        (r"任务.{0,12}(未结束|进行中|运行中)|正在.{0,8}(执行|运行|生成|处理)|资源.{0,8}(占用|繁忙)|server busy|resource.{0,12}busy|in progress|获取.{0,6}锁.{0,6}失败", "任务占用或繁忙"),
        (r"同一.{0,8}(片段|场景|分集)|same (segment|scene|episode)|cross.?segment|cross.?episode", "片段或归属范围约束"),
        (r"状态.{0,8}(冲突|不允许|不正确|不合法)|版本.{0,8}(冲突|过期)|state.{0,20}(conflict|invalid|not allow)|version.{0,12}(conflict|stale)", "状态或版本冲突"),
        (r"时长|duration", "时长校验"),
        (r"长度.{0,10}(超|过|限)|too (long|large)|maximum length|character limit", "内容长度限制"),
        (r"无权限|权限不足|permission denied|not authorized|forbidden", "权限限制"),
        (r"参数.{0,8}(错误|无效|非法)|invalid (parameter|argument)|素材.{0,8}(不属于|不存在)|not found|does not exist", "参数或资源校验"),
        (r"稍后重试|try again|temporar|系统.{0,8}(异常|错误)", "暂时性业务错误"),
    ):
        if re.search(pattern, text):
            return label
    return ""


class Client:
    base_url = "https://api.pavo-ai.cn"

    def __init__(self, store, transport=None, media_transport=None):
        self.store = store
        self.http = httpx.Client(transport=transport, timeout=45, follow_redirects=False, trust_env=False)
        self.media = httpx.Client(transport=media_transport or PublicTransport(), timeout=180, follow_redirects=False, trust_env=False)

    def close(self):
        self.http.close()
        self.media.close()

    def request(self, method, path, payload=None, params=None, _token=None):
        if not path.startswith("/api/") or ".." in path or "://" in path or "?" in path or "#" in path:
            raise ValueError("业务请求必须使用固定官网API路径")
        headers = {"Accept": "application/json", "X-Platform": "1"}
        session = {"access_token": _token} if _token is not None else self.store.session()
        if not session.get("access_token"):
            raise APIError("尚未登录，请先导入官网Bearer认证", definite=True)
        if _token is None and self.store.status()["expired"]:
            raise APIError("本地登录凭证已到期，请重新导入官网Bearer认证", definite=True)
        headers["Authorization"] = "Bearer " + session["access_token"]
        try:
            response = self.http.request(method, self.base_url + path, params=params, json=payload, headers=headers)
        except httpx.HTTPError:
            raise APIError("官网网络请求未完成，写入结果可能未知", definite=False) from None
        if response.status_code >= 400:
            raise APIError("登录失效，请重新登录" if response.status_code == 401 else f"官网请求返回 HTTP {response.status_code}",
                           status=response.status_code, definite=400 <= response.status_code < 500)
        if not 200 <= response.status_code < 300:
            raise APIError("官网返回意外重定向，未继续发送登录凭证")
        try:
            envelope = response.json()
        except ValueError:
            raise APIError("官网返回非JSON响应，未回显响应内容") from None
        if not isinstance(envelope, dict) or "code" not in envelope:
            raise APIError("官网响应信封不完整，结果需要核对")
        code = str(envelope["code"])
        if code != "000000":
            raw_message = envelope.get("message") or envelope.get("error_message", "")
            message = str(raw_message).lower()
            conflict = ("version" in message or "版本" in message) and any(s in message for s in ("conflict", "stale", "outdated", "冲突", "过期"))
            safe_code = code if code.isalnum() and len(code) <= 40 else "UNKNOWN"
            hint = business_error_hint(raw_message)
            detail = f"（诊断线索：{hint}，非根因确认）" if hint else ""
            raise APIError("登录失效，请重新登录" if code == "000501" else f"官网业务请求被拒绝，HTTP {response.status_code}，错误码 {safe_code}{detail}",
                           code="VERSION_CONFLICT" if conflict else safe_code, status=response.status_code, definite=True)
        return envelope.get("data")

    def import_authorization(self, authorization):
        match = re.fullmatch(r"Bearer ([A-Za-z0-9._~-]+)", authorization.strip(), flags=re.IGNORECASE)
        if not match:
            raise ValueError("请提供格式为Bearer加一个空格加凭证的Authorization值；不接受其他认证方式或换行")
        token, hints = match.group(1), {}
        try:
            segment = token.split(".")[1]
            hints = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
            if not isinstance(hints, dict):
                hints = {}
        except (ValueError, IndexError):
            pass
        try:
            if hints.get("exp") is not None and float(hints["exp"]) <= time.time():
                raise ValueError("此凭证已到期，请从官网取得新的Authorization")
            if hints.get("nbf") is not None and float(hints["nbf"]) > time.time() + 60:
                raise ValueError("此凭证尚未生效，请核对官网凭证和本机时间")
        except (TypeError, OverflowError):
            raise ValueError("凭证时间字段无法识别") from None
        data = self.request("GET", "/api/v1/pixa/canvas/folder/entries", _token=token)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise APIError("官网只读核验未返回已知项目列表结构，未保存凭证")
        self.store.save_session(token, {"id": str(hints.get("sub", ""))})
        return {**self.store.status(), "verified_by_readonly_api": True, "project_count_on_response": len(data["items"])}
