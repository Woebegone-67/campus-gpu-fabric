"""Ordinary users can follow their decisions without reading competing work."""

import json

import pytest
from fastapi.testclient import TestClient

from campus_fabric.contracts import models as m
from campus_fabric.policies import make_policy
from campus_fabric.workflows.devhost import create_app


def application(tmp_path, monkeypatch, policy="B0"):
    monkeypatch.setenv("FABRIC_B_POLICY", policy)
    actors = {
        owner: m.AuthenticatedActor(actor_id=owner, owner_id=owner, role="researcher")
        for owner in ("alice", "bob", "outsider")
    }
    actors["admin"] = m.AuthenticatedActor(actor_id="admin", role="administrator")
    resources = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8000)
    node = m.NodeInventory(node_id="shared-node", contributor_owner_id="supplier", state="Ready",
                           capacity=resources, available=resources, observed_at=0.0)
    app = create_app(data_dir=tmp_path, inventory=(node,), worker_enabled=False,
                     scheduling_mode="external",
                     auth_tokens={f"test-{name}": actor for name, actor in actors.items()})
    return app, actors


def headers(owner):
    return {"Authorization": f"Bearer test-{owner}",
            **({"X-Owner": owner} if owner != "admin" else {})}


def submit_work(client, owner):
    registration = m.WorkIntentRegistration(
        intent_id=f"work-{owner}", owner_id=owner, execution_mode="logical_simulation",
        settlement_action="keep_lease",
        specification=m.WorkSpecification(
            allocation=m.AllocationCreateParameters(kind="allocation.create", owner_id=owner,
                request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1000,
                                           gpu=m.GPURequest(count=0))),
            job=m.JobSpecification(image="simulation", command=("not-executed",)),
        ),
    )
    response = client.post("/x/v1/work-intents", headers=headers(owner),
                           json=registration.model_dump(mode="json"))
    assert response.status_code == 202, response.text


@pytest.mark.parametrize("policy", ["B0", "B1", "B2"])
def test_mixed_owner_grant_decisions_are_readable_and_private(tmp_path, monkeypatch, policy):
    app, actors = application(tmp_path, monkeypatch, policy)
    kernel = app.state.kernel
    with TestClient(app) as client:
        for owner in ("alice", "bob"):
            submit_work(client, owner)
        app.state.workflow_worker.tick()
        app.state.workflow_worker.tick()
        state_before_reads = kernel.store.read()
        ids = {}
        for owner in ("alice", "bob"):
            assert client.get(f"/x/v1/work-intents/work-{owner}",
                              headers=headers(owner)).json()["item"]["state"] == "running"
            events = client.get("/v1/events", headers=headers(owner)).json()["items"]
            grant = next(e for e in events if e["event_type"] == "allocation.granted")
            ids[owner] = grant["decision_id"]
            path = f"/x/v1/decisions/{ids[owner]}"
            response = client.get(path, headers=headers(owner))
            assert response.status_code == 200, response.text
            own = response.json()
            plan = own["item"]
            assert {c["owner_id"] for c in plan["candidates"]} == {owner}
            assert plan["selected_candidate_id"] in {c["candidate_id"] for c in plan["candidates"]}
            assert all(not c["scores"] and not c["preconditions"] for c in plan["candidates"])
            assert plan["coverage"]["scope"] == "partial"
            assert plan["coverage"]["domain_size"] is None
            assert not plan["asserts_pool_infeasible"]
            assert own["commit_status"] == "committed"
            assert grant in own["actual_events"]
            assert all(e["owner_id"] == owner for e in own["actual_events"])
            other = "bob" if owner == "alice" else "alice"
            assert other not in response.text
            assert client.get(path, headers=headers("outsider")).status_code == 404
            spoofed = {**headers("outsider"), "X-Role": "administrator"}
            assert client.get(path + "?global_scope=true", headers=spoofed).status_code == 404
            admin = client.get(path, headers=headers("admin")).json()["item"]
            assert admin == state_before_reads["decisions"][ids[owner]]

        # The first plan considered both owners. Bob may inspect his candidates,
        # but must not learn Alice's selected candidate or receive Alice's events.
        admin_first = client.get(f"/x/v1/decisions/{ids['alice']}",
                                 headers=headers("admin")).json()["item"]
        assert {c["owner_id"] for c in admin_first["candidates"]} == {"alice", "bob"}
        bob_first = client.get(f"/x/v1/decisions/{ids['alice']}",
                               headers=headers("bob")).json()
        assert bob_first["item"]["selected_candidate_id"] is None
        assert bob_first["actual_events"] == []
        assert bob_first["commit_status"] == "not_observed"
        assert admin_first["selected_candidate_id"] not in json.dumps(bob_first)
        assert "alice" not in json.dumps(bob_first)

        # Both authoritative read paths use the same restricted decision view.
        owner_export = kernel.export_state(actors["alice"])
        assert "bob" not in json.dumps(owner_export["decisions"])
        assert owner_export["decisions"][ids["alice"]]["coverage"]["scope"] == "partial"
        assert kernel.store.read() == state_before_reads, "Read projections must not rewrite decisions"


