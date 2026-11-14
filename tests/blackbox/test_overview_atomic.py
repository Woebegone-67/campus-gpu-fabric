"""Admin overview must derive record counts and capacity from one committed read.

The concurrency regression uses two independent Kernel/SQLite connections. A
writer commits after the first SELECT has returned, before projection resumes;
no timing sleeps, mocked revisions, or replacement state dictionaries are used.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from campus_fabric.api.queries import QueryServices, make_kernel_services, register_queries
from campus_fabric.api.queries.service import QueryFailure
from campus_fabric.app import create_app
from campus_fabric.contracts import models as m
from campus_fabric.domain.kernel import Kernel
from campus_fabric.projections.read_models import InventoryRead, OverviewRead

ADMIN = m.AuthenticatedActor(actor_id="overview-admin", role="administrator")
ORIGIN = "2026-01-01T00:00:00Z"
PATH = "/x/v1/admin/overview"
VERSION = m.VersionStamp(epoch=2, revision=3)


def node(name="overview-node"):
    capacity = m.ResourceAmounts(cpu_millicores=8000, memory_mib=16000)
    return m.NodeInventory(node_id=name, contributor_owner_id="supplier", state="Ready",
                           capacity=capacity, available=capacity, observed_at=0.0)


def query_client(services):
    app = FastAPI()
    register_queries(app, services)
    return TestClient(app)


def commit_allocation(kernel, name="new-lease"):
    actor = m.AuthenticatedActor(actor_id="alice", role="researcher", owner_id="alice")
    parameters = m.AllocationCreateParameters(
        kind="allocation.create", owner_id="alice", ttl_seconds=300,
        request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1000,
                                  gpu=m.GPURequest(count=0)),
    )
    receipt, payload = kernel.commit_with_payload(m.CommandEnvelope(
        request_id=name, actor=actor, operation=parameters.kind, parameters=parameters,
    ))
    assert receipt.outcome == "committed", payload


@pytest.mark.parametrize("legacy", [False, True], ids=["atomic", "legacy-race-control"])
@pytest.mark.parametrize("write_kind", ["allocation", "clock"])
def test_actual_sqlite_commit_between_reads_is_coherent_or_legacy_409(
    tmp_path, monkeypatch, legacy, write_kind,
):
    database = tmp_path / "overview.sqlite3"
    kernel = Kernel(database, inventory=(node(),))
    writer = Kernel(database)
    services = make_kernel_services(kernel, SimpleNamespace(actor=lambda request: ADMIN),
                                    clock_origin=ORIGIN)
    if legacy:
        services = replace(services, overview_reader=None)
    else:
        # The new path cannot silently fall back to the old expensive read port.
        monkeypatch.setattr(kernel, "read_snapshot", Mock(side_effect=AssertionError(
            "Atomic overview must not call read_snapshot")))
        services = replace(services, inventory_reader=Mock(side_effect=AssertionError(
            "Atomic overview must not call its separate inventory provider")))

    captured = Event()
    committed = Event()
    original_read = kernel.store.read
    reads = []

    def read_with_commit_between_capture_and_projection():
        snapshot = original_read()
        reads.append((snapshot["revision"], snapshot["now"]))
        if len(reads) == 1:
            captured.set()
            assert committed.wait(5), "Concurrent writer failed to commit"
        return snapshot

    def mutate():
        assert captured.wait(5), "Overview never captured its first SQLite snapshot"
        try:
            if write_kind == "allocation":
                commit_allocation(writer)
            else:
                parameters = m.ClockAdvanceParameters(
                    kind="clock.advance", advance_seconds=1.0, source="test_control")
                receipt, _ = writer.commit_with_payload(m.CommandEnvelope(
                    request_id="concurrent-clock", operation=parameters.kind,
                    actor=m.AuthenticatedActor(actor_id="clock", role="test_driver"),
                    parameters=parameters,
                ))
                assert receipt.outcome == "committed"
        finally:
            committed.set()

    monkeypatch.setattr(kernel.store, "read", read_with_commit_between_capture_and_projection)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(mutate)
        response = query_client(services).get(PATH)
        future.result(timeout=5)

    fresh = writer.export_state(ADMIN)
    assert fresh["revision"] == 1
    if legacy:
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "stale_version"
        assert len(reads) == 2
    else:
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["version"] == {"epoch": 0, "revision": 0}
        assert body["now"] == 0
        assert body["counts"] == {"allocations": 0, "jobs": 0, "work_intents": 0}
        assert body["capacity"]["totals"]["available"]["cpu_millicores"] == 8000
        assert len(reads) == 1
        kernel.read_snapshot.assert_not_called()
        services.inventory_reader.assert_not_called()
        if write_kind == "allocation":
            assert len(fresh["allocations"]) == 1
            assert fresh["nodes"]["overview-node"]["available"]["cpu_millicores"] == 7000
        else:
            assert fresh["now"] == 1


@pytest.mark.parametrize("with_inventory", [False, True])
def test_empty_overview_reads_once_and_preserves_metric_shape(tmp_path, monkeypatch, with_inventory):
    kernel = Kernel(tmp_path / "empty.sqlite3", inventory=(node(),) if with_inventory else ())
    services = make_kernel_services(kernel, SimpleNamespace(actor=lambda request: ADMIN),
                                    clock_origin=ORIGIN)
    read = Mock(wraps=kernel.store.read)
    monkeypatch.setattr(kernel.store, "read", read)
    monkeypatch.setattr(kernel, "read_snapshot", Mock(side_effect=AssertionError("wrong reader")))
    response = query_client(services).get(PATH)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["counts"] == {"allocations": 0, "jobs": 0, "work_intents": 0}
    assert result["allocations_by_state"] == result["jobs_by_state"] == result["work_by_state"] == {}
    assert result["allocations"] == result["jobs"] == result["running_jobs"] == 0
    assert result["succeeded_jobs"] == result["failed_jobs"] == 0
    assert result["scope"] == "global" and result["owner_id"] is None
    assert result["measurement_kind"] == "authorized_record_counts"
    assert result["performance_measurements"] is None
    assert len(result["capacity"]["nodes"]) == int(with_inventory)
    assert result["capacity"]["totals"]["available"]["cpu_millicores"] == 8000 * with_inventory
    assert result["capacity"]["totals"]["capacity"]["memory_mib"] == 16000 * with_inventory
    assert response.headers["Cache-Control"] == "no-store"
    read.assert_called_once_with()
    kernel.read_snapshot.assert_not_called()


def test_multiple_owners_and_states_match_existing_metrics_and_legacy_overview(tmp_path):
    tokens = {
        "admin": ADMIN,
        "tenant-admin": m.AuthenticatedActor(actor_id="tenant-admin", role="administrator",
                                             owner_id="alice"),
        **{owner: m.AuthenticatedActor(actor_id=owner, owner_id=owner, role="researcher")
           for owner in ("alice", "bob")},
    }
    app = create_app(data_dir=tmp_path, inventory=(node("n1"), node("n2")),
                     auth_tokens=tokens, test_key="overview-clock-only")
    kernel = app.state.kernel
    services = make_kernel_services(kernel, app.state.identity, clock_origin=ORIGIN)
    register_queries(app, services, replace_existing=True)
    client = TestClient(app)

    def headers(owner):
        result = {"Authorization": "Bearer " + owner}
        if tokens[owner].owner_id is not None:
            result["X-Owner"] = tokens[owner].owner_id
        return result

    def allocation(owner, cpu=1000):
        response = client.post("/v1/allocations", headers=headers(owner), json={
            "owner": owner,
            "request": {"cpu_millicores": cpu, "memory_mib": 1000, "gpu": {"count": 0}},
        })
        assert response.status_code == (202 if cpu > 8000 else 201), response.text
        return response.json()["id"]

    def workflow(owner, name, actions=()):
        registration = m.WorkIntentRegistration(
            intent_id=name, owner_id=owner, execution_mode="logical_simulation",
            settlement_action="release_on_success", specification=m.WorkSpecification(
                allocation=m.AllocationCreateParameters(kind="allocation.create", owner_id=owner,
                    request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1000,
                                               gpu=m.GPURequest(count=0))),
                job=m.JobSpecification(image="simulation", command=("not-executed",)),
            ),
        )
        response = client.post("/x/v1/work-intents", headers=headers(owner),
                               json=registration.model_dump(mode="json"))
        assert response.status_code == 202, response.text
        path = "/x/v1/work-intents/" + name
        for action in actions:
            current = client.get(path, headers=headers(owner)).json()
            response = client.post(path + "/actions", headers=headers(owner), json={
                "action": action, "stage_id": action, "expected_version": current["version"],
            })
            assert response.status_code == 200, response.text

    workflow("alice", "completed", ("allocate", "create_job"))
    response = client.post("/_test/clock", headers={"X-Test-Key": "overview-clock-only"},
                           json={"advance_seconds": 10})
    assert response.status_code == 200, response.text
    workflow("bob", "running", ("allocate", "create_job"))
    workflow("alice", "pending")
    allocation("alice")
    allocation("bob", 100000)
    cancelled_lease = allocation("alice")
    response = client.post("/v1/jobs", headers=headers("alice"), json={
        "allocation_id": cancelled_lease, "image": "simulation", "command": ["not-executed"],
    })
    assert response.status_code == 201, response.text
    response = client.delete(f"/v1/jobs/{response.json()['id']}", headers=headers("alice"))
    assert response.status_code == 202, response.text

    metric_fields = (
        "scope", "owner_id", "allocations_by_state", "jobs_by_state", "work_by_state", "counts",
        "measurement_kind", "performance_measurements", "allocations", "jobs", "running_jobs",
        "succeeded_jobs", "failed_jobs", "version", "now",
    )
    overview = client.get(PATH, headers=headers("admin"))
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["counts"] == {"allocations": 5, "jobs": 3, "work_intents": 3}
    assert body["allocations_by_state"] == {"Allocated": 2, "Pending": 1, "Released": 1, "Running": 1}
    assert body["jobs_by_state"] == {"Cancelled": 1, "Running": 1, "Succeeded": 1}
    assert body["work_by_state"] == {"completed": 1, "pending": 1, "running": 1}
    assert body["running_jobs"] == body["succeeded_jobs"] == 1
    assert body["failed_jobs"] == 0
    assert body["capacity"]["totals"]["available"]["cpu_millicores"] == 13000
    assert body["capacity"]["totals"]["available"]["memory_mib"] == 29000
    metrics = client.get("/v1/metrics", headers=headers("admin")).json()
    assert {key: body[key] for key in metric_fields} == {key: metrics[key] for key in metric_fields}
    # Explicit administrator overview is global even for an owner-bearing admin.
    assert client.get(PATH, headers=headers("tenant-admin")).json()["counts"] == body["counts"]
    atomic = services.overview(ADMIN)
    assert {item.owner_id for item in atomic.snapshot.allocations} == {"alice", "bob"}
    assert not atomic.snapshot.events and not atomic.snapshot.decisions
    assert not atomic.snapshot.legal_actions
    legacy = replace(services, overview_reader=None)
    register_queries(app, legacy, replace_existing=True)
    old_body = client.get(PATH, headers=headers("admin")).json()
    assert {key: old_body[key] for key in (*metric_fields, "capacity")} == {
        key: body[key] for key in (*metric_fields, "capacity")}


@pytest.mark.parametrize("role", ["researcher", "contributor"])
@pytest.mark.parametrize("legacy", [False, True])
def test_non_admin_denied_before_any_provider_call(role, legacy):
    actor = m.AuthenticatedActor(actor_id="caller", owner_id="alice", role=role)
    read_port = Mock()
    inventory_reader = Mock(side_effect=AssertionError("inventory queried before authorization"))
    overview_reader = Mock(side_effect=AssertionError("overview queried before authorization"))
    services = QueryServices(read_port=read_port, inventory_reader=inventory_reader,
                             overview_reader=None if legacy else overview_reader,
                             resolve_actor=lambda request: actor, clock_origin=ORIGIN)
    response = query_client(services).get(PATH, headers={"X-Role": "administrator"})
    assert response.status_code == 403, response.text
    assert response.json()["error"] == "forbidden"
    with pytest.raises(QueryFailure) as error:
        services.overview(actor)
    assert error.value.status == 403
    read_port.read_snapshot.assert_not_called()
    inventory_reader.assert_not_called()
    overview_reader.assert_not_called()


def valid_overview():
    snapshot = m.ReadSnapshot(version=VERSION, now=10.0, viewer=ADMIN, global_scope=True)
    inventory = InventoryRead(viewer=ADMIN, version=VERSION, now=10.0, nodes=(node(),))
    return OverviewRead(snapshot=snapshot, inventory=inventory)


def malformed_overview(case):
    value = valid_overview()
    other_admin = m.AuthenticatedActor(actor_id="other-admin", role="administrator", owner_id="bob")
    if case == "wrong-outer-type":
        return {"snapshot": value.snapshot, "inventory": value.inventory}
    if case == "wrong-snapshot-type":
        return replace(value, snapshot={})
    if case == "wrong-inventory-type":
        return replace(value, inventory={})
    if case == "snapshot-viewer":
        return replace(value, snapshot=value.snapshot.model_copy(update={"viewer": other_admin}))
    if case == "inventory-viewer":
        return replace(value, inventory=replace(value.inventory, viewer=other_admin))
    if case == "owner-scope":
        return replace(value, snapshot=value.snapshot.model_copy(update={"global_scope": False}))
    if case == "version-mismatch":
        return replace(value, inventory=replace(value.inventory,
                        version=m.VersionStamp(epoch=2, revision=4)))
    if case == "epoch-mismatch":
        return replace(value, inventory=replace(value.inventory,
                        version=m.VersionStamp(epoch=3, revision=3)))
    if case == "time-mismatch":
        return replace(value, inventory=replace(value.inventory, now=11.0))
    if case == "bad-version":
        return replace(value, inventory=replace(value.inventory,
                        version=VERSION.model_copy(update={"revision": -1})))
    if case == "negative-time":
        return replace(value, inventory=replace(value.inventory, now=-1.0))
    if case == "nonfinite-time":
        return replace(value, inventory=replace(value.inventory, now=float("nan")))
    if case == "snapshot-future-object":
        allocation = m.AllocationView(allocation_id="future", owner_id="alice", state="Pending",
            request=m.ResourceRequest(cpu_millicores=1, memory_mib=1, gpu=m.GPURequest(count=0)),
            created_at=11.0)
        return replace(value, snapshot=value.snapshot.model_copy(update={"allocations": (allocation,)}))
    if case == "future-node":
        return replace(value, inventory=replace(value.inventory,
                        nodes=(node().model_copy(update={"observed_at": 11.0}),)))
    if case == "duplicate-node":
        return replace(value, inventory=replace(value.inventory, nodes=(node(), node())))
    if case == "over-capacity-node":
        invalid = node().model_copy(update={"available": m.ResourceAmounts(
            cpu_millicores=9000, memory_mib=16000)})
        return replace(value, inventory=replace(value.inventory, nodes=(invalid,)))
    if case == "wrong-node-type":
        return replace(value, inventory=replace(value.inventory, nodes=({},)))
    raise AssertionError(case)


@pytest.mark.parametrize("case", [
    "wrong-outer-type", "wrong-snapshot-type", "wrong-inventory-type", "snapshot-viewer",
    "inventory-viewer", "owner-scope", "bad-version", "negative-time", "nonfinite-time",
    "snapshot-future-object", "future-node", "duplicate-node", "over-capacity-node", "wrong-node-type",
    "version-mismatch", "epoch-mismatch", "time-mismatch",
])
def test_malformed_overview_is_rejected_without_retry_or_fallback(case):
    provider = Mock(return_value=malformed_overview(case))
    read_port = Mock()
    inventory_reader = Mock()
    services = QueryServices(read_port=read_port, inventory_reader=inventory_reader,
        overview_reader=provider, resolve_actor=lambda request: ADMIN, clock_origin=ORIGIN)
    response = query_client(services).get(PATH)
    expected = 409 if case.endswith("mismatch") else 502
    assert response.status_code == expected, response.text
    assert response.json()["error"] == ("stale_version" if expected == 409 else "provider_contract_error")
    provider.assert_called_once_with(ADMIN)
    read_port.read_snapshot.assert_not_called()
    inventory_reader.assert_not_called()


@pytest.mark.parametrize("field,value", [("version", m.VersionStamp(epoch=2, revision=4)),
                                         ("now", 11.0)])
def test_legacy_provider_mismatch_remains_explicit_409(field, value):
    overview = valid_overview()
    read_port = Mock()
    read_port.read_snapshot.return_value = overview.snapshot
    inventory_reader = Mock(return_value=replace(overview.inventory, **{field: value}))
    services = QueryServices(read_port=read_port, inventory_reader=inventory_reader,
                             resolve_actor=lambda request: ADMIN, clock_origin=ORIGIN)
    response = query_client(services).get(PATH)
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "stale_version"
    read_port.read_snapshot.assert_called_once()
    inventory_reader.assert_called_once_with(ADMIN)


def exported_pending_intent():
    return m.WorkIntent(
        intent_id="pending-work", owner_id="alice", first_arrival_at=0,
        execution_mode="logical_simulation", settlement_action="keep_lease", state="pending",
        specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(
                kind="allocation.create", owner_id="alice", request=m.ResourceRequest(
                    cpu_millicores=1000, memory_mib=1000, gpu=m.GPURequest(count=0))),
            job=m.JobSpecification(image="simulation", command=("not-executed",)),
        ),
    ).model_dump(mode="json")


@pytest.mark.parametrize("collection", ["nodes", "intents"])
@pytest.mark.parametrize("nested", [False, True], ids=["record-extra", "nested-extra"])
def test_adapter_rejects_unknown_node_and_intent_fields(tmp_path, monkeypatch, collection, nested):
    kernel = Kernel(tmp_path / "strict-export.sqlite3", inventory=(node(),))
    state = kernel.export_state(ADMIN)
    state["intents"] = {"pending-work": exported_pending_intent()}
    value = next(iter(state[collection].values()))
    if nested:
        value = value["capacity"] if collection == "nodes" else value["specification"]
    value["unrecognized_contract_field"] = "must-not-be-silently-dropped"
    export = Mock(return_value=state)
    monkeypatch.setattr(kernel, "export_state", export)
    monkeypatch.setattr(kernel, "read_snapshot", Mock(side_effect=AssertionError("fallback read")))
    services = make_kernel_services(kernel, SimpleNamespace(actor=lambda request: ADMIN),
                                    clock_origin=ORIGIN)
    response = query_client(services).get(PATH)
    assert response.status_code == 502, response.text
    assert response.json()["error"] == "provider_contract_error"
    export.assert_called_once_with(ADMIN)
    kernel.read_snapshot.assert_not_called()


@pytest.mark.parametrize("key", ["epoch", "revision", "now", "nodes", "allocations", "jobs", "intents"])
def test_adapter_rejects_missing_export_fields(tmp_path, monkeypatch, key):
    kernel = Kernel(tmp_path / "missing-field.sqlite3", inventory=(node(),))
    state = kernel.export_state(ADMIN)
    del state[key]
    export = Mock(return_value=state)
    monkeypatch.setattr(kernel, "export_state", export)
    services = make_kernel_services(kernel, SimpleNamespace(actor=lambda request: ADMIN),
                                    clock_origin=ORIGIN)
    response = query_client(services).get(PATH)
    assert response.status_code == 502, response.text
    assert response.json()["error"] == "provider_contract_error"
    export.assert_called_once_with(ADMIN)


@pytest.mark.parametrize("collection", ["nodes", "allocations", "jobs", "intents"])
@pytest.mark.parametrize("invalid", [None, []], ids=["null", "list"])
def test_adapter_rejects_nonmapping_export_collections(tmp_path, monkeypatch, collection, invalid):
    kernel = Kernel(tmp_path / "bad-collection.sqlite3", inventory=(node(),))
    state = kernel.export_state(ADMIN)
    state[collection] = invalid
    export = Mock(return_value=state)
    monkeypatch.setattr(kernel, "export_state", export)
    services = make_kernel_services(kernel, SimpleNamespace(actor=lambda request: ADMIN),
                                    clock_origin=ORIGIN)
    response = query_client(services).get(PATH)
    assert response.status_code == 502, response.text
    assert response.json()["error"] == "provider_contract_error"
    export.assert_called_once_with(ADMIN)


@pytest.mark.parametrize("value", [None, [], "not-a-state"])
def test_adapter_rejects_missing_or_wrong_state(value):
    kernel = SimpleNamespace(export_state=Mock(return_value=value))
    services = make_kernel_services(kernel, SimpleNamespace(actor=lambda request: ADMIN),
                                    clock_origin=ORIGIN)
    response = query_client(services).get(PATH)
    assert response.status_code == 502, response.text
    assert response.json()["error"] == "provider_contract_error"
    kernel.export_state.assert_called_once_with(ADMIN)


def test_adapter_rejects_missing_export_method():
    services = make_kernel_services(SimpleNamespace(), SimpleNamespace(actor=lambda request: ADMIN),
                                    clock_origin=ORIGIN)
    response = query_client(services).get(PATH)
    assert response.status_code == 502, response.text
    assert response.json()["error"] == "provider_contract_error"


@pytest.mark.parametrize("error,status,code", [
    (KeyError("missing state"), 502, "provider_contract_error"),
    (ValueError("invalid state"), 502, "provider_contract_error"),
    (TypeError("invalid export"), 502, "provider_contract_error"),
    (AttributeError("missing method"), 502, "provider_contract_error"),
    (PermissionError("denied"), 403, "forbidden"),
    (ConnectionError("offline"), 503, "dependency_unavailable"),
    (TimeoutError("timed out"), 503, "dependency_unavailable"),
])
def test_adapter_preserves_authority_and_availability_error_classes(error, status, code):
    kernel = SimpleNamespace(export_state=Mock(side_effect=error))
    services = make_kernel_services(kernel, SimpleNamespace(actor=lambda request: ADMIN),
                                    clock_origin=ORIGIN)
    response = query_client(services).get(PATH)
    assert response.status_code == status, response.text
    assert response.json()["error"] == code
    kernel.export_state.assert_called_once_with(ADMIN)
