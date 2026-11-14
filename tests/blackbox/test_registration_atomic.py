"""Registration without If-Match must not invent a stale caller precondition.

CPU ticks use an independent Kernel/SQLite connection. The deterministic barrier
commits a real tick immediately before the HTTP write obtains its transaction.
No retry, mocked revision, or replacement state bypasses the production write.
"""

import copy
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from campus_fabric.app import create_app
from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel

PATH = "/x/v1/work-intents"
ALICE = m.AuthenticatedActor(actor_id="alice", owner_id="alice", role="researcher")
SUPPLIER = m.AuthenticatedActor(actor_id="supplier", owner_id="supplier", role="contributor")
HEADERS = {"Authorization": "Bearer registration-alice", "X-Owner": "alice"}


def registration(name="atomic-registration"):
    return {
        "intent_id": name, "owner_id": "alice", "execution_mode": "cpu_reference",
        "settlement_action": "release_on_success",
        "specification": {
            "allocation": {
                "kind": "allocation.create", "owner_id": "alice", "ttl_seconds": 300,
                "request": {"cpu_millicores": 1000, "memory_mib": 1024,
                            "gpu": {"count": 0}},
            },
            "job": {"image": "reference", "command": ["fixed"], "timeout_seconds": 60},
        },
    }


@pytest.fixture
def host(tmp_path):
    app = create_app(
        data_dir=tmp_path, execution_mode="cpu_reference", scheduling_mode="builtin",
        auth_tokens={"registration-alice": ALICE, "registration-supplier": SUPPLIER},
    )
    kernel = app.state.kernel
    # No lifespan is entered: these tests exercise registration and the actual
    # clock port only, without launching an unrelated CPU executor process.
    client = TestClient(app)
    writer = Kernel(tmp_path / "fabric.sqlite3", execution_mode="cpu_reference")
    writer.sync_cpu_clock("registration-test-boot", 1_000_000_000)
    yield client, kernel, writer
    client.close()
    kernel.store.close()
    writer.store.close()


@pytest.mark.parametrize("explicit", [False, True], ids=["no-precondition", "explicit-stale"])
def test_tick_before_writer_lock_needs_no_retry_but_preserves_explicit_cas(
    host, monkeypatch, explicit,
):
    client, kernel, writer = host
    original_transaction = kernel.store.transaction
    entries = []

    @contextmanager
    def tick_then_lock():
        entries.append("entry")
        assert len(entries) == 1, "Registration must not mask the race with retries"
        writer.sync_cpu_clock("registration-test-boot", 1_050_000_000)
        with original_transaction() as conn:
            yield conn

    monkeypatch.setattr(kernel.store, "transaction", tick_then_lock)
    monkeypatch.setattr(kernel, "decision_snapshot", Mock(side_effect=AssertionError(
        "Registration must not read an unconsumed planner snapshot")))
    headers = {**HEADERS, **({"If-Match": "0:0"} if explicit else {})}
    response = client.post(PATH, headers=headers, json=registration())
    final = writer.store.read()
    assert entries == ["entry"]
    assert final["now"] == 0.05
    if explicit:
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "stale_version"
        assert final["revision"] == 1 and final["intents"] == {}
        assert not final["dedup"]
    else:
        assert response.status_code == 202, response.text
        assert response.json()["outcome"] == "committed"
        assert response.json()["version"] == {"epoch": 0, "revision": 2}
        assert response.json()["logical_time"] == 0.05
        assert final["intents"]["atomic-registration"]["first_arrival_at"] == 0.05
        assert len(final["intents"]) == len(final["dedup"]) == 1
        assert sum(e["event_type"] == "intent.registered" for e in final["events"]) == 1
    kernel.decision_snapshot.assert_not_called()