def test_truncated_plan_redacts_global_context_even_when_only_own_candidates_remain(
    tmp_path, monkeypatch,
):
    app, _ = application(tmp_path, monkeypatch, "B2")
    kernel = app.state.kernel
    with TestClient(app) as client:
        allocation_ids = {}
        for owner in ("alice", "bob"):
            response = client.post("/v1/allocations", headers=headers(owner), json={
                "owner": owner, "request": {"cpu_millicores": 1000, "memory_mib": 1000,
                                             "gpu": {"count": 0}},
            })
            assert response.status_code == 202, response.text
            allocation_ids[owner] = response.json()["id"]
        original = make_policy("B2", max_candidates=1).plan(kernel.decision_snapshot())
        assert {c.owner_id for c in original.candidates} == {"alice"}
        secret = "bob-private-demand-and-priority-98765"
        selected = original.candidates[0].model_copy(update={
            "scores": (m.ScoreComponent(name="competition", value=98765, explanation=secret),),
        })
        excluded = selected.model_copy(update={
            "candidate_id": "own-excluded", "feasibility": "infeasible",
            "violations": (m.ConstraintViolation(constraint="owner", detail=secret,
                entity=m.EntityRef(kind="allocation", entity_id=allocation_ids["bob"])),),
            "preconditions": (m.Precondition(kind="owner",
                entity=m.EntityRef(kind="allocation", entity_id=allocation_ids["bob"]),
                expected_value=secret),),
        })
        # Candidate owner labels cannot authorize a reference to Bob's allocation.
        forged_owner = selected.model_copy(update={
            "candidate_id": "forged-owner", "allocation_id": allocation_ids["bob"],
            "demand_id": allocation_ids["bob"],
        })
        plan = original.model_copy(update={
            "candidates": (selected, excluded, forged_owner),
            "policy": original.policy.model_copy(update={"uri": "https://private.invalid/" + secret}),
            "model": original.model.model_copy(update={"uri": "https://private.invalid/" + secret}),
            "rationale": secret, "tie_break_rule": secret,
            "coverage": m.SearchCoverage(scope="partial", domain_description=secret,
                examined_count=3, domain_size=98765, truncation_reason=secret),
        })
        receipt = kernel.commit(m.CommandEnvelope(
            request_id="commit-private-explanation", operation="decision.commit",
            actor=m.AuthenticatedActor(actor_id="policy", role="system"),
            expected_version=plan.snapshot_version,
            parameters=m.DecisionCommitParameters(kind="decision.commit", plan=plan),
        ))
        assert receipt.outcome == "committed", receipt.error
        state_before_reads = kernel.store.read()
        path = f"/x/v1/decisions/{plan.decision_id}"
        response = client.get(path, headers=headers("alice"))
        assert response.status_code == 200, response.text
        own = response.json()["item"]
        assert secret not in response.text and "98765" not in response.text
        assert allocation_ids["bob"] not in response.text
        assert {c["candidate_id"] for c in own["candidates"]} == {
            selected.candidate_id, excluded.candidate_id,
        }
        assert own["coverage"]["examined_count"] == 2
        assert own["coverage"]["domain_size"] is None
        assert own["selected_candidate_id"] == selected.candidate_id
        violation = own["candidates"][1]["violations"][0]
        assert violation["constraint"] == "owner" and violation["entity"] is None
        assert client.get(path, headers=headers("bob")).status_code == 404
        admin = client.get(path, headers=headers("admin")).json()["item"]
        assert admin == state_before_reads["decisions"][plan.decision_id]
        assert secret in json.dumps(admin)
        assert kernel.store.read() == state_before_reads
