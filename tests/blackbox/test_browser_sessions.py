"""Product login keeps a browser session while retaining server ownership boundaries."""

from __future__ import annotations

import copy
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from campus_fabric.api.queries import make_kernel_services, register_queries
from campus_fabric.app import create_app
from campus_fabric.auth.sessions import SESSION_SECONDS, hash_password

PASSWORD = "test-only-password-20261115"
BROWSER = {"X-Fabric-Request": "browser", "Origin": "http://testserver"}


@pytest.fixture(scope="module")
def accounts():
    encoded = hash_password(PASSWORD)
    return {name: {"password_hash": encoded, "actor": {
        "actor_id": owner or "admin", "owner_id": owner,
        "role": "administrator" if owner is None else "member",
    }} for name, owner in (("alice", "alice"), ("bob", "bob"),
                          ("chen", "supplier"), ("admin", None))}


def login(client, username="alice", password=PASSWORD, headers=None):
    return client.post("/x/v1/auth/login", json={"username": username, "password": password},
                       headers=BROWSER if headers is None else headers)


def browser(actor="alice"):
    return {**BROWSER, "X-Fabric-Actor": actor}


def node(owner):
    resources = {"cpu_millicores": 8000, "memory_mib": 16000,
                 "gpu_count": 1, "gpu_memory_mib": 8192}
    return {"node_id": f"{owner}-node", "contributor_owner_id": owner, "state": "Ready",
            "capacity": resources, "available": resources, "observed_at": 0.0,
            "devices": [{"device_id": f"{owner}-gpu", "physical_gpu_id": f"{owner}-gpu",
                         "model": "A10", "memory_mib": 8192, "state": "available"}]}


def product_app(tmp_path, accounts):
    app = create_app(data_dir=tmp_path, auth_accounts=accounts)
    register_queries(app, make_kernel_services(app.state.kernel, app.state.identity,
                     clock_origin="2026-01-01T00:00:00Z"), replace_existing=True)
    return app


def test_login_refresh_restart_and_private_persistence(tmp_path, accounts):
    app = create_app(data_dir=tmp_path, auth_accounts=accounts)
    with TestClient(app) as client:
        response = login(client)
        assert response.status_code == 200, response.text
        assert response.json() == {"username": "alice", "actor_id": "alice", "owner_id": "alice",
                                   "capabilities": ["use_compute", "contribute_resources"],
                                   "is_administrator": False}
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert f"Max-Age={SESSION_SECONDS}" in cookie and "Domain=" not in cookie
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/x/v1/session").json() == response.json()
        cookies = dict(client.cookies)
    reopened = create_app(data_dir=tmp_path, auth_accounts=accounts)
    with TestClient(reopened) as client:
        client.cookies.update(cookies)
        assert client.get("/x/v1/session").json() == response.json()
    raw_database = (tmp_path / "browser-sessions.sqlite3").read_bytes()
    assert PASSWORD.encode() not in raw_database
    assert next(iter(cookies.values())).encode() not in raw_database
    assert (tmp_path / "browser-sessions.sqlite3").stat().st_mode & 0o777 == 0o600


