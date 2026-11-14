"""Query module evidence using explicit isolated fixtures, not product runtime data."""

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from campus_fabric.api.queries import QueryServices, register_queries
from campus_fabric.app import create_app
from campus_fabric.contracts.models import (
    AllocationView,
    AuthenticatedActor,
    CommittedEvent,
    EntityRef,
    EventCursor,
    EvidenceCheck,
    GPUDevice,
    GPURequest,
    LegalAction,
    NodeInventory,
    ReadSnapshot,
    RequestCause,
    ResourceAmounts,
    ResourceRequest,
    VersionStamp,
)
from campus_fabric.projections.read_models import EvidenceRead, InventoryRead

ACTOR = AuthenticatedActor(actor_id="fixture-actor", role="researcher", owner_id="fixture-alice")
VERSION = VersionStamp(epoch=2, revision=3)
ORIGIN = "2026-01-01T00:00:00Z"
REQUEST = ResourceRequest(cpu_millicores=100, memory_mib=256, gpu=GPURequest(count=1))


def allocation(name="fixture-a1", owner="fixture-alice"):
    return AllocationView(
        allocation_id=name, owner_id=owner, state="Pending", request=REQUEST, created_at=1.0
    )


def event(sequence):
    return CommittedEvent(
        event_id=f"fixture-e{sequence}", sequence=sequence, version=VERSION,
        logical_time=float(sequence), event_type="FixtureEvent", owner_id=ACTOR.owner_id,
        entities=(EntityRef(kind="allocation", entity_id="fixture-a1"),),
        cause=RequestCause(kind="request", request_id=f"fixture-request-{sequence}"),
    )


class FixtureReader:
    def __init__(self, snapshot):
        self.snapshot, self.queries = snapshot, []

    def read_snapshot(self, query):
        self.queries.append(query)
        return self.snapshot


def configured(snapshot=None, actor=ACTOR):
    snapshot = snapshot or ReadSnapshot(
        version=VERSION, now=10.0, viewer=actor,
        allocations=(allocation(), allocation("fixture-a2")),
    )
    return QueryServices(
        read_port=FixtureReader(snapshot), resolve_actor=lambda request: actor, clock_origin=ORIGIN
    )


def client(services=None):
    app = FastAPI()
    register_queries(app, services)
    return TestClient(app)


def test_no_provider_cannot_masquerade_as_empty_inventory_or_success():
    for path in ("/v1/jobs", "/v1/capacity", "/x/v1/work-intents", "/x/v1/acceptance"):
        response = client().get(path, headers={"X-Owner": "admin", "X-Role": "administrator"})
        assert response.status_code == 503
        assert response.json()["error"] == "dependency_unavailable"
        assert "items" not in response.json()
        assert response.headers["Cache-Control"] == "no-store"
    response = client(QueryServices(resolve_actor=lambda request: ACTOR)).get("/v1/jobs")
    assert response.status_code == 503
    assert "ReadPort" in response.json()["detail"]


def test_public_allocation_shape_uses_declared_origin_and_missing_origin_fails_closed():
    services = configured()
    response = client(services).get("/v1/allocations/fixture-a1")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == body["item"]["allocation_id"] == "fixture-a1"
    assert body["owner"] == "fixture-alice"
    assert body["created_at"] == "2026-01-01T00:00:01Z"
    assert body["created_at_logical"] == 1.0
    assert client(replace(services, clock_origin=None)).get("/v1/allocations").status_code == 503


def test_owner_scope_and_detail_do_not_expose_foreign_records():
    services = configured()
    assert client(services).get("/v1/allocations/foreign-id").status_code == 404
    response = client(services).get("/v1/metrics?global_scope=true")
    assert response.json()["counts"]["allocations"] == 2
    assert services.read_port.queries[-1].global_scope is False
    assert client(services).get("/x/v1/admin/overview", headers={"X-Role": "administrator"}).status_code == 403


def test_provider_returning_another_viewer_or_cross_owner_snapshot_is_rejected():
    other = AuthenticatedActor(actor_id="other", owner_id="fixture-bob", role="researcher")
    wrong_viewer = ReadSnapshot(version=VERSION, now=10.0, viewer=other)
    assert client(configured(wrong_viewer)).get("/v1/jobs").status_code == 502
    poisoned = ReadSnapshot.model_construct(
        version=VERSION, now=10.0, viewer=ACTOR, global_scope=False,
        allocations=(allocation(owner="fixture-bob"),), jobs=(), intents=(), events=(),
        decisions=(), legal_actions=(), next_cursor=None,
    )
    response = client(configured(poisoned)).get("/v1/allocations")
    assert response.status_code == 502
    assert "fixture-bob" not in response.text


def test_unrelated_legal_actions_are_not_exposed():
    snapshot = ReadSnapshot(
        version=VERSION, now=10.0, viewer=ACTOR, allocations=(allocation(),),
        legal_actions=(LegalAction(operation="allocation.release",
                                   entity=EntityRef(kind="allocation", entity_id="foreign-id"),
                                   explanation="must not reach caller"),),
    )
    assert client(configured(snapshot)).get("/v1/allocations").json()["legal_actions"] == []


