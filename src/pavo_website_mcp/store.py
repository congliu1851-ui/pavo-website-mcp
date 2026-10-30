"""本地私有会话与不可自动重投的操作台账。"""

import base64
from contextlib import contextmanager
from contextvars import ContextVar
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time


class OperationUnknown(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, root=None):
        self._operation_session = ContextVar("pavo_operation_session", default=None)
        self.root = Path(root or os.environ.get("PAVO_STATE_DIR", "~/.local/share/pavo-website-mcp")).expanduser().absolute()
        if self.root.is_symlink():
            raise ValueError("状态目录不能是符号链接")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.db = self.root / "operations.sqlite3"
        fd = os.open(self.db, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        self.db.chmod(0o600)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operations (
                operation_id TEXT PRIMARY KEY, kind TEXT NOT NULL, fingerprint TEXT NOT NULL,
                payload TEXT NOT NULL, request_id TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, error TEXT, updated REAL NOT NULL)""")
            db.execute("CREATE TABLE IF NOT EXISTS claims (resource TEXT PRIMARY KEY, operation_id TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.db, timeout=10)

    def session(self):
        pinned = self._operation_session.get()
        if pinned is not None:
            return pinned
        path = self.root / "session.json"
        if not path.exists():
            return {}
        if path.is_symlink():
            raise ValueError("会话文件不能是符号链接")
        path.chmod(0o600)
        return json.loads(path.read_text())

    def save_session(self, token, user_info):
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
            raise ValueError("登录响应缺少有效凭证")
        phone = str(user_info.get("phone_number", ""))
        safe_user = {"id": str(user_info.get("id", "")), "phone": phone[:3] + "****" + phone[-4:] if len(phone) >= 7 else "已隐藏"}
        expiry = None
        try:
            segment = token.split(".")[1]
            expiry = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))).get("exp")
            expiry = float(expiry) if expiry is not None else None
        except (ValueError, IndexError, TypeError):
            pass
        value = {"access_token": token, "user": safe_user, "expires_at": expiry}
        fd, tmp = tempfile.mkstemp(dir=self.root, prefix=".session-")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(canonical(value))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.root / "session.json")
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def status(self):
        data = self.session()
        expiry = data.get("expires_at")
        return {"logged_in": bool(data.get("access_token")), "user": data.get("user"),
                "expires_at": expiry, "expiry_is_unverified_jwt_claim": expiry is not None,
                "expired": bool(expiry and expiry <= time.time())}

    def account_key(self):
        session = self.session()
        return session.get("user", {}).get("id") or hashlib.sha256(session.get("access_token", "").encode()).hexdigest()

    def get_operation(self, operation_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if not row:
            raise ValueError("找不到该操作号")
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        result["payload"] = json.loads(result["payload"])
        # 另一个账号不能借本地历史回执进行恢复。
        if result["payload"].get("_account") != self.account_key():
            raise ValueError("该操作属于另一个登录会话，请切回原账号")
        return result

    @contextmanager
    def auth_context(self):
        # 写操作全程使用同一认证快照，其他进程换号不改变本次请求归属。
        token = self._operation_session.set(self.session())
        try:
            yield
        finally:
            self._operation_session.reset(token)

    def operation(self, operation_id, kind, payload, action):
        with self.auth_context():
            return self._operation(operation_id, kind, payload, action)

    def _operation(self, operation_id, kind, payload, action):
        if not isinstance(operation_id, str) or not operation_id.strip() or len(operation_id) > 200:
            raise ValueError("operation_id 必须是1至200字符的稳定操作号")
        payload = {**payload, "_account": self.account_key()}
        encoded = canonical(payload)
        fingerprint = hashlib.sha256((kind + encoded).encode()).hexdigest()
        request_id = "req-" + hashlib.sha256((kind + operation_id).encode()).hexdigest()[:32]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT fingerprint,status,result,error FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise ValueError("同一 operation_id 的输入或账号不同，请核对操作")
                if row[1] == "SUCCESS":
                    return json.loads(row[2])
                if row[1] == "FAILED":
                    raise ValueError(row[3] or "此操作已被明确拒绝；修正输入后使用新操作号")
                raise OperationUnknown("该操作正在执行或结果未知，禁止重投；请查询 pavo_get_operation 并核对官网回执")
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?,'RUNNING',NULL,NULL,?)",
                       (operation_id, kind, fingerprint, encoded, request_id, time.time()))
        action_returned = False
        try:
            result = action(request_id)
            action_returned = True
            encoded_result = canonical(result)
        except Exception as e:
            definite = not action_returned and (isinstance(e, ValueError) or getattr(e, "definite", False))
            state = "FAILED" if definite else "UNKNOWN"
            message = str(e) if definite else "请求或后处理未完成，服务端结果未知；禁止自动重投"
            with self.connect() as db:
                db.execute("UPDATE operations SET status=?,error=?,updated=? WHERE operation_id=?", (state, message, time.time(), operation_id))
            if definite:
                raise
            raise OperationUnknown(message) from None
        with self.connect() as db:
            db.execute("UPDATE operations SET status='SUCCESS',result=?,updated=? WHERE operation_id=?", (encoded_result, time.time(), operation_id))
        return result

    def claim(self, resource, operation_id, replace_operation_id=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT operation_id FROM claims WHERE resource=?", (resource,)).fetchone()
            if row and row[0] != operation_id and row[0] != replace_operation_id:
                raise OperationUnknown("该节点已有生成提交记录；先核对原操作回执，换操作号也不能绕过未知提交")
            db.execute("INSERT OR REPLACE INTO claims VALUES (?,?)", (resource, operation_id))

    def claim_owner(self, resource):
        with self.connect() as db:
            row = db.execute("SELECT operation_id FROM claims WHERE resource=?", (resource,)).fetchone()
        return row[0] if row else None

    def release_claim(self, resource, operation_id, restore_operation_id=None):
        with self.connect() as db:
            if restore_operation_id:
                db.execute("UPDATE claims SET operation_id=? WHERE resource=? AND operation_id=?", (restore_operation_id, resource, operation_id))
            else:
                db.execute("DELETE FROM claims WHERE resource=? AND operation_id=?", (resource, operation_id))

    @contextmanager
    def resource_lock(self, resource):
        path = self.root / (".lock-" + hashlib.sha256(resource.encode()).hexdigest())
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