def test_bad_credentials_do_not_fall_back_to_client_role_or_owner(tmp_path, accounts):
    with TestClient(create_app(data_dir=tmp_path, auth_accounts=accounts)) as client:
        assert client.get("/x/v1/session", headers={"X-Owner": "alice"}).status_code == 401
        known = login(client, password="wrong")
        unknown = login(client, username="not-an-account", password="wrong")
        assert known.status_code == unknown.status_code == 401
        assert known.json()["detail"] == unknown.json()["detail"]
        assert "set-cookie" not in known.headers
        assert login(client).status_code == 200
        trusted = client.get("/x/v1/session", headers={"X-Role": "administrator"}).json()
        assert not trusted["is_administrator"]
        assert client.get("/x/v1/session", headers={"X-Owner": "bob"}).status_code == 403
        assert client.get("/x/v1/session", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert client.get("/x/v1/session", headers={"Authorization": ""}).status_code == 401
        assert client.get("/x/v1/session", headers={"Authorization": "", "X-Owner": "bob"}).status_code == 401


@pytest.mark.parametrize("headers", [{}, {"X-Fabric-Request": "browser", "Origin": "https://evil.test"},
    {"X-Fabric-Request": "browser", "Origin": "http://testserver:99"},
    {"X-Fabric-Request": "browser", "Origin": "null"},
    {"X-Fabric-Request": "browser", "Origin": "http://[broken"}])
def test_cross_site_login_logout_and_cookie_mutation_are_rejected(tmp_path, accounts, headers):
    with TestClient(create_app(data_dir=tmp_path, auth_accounts=accounts)) as client:
        assert login(client, headers=headers).status_code == 403
        assert login(client).status_code == 200
        before = client.app.state.kernel.store.read()
        assert client.post("/x/v1/auth/logout", headers=headers).status_code == 403
        assert client.post("/x/v1/contributions", json=node("alice"),
                           headers={**headers, "X-Fabric-Actor": "alice"}).status_code == 403
        assert client.get("/x/v1/session").status_code == 200
        assert client.app.state.kernel.store.read() == before


def test_cookie_identity_retains_owner_and_admin_isolation(tmp_path, accounts):
    with TestClient(product_app(tmp_path, accounts)) as client:
        assert login(client).status_code == 200
        assert client.post("/x/v1/contributions", json=node("alice"), headers=browser()).status_code == 201
        own = client.get("/x/v1/contributions", headers=browser()).json()
        assert any(item["node_id"] == "alice-node" for item in own["items"])
        before = client.app.state.kernel.store.read()
        assert client.post("/x/v1/contributions", json=node("bob"), headers=browser()).status_code == 403
        assert client.post("/x/v1/admin/clock", json={"advance_seconds": 1},
                           headers=browser()).status_code == 403
        assert client.post("/_test/reset", headers=browser()).status_code == 403
        assert client.app.state.kernel.store.read() == before
        assert login(client, "bob").status_code == 200
        assert client.get("/x/v1/contributions", headers=browser("bob")).json()["items"] == []
        assert client.post("/x/v1/contributions/recall", headers=browser("bob"),
                           json={"gpu_ids": ["alice-gpu"], "notice_seconds": 1,
                                 "duration_seconds": 5}).status_code == 403
        assert login(client, "admin").json()["is_administrator"] is True
        assert client.get("/x/v1/session").json()["owner_id"] is None


def test_alias_is_a_normal_member_and_stale_tabs_cannot_write_as_new_user(tmp_path, accounts):
    with TestClient(product_app(tmp_path, accounts)) as client:
        assert login(client).status_code == 200
        old_cookies = dict(client.cookies)
        response = login(client, "chen")
        assert response.status_code == 200
        assert response.json()["username"] == "chen"
        assert response.json()["actor_id"] == "supplier"
        assert response.json()["capabilities"] == ["use_compute", "contribute_resources"]
        for method, path in (("get", "/x/v1/contributions"), ("post", "/x/v1/contributions")):
            response = getattr(client, method)(path, headers=browser("alice"))
            assert response.status_code == 409
            assert response.json()["error"] == "account_changed"
        assert client.get("/x/v1/session").json()["username"] == "chen"
        assert client.post("/x/v1/contributions", json=node("supplier"),
                           headers=BROWSER).status_code == 409
        assert client.post("/x/v1/contributions", json=node("supplier"),
                           headers=browser("supplier")).status_code == 201
        with TestClient(client.app) as old_client:
            old_client.cookies.update(old_cookies)
            assert old_client.get("/x/v1/session").status_code == 401


def test_logout_and_expiration_revoke_sessions(tmp_path, accounts):
    app = create_app(data_dir=tmp_path, auth_accounts=accounts)
    with TestClient(app) as client:
        assert login(client).status_code == 200
        old_cookies = dict(client.cookies)
        assert client.post("/x/v1/auth/logout", headers=BROWSER).status_code == 200
        assert client.get("/x/v1/session").status_code == 401
        client.cookies.update(old_cookies)
        assert client.get("/x/v1/session").status_code == 401
        assert login(client).status_code == 200
        with sqlite3.connect(app.state.identity.browser_sessions.path) as db:
            db.execute("UPDATE sessions SET expires=0")
        assert client.get("/x/v1/session").status_code == 401


@pytest.mark.parametrize("change", ["password", "role", "removed"])
def test_account_configuration_change_revokes_prior_session(tmp_path, accounts, change):
    with TestClient(create_app(data_dir=tmp_path, auth_accounts=accounts)) as client:
        assert login(client).status_code == 200
        cookies = dict(client.cookies)
    modified = copy.deepcopy(accounts)
    if change == "password":
        modified["alice"]["password_hash"] = hash_password("replacement-password")
    elif change == "role":
        modified["alice"]["actor"]["role"] = "researcher"
    else:
        del modified["alice"]
    with TestClient(create_app(data_dir=tmp_path, auth_accounts=modified)) as client:
        client.cookies.update(cookies)
        assert client.get("/x/v1/session").status_code == 401


def test_https_cookie_and_separate_instances(tmp_path, accounts):
    first = create_app(data_dir=tmp_path / "first", auth_accounts=accounts)
    second = create_app(data_dir=tmp_path / "second", auth_accounts=accounts)
    assert first.state.identity.browser_sessions.cookie_name != second.state.identity.browser_sessions.cookie_name
    with TestClient(first, base_url="https://testserver") as client:
        response = login(client, headers={**BROWSER, "Origin": "https://testserver"})
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
        cookies = dict(client.cookies)
    with TestClient(second) as client:
        client.cookies.update(cookies)
        assert client.get("/x/v1/session").status_code == 401


def test_accounts_file_and_login_throttle(tmp_path, accounts, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(accounts))
    monkeypatch.setenv("FABRIC_ACCOUNTS_FILE", str(path))
    with TestClient(create_app(data_dir=tmp_path / "data")) as client:
        for _ in range(20):
            assert login(client, password="wrong").status_code == 401
        assert login(client, password="wrong").status_code == 429