def test_pagination_pins_revision_and_rejects_invalid_limits():
    app = client(configured())
    first = app.get("/v1/allocations?limit=1").json()
    assert first["page"]["next_offset"] == 1
    assert app.get("/v1/allocations?limit=1&offset=1").status_code == 400
    second = app.get("/v1/allocations?limit=1&offset=1&at_epoch=2&at_revision=3")
    assert second.status_code == 200
    assert second.json()["items"][0]["id"] == "fixture-a2"
    assert app.get("/v1/allocations?offset=1&at_epoch=1&at_revision=3").status_code == 409
    for value in ("0", "501", "-1", "1.5", "abc"):
        assert app.get(f"/v1/allocations?limit={value}").status_code == 400


def test_event_cursor_survives_page_truncation_and_rejects_reset_epoch():
    snapshot = ReadSnapshot(
        version=VERSION, now=10.0, viewer=ACTOR, events=(event(1), event(2)),
        next_cursor=EventCursor(epoch=2, after_sequence=20),
    )
    app = client(configured(snapshot))
    first = app.get("/v1/events?limit=1").json()
    assert first["next_cursor"] == {"epoch": 2, "after_sequence": 1}
    assert first["next_after"] == "2:1" and first["has_more"] is True
    second = app.get("/v1/events?limit=1&after=2:1").json()
    assert second["items"][0]["sequence"] == 2
    assert second["next_after"] == "2:2"
    assert app.get("/v1/events?after=2:21").status_code == 409
    assert app.get("/v1/events?after=1:1").status_code == 409
    assert app.get("/v1/events?after=invalid").status_code == 400


def node(name, owner):
    resources = ResourceAmounts(cpu_millicores=100, memory_mib=256, gpu_count=1)
    return NodeInventory(
        node_id=name, contributor_owner_id=owner, state="Ready", capacity=resources,
        available=resources, observed_at=1.0,
        devices=(GPUDevice(device_id=name+"-gpu", physical_gpu_id=name+"-gpu", model="A100",
                           memory_mib=40000, state="available"),),
    )


def test_inventory_uses_authorized_provider_and_contributions_filter_again():
    contributor = AuthenticatedActor(actor_id="contributor", role="contributor", owner_id="fixture-alice")
    def reader(actor):
        return InventoryRead(
            actor, VERSION, 10.0, (node("n1", "fixture-alice"), node("n2", "fixture-bob"))
        )
    services = replace(configured(actor=contributor), inventory_reader=reader)
    app = client(services)
    capacity = app.get("/v1/capacity").json()
    assert capacity["totals"]["capacity"]["gpu_count"] == 2
    assert "contributor_owner_id" not in capacity["nodes"][0]
    own = app.get("/x/v1/contributions").json()["items"]
    assert len(own) == 1 and own[0]["node_id"] == "n1"
    assert client(replace(services, resolve_actor=lambda request: ACTOR)).get("/x/v1/contributions").status_code == 403


def test_admin_overview_requires_same_revision_across_providers():
    admin = AuthenticatedActor(actor_id="admin", role="administrator")
    snapshot = ReadSnapshot(version=VERSION, now=10.0, viewer=admin, global_scope=True)
    services = replace(
        configured(snapshot, admin),
        inventory_reader=lambda actor: InventoryRead(actor, VersionStamp(epoch=2, revision=4), 10.0, ()),
    )
    assert client(services).get("/x/v1/admin/overview").status_code == 409


def test_absent_or_unexecuted_acceptance_never_claims_met():
    admin = AuthenticatedActor(actor_id="admin", role="administrator")
    services = QueryServices(resolve_actor=lambda request: admin)
    assert client(services).get("/x/v1/acceptance").status_code == 503
    services = replace(services, evidence_reader=lambda actor: EvidenceRead(
        actor, VERSION, 10.0,
        checks=(EvidenceCheck(target_id="T-PRODUCT", status="NOT_RUN", expected="run user journey"),),
        build_matches=True,
    ))
    assert client(services).get("/x/v1/acceptance").json()["status"] == "NOT_RUN"


def test_explicit_registration_replaces_get_stubs_without_changing_writes(tmp_path):
    app = create_app(data_dir=tmp_path)
    write_routes = { (route.path, method): route.endpoint
                     for route in app.routes for method in getattr(route, "methods", ())
                     if method in {"POST", "DELETE", "PATCH", "PUT"} }
    with pytest.raises(ValueError):
        register_queries(app, configured())
    register_queries(app, configured(), replace_existing=True)
    current = TestClient(app)
    assert current.get("/v1/allocations").status_code == 200
    assert current.post("/v1/allocations", json={}).status_code == 401
    assert current.post("/_test/reset").status_code == 403
    assert current.get("/healthz").json()["control_plane_ready"] is True
    after = { (route.path, method): route.endpoint
              for route in app.routes for method in getattr(route, "methods", ())
              if method in {"POST", "DELETE", "PATCH", "PUT"} }
    assert after == write_routes