def test_registration_replay_keeps_original_arrival_and_rejects_changed_inputs(host):
    client, kernel, writer = host
    body = registration()
    first = client.post(PATH, headers=HEADERS, json=body)
    assert first.status_code == 202, first.text
    original = kernel.store.read()["intents"][body["intent_id"]]
    writer.sync_cpu_clock("registration-test-boot", 2_000_000_000)
    before_replay = kernel.store.read()
    replay = client.post(PATH, headers=HEADERS, json=body)
    assert replay.status_code == 202 and replay.json() == first.json()
    assert kernel.store.read() == before_replay
    assert kernel.store.read()["intents"][body["intent_id"]] == original
    assert original["first_arrival_at"] == 0

    # Durable idempotency still wins over an obsolete explicit expected version.
    explicit_replay = client.post(PATH, headers={**HEADERS, "If-Match": "0:0"}, json=body)
    assert explicit_replay.status_code == 202 and explicit_replay.json() == first.json()
    changed = copy.deepcopy(body)
    changed["specification"]["job"]["timeout_seconds"] = 61
    conflict = client.post(PATH, headers=HEADERS, json=changed)
    assert conflict.status_code == 409 and conflict.json()["error"] == "stage_conflict"
    assert kernel.store.read() == before_replay


def test_current_registration_still_rolls_back_with_the_shared_transaction(host, monkeypatch):
    client, kernel, writer = host
    before = kernel.store.read()
    original_transaction = kernel.store.transaction

    @contextmanager
    def fail_before_commit():
        with original_transaction() as conn:
            yield conn
            raise OSError("injected commit-boundary failure")

    with monkeypatch.context() as patch:
        patch.setattr(kernel.store, "transaction", fail_before_commit)
        with pytest.raises(OSError, match="commit-boundary"):
            client.post(PATH, headers=HEADERS, json=registration())
    assert kernel.store.read() == before
    writer.sync_cpu_clock("registration-test-boot", 2_000_000_000)
    response = client.post(PATH, headers=HEADERS, json=registration())
    assert response.status_code == 202, response.text
    assert response.json()["logical_time"] == 1.0
    assert len(kernel.store.read()["dedup"]) == 1


def test_explicit_current_version_and_workflow_stage_cas_remain_enforced(host):
    client, kernel, writer = host
    registered = client.post(PATH, headers={**HEADERS, "If-Match": "0:0"}, json=registration())
    assert registered.status_code == 202, registered.text
    version = registered.json()["version"]
    writer.sync_cpu_clock("registration-test-boot", 1_050_000_000)
    before = kernel.store.read()
    stage = client.post(PATH + "/atomic-registration/actions", headers=HEADERS,
                        json={"action": "allocate", "stage_id": "allocation-1",
                              "expected_version": version})
    assert stage.status_code == 409 and stage.json()["error"] == "stale_version"
    assert kernel.store.read() == before
    missing = client.post(PATH + "/atomic-registration/actions", headers=HEADERS,
                          json={"action": "allocate", "stage_id": "allocation-1"})
    assert missing.status_code == 400
    assert kernel.store.read() == before


@pytest.mark.parametrize("match", ["", "0", "0:1:2", "-1:0", "zero:one"])
def test_malformed_explicit_precondition_is_not_treated_as_absent(host, match):
    client, kernel, _ = host
    before = kernel.store.read()
    response = client.post(PATH, headers={**HEADERS, "If-Match": match}, json=registration())
    assert response.status_code == 400, response.text
    assert kernel.store.read() == before


def test_registration_authority_and_execution_mode_are_not_weakened(host):
    client, kernel, _ = host
    before = kernel.store.read()
    body = registration()
    body["owner_id"] = body["specification"]["allocation"]["owner_id"] = "supplier"
    mismatch = client.post(PATH, headers=HEADERS, json=body)
    assert mismatch.status_code == 403
    contributor = client.post(PATH, headers={"Authorization": "Bearer registration-supplier",
                                           "X-Owner": "supplier"}, json=body)
    assert contributor.status_code == 403 and contributor.json()["error"] == "forbidden"
    wrong_mode = registration()
    wrong_mode["execution_mode"] = "logical_simulation"
    mode_response = client.post(PATH, headers=HEADERS, json=wrong_mode)
    assert mode_response.status_code == 400 and mode_response.json()["error"] == "invalid_command"
    assert kernel.store.read() == before
