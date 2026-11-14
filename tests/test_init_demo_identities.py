"""Fresh demo identities gain both capabilities; existing files never migrate implicitly."""

import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from campus_fabric.auth.http import HTTPIdentity

ROOT = Path(__file__).resolve().parents[1]


def initialize(directory):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts/init_cp1_demo.py"), str(directory)],
        capture_output=True, text=True, check=False,
    )


def test_fresh_credentials_bind_dual_capability_members_and_separate_admin(tmp_path):
    result = initialize(tmp_path)
    assert result.returncode == 0, result.stderr
    auth_path = tmp_path / "auth.json"
    client_path = tmp_path / "client-identities.json"
    tokens = json.loads(auth_path.read_text())
    clients = json.loads(client_path.read_text())
    adapter = HTTPIdentity.from_file(auth_path)
    assert set(clients) == {"alice", "bob", "supplier", "admin"}
    assert len(tokens) == 4
    for name, headers in clients.items():
        token = headers["Authorization"].removeprefix("Bearer ")
        assert token not in result.stdout + result.stderr
        actor = adapter.tokens[token]
        assert actor.actor_id == name
        assert actor.can_use_compute and actor.can_contribute_resources
        if name == "admin":
            assert actor.role == "administrator"
            assert actor.owner_id is None
            assert "X-Owner" not in headers
        else:
            assert actor.role == "member"
            assert actor.owner_id == headers["X-Owner"] == name
    for path in (auth_path, client_path):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("existing", ["auth.json", "client-identities.json", "both"])
def test_existing_credentials_are_preserved_without_migration_or_partial_pair(tmp_path, existing):
    legacy = {
        "old-alice": {"actor_id": "alice", "role": "researcher", "owner_id": "alice"},
        "old-supplier": {
            "actor_id": "supplier", "role": "contributor", "owner_id": "supplier",
        },
    }
    contents = {
        "auth.json": json.dumps(legacy).encode(),
        "client-identities.json": b'{"alice":{"Authorization":"Bearer old-alice"}}',
    }
    present = contents if existing == "both" else {existing: contents[existing]}
    for name, content in present.items():
        (tmp_path / name).write_bytes(content)
    result = initialize(tmp_path)
    assert result.returncode != 0
    assert "Existing identities preserved" in result.stderr
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == present
    if "auth.json" in present:
        adapter = HTTPIdentity.from_file(tmp_path / "auth.json")
        assert adapter.tokens["old-alice"].capabilities == ("use_compute",)
        assert adapter.tokens["old-supplier"].capabilities == ("contribute_resources",)
