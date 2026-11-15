"""Configured-account access and revocable sessions, separate from the resource ledger.

Account-direct login intentionally accepts any nonempty password-field value. It
does not verify password ownership. Stored hashes remain configuration-compatible
and participate in session revocation when the account configuration changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from campus_fabric.contracts.models import AuthenticatedActor

SESSION_SECONDS = 7 * 24 * 60 * 60
_HASH_PREFIX = "scrypt$16384$8$1"


def hash_password(password: str) -> str:
    if not isinstance(password, str) or not 1 <= len(password) <= 256:
        raise ValueError("Password must contain 1 to 256 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"{_HASH_PREFIX}${salt.hex()}${derived.hex()}"


def _validate_hash(encoded: str) -> tuple[bytes, bytes]:
    if not isinstance(encoded, str) or not encoded.startswith(_HASH_PREFIX + "$"):
        raise ValueError("Account password must use the supported scrypt hash")
    parts = encoded.split("$")
    if len(parts) != 6:
        raise ValueError("Invalid password hash")
    salt, expected = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    if len(salt) != 16 or len(expected) != 32:
        raise ValueError("Invalid password hash")
    return salt, expected


def require_browser_request(request: Request) -> None:
    """JSON/custom-header requests cannot be forged by cross-site form submissions."""
    if request.headers.get("x-fabric-request") != "browser":
        raise HTTPException(403, "请从本站页面提交操作。")
    origin = request.headers.get("origin")
    if origin:
        try:
            parsed = urlsplit(origin)
        except ValueError:
            raise HTTPException(403, "请求来源不匹配。") from None
        if (parsed.scheme != request.url.scheme or parsed.netloc.lower() !=
                request.url.netloc.lower() or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise HTTPException(403, "请求来源不匹配。")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "请求来源不匹配。")


class AccountChanged(HTTPException):
    error_code = "account_changed"

    def __init__(self):
        super().__init__(409, "账户已切换，请刷新后重试。")


class BrowserSessions:
    def __init__(self, accounts: Mapping, directory: Path):
        self.accounts: dict[str, tuple[str, AuthenticatedActor, str]] = {}
        for username, record in accounts.items():
            if (not isinstance(username, str) or not 1 <= len(username) <= 128
                    or any(character.isspace() for character in username)):
                raise ValueError("Account names must be nonempty and trimmed")
            encoded = record["password_hash"]
            _validate_hash(encoded)
            actor = AuthenticatedActor.model_validate_json(json.dumps(record["actor"]))
            if actor.role in {"test_driver", "system"}:
                raise ValueError("Internal roles cannot be granted to browser accounts")
            fingerprint = hashlib.sha256(json.dumps(
                [encoded, actor.model_dump(mode="json")], sort_keys=True).encode()).hexdigest()
            self.accounts[username] = (encoded, actor, fingerprint)
        self.path = directory / "browser-sessions.sqlite3"
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.path.chmod(0o600)
        namespace = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()[:16]
        self.cookie_name = f"fabric_session_{namespace}"
        with self._connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS sessions ("
                       "digest TEXT PRIMARY KEY, username TEXT NOT NULL, "
                       "fingerprint TEXT NOT NULL, expires REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS login_attempts ("
                       "client TEXT PRIMARY KEY, count INTEGER NOT NULL, expires REAL NOT NULL)")

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def login(self, request: Request, username: str, password: str):
        require_browser_request(request)
        now = time.time()
        client = self._digest(request.client.host if request.client else "unknown")
        limited = False
        with self._connection() as db:
            db.execute("DELETE FROM login_attempts WHERE expires <= ?", (now,))
            db.execute("DELETE FROM sessions WHERE expires <= ?", (now,))
            db.execute("INSERT INTO login_attempts VALUES (?, 1, ?) ON CONFLICT(client) "
                       "DO UPDATE SET count=count+1", (client, now + 60))
            count = db.execute("SELECT count FROM login_attempts WHERE client=?", (client,)).fetchone()[0]
            limited = count > 20
        if limited:
            raise HTTPException(429, "尝试次数较多，请稍后再试。")
        entry = self.accounts.get(username)
        if entry is None or not isinstance(password, str) or not 1 <= len(password) <= 256:
            raise HTTPException(401, "账户不存在或登录内容为空。")
        token = secrets.token_urlsafe(32)
        with self._connection() as db:
            previous = request.cookies.get(self.cookie_name)
            if previous:
                db.execute("DELETE FROM sessions WHERE digest=?", (self._digest(previous),))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                       (self._digest(token), username, entry[2], now + SESSION_SECONDS))
        return entry[1], token

    def actor(self, request: Request) -> AuthenticatedActor:
        token = request.cookies.get(self.cookie_name, "")
        if not token or len(token) > 128:
            raise HTTPException(401, "请登录账户。")
        with self._connection() as db:
            row = db.execute("SELECT username, fingerprint, expires FROM sessions WHERE digest=?",
                             (self._digest(token),)).fetchone()
        entry = self.accounts.get(row[0]) if row else None
        if not row or row[2] <= time.time() or entry is None or row[1] != entry[2]:
            raise HTTPException(401, "登录已过期，请重新登录。")
        actor = entry[1]
        request.state.auth_username = row[0]
        owner = request.headers.get("x-owner")
        if owner is not None and owner != actor.owner_id:
            raise HTTPException(403, "账户不能访问其他用户的资源。")
        expected_actor = request.headers.get("x-fabric-actor")
        if ((expected_actor is not None or request.method not in {"GET", "HEAD", "OPTIONS"})
                and expected_actor != actor.actor_id):
            raise AccountChanged()
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            require_browser_request(request)
        return actor

    def logout(self, request: Request) -> None:
        require_browser_request(request)
        token = request.cookies.get(self.cookie_name)
        if token:
            with self._connection() as db:
                db.execute("DELETE FROM sessions WHERE digest=?", (self._digest(token),))
