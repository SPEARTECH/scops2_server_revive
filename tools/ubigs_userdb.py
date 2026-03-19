from __future__ import annotations

import json
import pathlib
import threading
import time
from typing import Any


class UserDB:
    def __init__(self, path: str) -> None:
        self.path = pathlib.Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"users": {}}

    def load(self) -> None:
        with self._lock:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                self._data = {"users": {}}
                return
            except OSError:
                self._data = {"users": {}}
                return
            try:
                obj = json.loads(raw)
            except Exception:
                obj = {"users": {}}
            if not isinstance(obj, dict) or "users" not in obj or not isinstance(obj.get("users"), dict):
                obj = {"users": {}}
            self._data = obj

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self.path)

    def upsert_user(self, *, username: str, password: str, dp: str | None = None) -> None:
        if not username:
            return
        with self._lock:
            users: dict[str, Any] = self._data.setdefault("users", {})
            u = users.get(username)
            if not isinstance(u, dict):
                u = {}
            u["password"] = password
            if dp is not None:
                u["dp"] = dp
            u.setdefault("created_ts", int(time.time()))
            users[username] = u

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self._lock:
            users = self._data.get("users")
            if not isinstance(users, dict):
                return None
            u = users.get(username)
            return u if isinstance(u, dict) else None

    def check_password(self, *, username: str, password: str) -> bool:
        u = self.get_user(username)
        if not u:
            return False
        return str(u.get("password", "")) == str(password)

    def remove_user(self, username: str) -> bool:
        with self._lock:
            users = self._data.get("users")
            if not isinstance(users, dict):
                return False
            if username in users:
                del users[username]
                return True
            return False
