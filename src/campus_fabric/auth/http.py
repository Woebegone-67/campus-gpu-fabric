"""Trusted HTTP identity adapter; client role headers never grant authority."""

from __future__ import annotations

import hmac
import json
from pathlib import Path
from typing import Mapping

from fastapi import HTTPException, Request
from pydantic import ValidationError

from campus_fabric.auth.sessions import BrowserSessions
from campus_fabric.contracts.models import AuthenticatedActor


class HTTPIdentity:
    def __init__(self, tokens: Mapping | None = None, *, test_key: str | None = None,
                 browser_sessions: BrowserSessions | None = None):
        self.tokens: dict[str, AuthenticatedActor] | None = None
        if tokens is not None:
            self.tokens = {}
            for token, actor in tokens.items():
                if not isinstance(token, str) or not token:
                    raise ValueError("Authentication tokens must be nonempty strings")
                actor = actor if isinstance(actor, AuthenticatedActor) else (
                    AuthenticatedActor.model_validate_json(json.dumps(actor))
                )
                if actor.role in {"test_driver", "system"}:
                    raise ValueError("Internal roles cannot be granted by a public bearer token")
                self.tokens[token] = actor
        self.test_key = test_key or None
        self.browser_sessions = browser_sessions

    @classmethod
    def from_file(cls, path: str | Path | None, *, test_key: str | None = None):
        return cls(json.loads(Path(path).read_text()) if path else None, test_key=test_key)

    def actor(self, request: Request) -> AuthenticatedActor:
        owner = request.headers.get("x-owner")
        authorization = request.headers.get("authorization")
        if authorization is None and self.browser_sessions is not None:
            return self.browser_sessions.actor(request)
        if self.tokens is not None:
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(401, "Bearer authentication is required")
            token = authorization.removeprefix("Bearer ")
            actor = next((actor for key, actor in self.tokens.items()
                          if hmac.compare_digest(token.encode(), key.encode())), None)
            if actor is None:
                raise HTTPException(401, "Invalid bearer credential")
            if actor.owner_id is not None and owner != actor.owner_id:
                raise HTTPException(403, "X-Owner must match the authenticated owner")
            if actor.owner_id is None and owner:
                raise HTTPException(403, "This global identity cannot impersonate an owner")
            return actor
        if self.browser_sessions is not None:
            # An empty Authorization header must not enter the development adapter.
            raise HTTPException(401, "请登录账户。")
        if authorization:
            # Local mode does not parse unverified bearer strings into identities.
            raise HTTPException(401, "Bearer authentication has not been configured")
        if not owner:
            raise HTTPException(401, "X-Owner is required in local development mode")
        try:
            return AuthenticatedActor(actor_id=owner, role="researcher", owner_id=owner)
        except ValidationError as exc:
            raise HTTPException(400, "Invalid X-Owner") from exc

    def test_actor(self, request: Request) -> AuthenticatedActor:
        provided = request.headers.get("x-test-key", "")
        if not self.test_key or not hmac.compare_digest(provided.encode(), self.test_key.encode()):
            raise HTTPException(403, "Invalid or disabled test credential")
        return AuthenticatedActor(actor_id="http-test-driver", role="test_driver")
