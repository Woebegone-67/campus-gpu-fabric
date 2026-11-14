"""Independent joint-gate attack fixtures, never actual human acceptance evidence."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from campus_fabric.contracts import models as m
from campus_fabric.workflows.acceptance_evidence import checkpoint_checks
from campus_fabric.workflows.devhost import create_app

ROOT = Path(__file__).resolve().parents[2]


def load_gate(root):
    spec = importlib.util.spec_from_file_location("joint_security_gate", root / "scripts/check_checkpoint.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def joint_fixture(tmp_path):
    """Synthetic schema fixtures use isolated files; no product outcome is claimed."""
    registry = json.loads((ROOT / "configs/checkpoints.json").read_text())
    paths = ["configs/checkpoints.json", "scripts/check_checkpoint.py"]
    paths += [row["validator_path"] for row in registry["checkpoints"]["CP2"]["required_checks"]]
    for relative in paths:
        dest = tmp_path / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, dest)
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=Parser fixture",
                    "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty",
                    "-m", "Isolated fixture only"], check=True, capture_output=True)
    gate = load_gate(tmp_path)
    build = gate.current_build(tmp_path)
    base = tmp_path / "evidence/cp2/b"
    base.mkdir(parents=True)
    artifact = base / "synthetic-observation.txt"
    artifact.write_text("Parser-fixture text only; no actual UI or human acceptance took place.\n")
    reference = {"path": str(artifact.relative_to(tmp_path)), "sha256": gate.sha256_file(artifact)}
    now = datetime.now(timezone.utc).isoformat()
    workspaces = ["run", "resources", "continuity", "decisions", "events", "acceptance"]
    observation = {"status": "PASS", "actual": "Synthetic schema fixture only, not real observed behavior.",
                   "evidence": [reference]}
    ui = {"build": build, "observed_at": now, "observer_kind": "agent", "observer_ids": ["fixture"],
          "workspaces": workspaces, "roles": ["researcher", "contributor", "administrator"],
          "checks": [{**observation, "id": identity} for identity in [
              "three-role-recall-resume", "checkpoint-truth-and-waiting", "contributor-isolation",
              "real-cpu-artifact", "mode-owner-and-experiment-isolation"]]}
    humans = {"build": build, "observed_at": now, "operators": [
        {**observation, "observer_kind": "human", "human_id": "fixture-" + role,
         "role": role, "workspaces": workspaces} for role in ["A", "B"]]}
    (base / "ui_receipt.json").write_text(json.dumps(ui))
    (base / "human_handoff.json").write_text(json.dumps(humans))
    summary_path = tmp_path / "evidence/cp2/six_workspace_receipt.json"
    summary = {"check": "CP2-SIX-WORKSPACES", "status": "PASS", "build": build,
               "scope": "JOINT_CP2_UI_EVIDENCE_REVIEW", "observed_at": now}
    summary_path.write_text(json.dumps(summary))
    checks = []
    for requirement in registry["checkpoints"]["CP2"]["required_checks"]:
        result = summary_path if requirement["id"] == "CP2-SIX-WORKSPACES" else artifact
        checks.append({"id": requirement["id"], "target_id": requirement["target_id"],
            "status": "MET", "build": build, "expected": requirement["expected"],
            "evidence_kind": requirement["allowed_evidence_kinds"][0],
            "validator": {"path": requirement["validator_path"],
                          "sha256": gate.sha256_file(tmp_path / requirement["validator_path"])},
            "input_version": "isolated-parser-fixture", "executor": "schema-parser-regression",
            "command": "synthetic fixture only", "actual": "Parser shape accepted; not real acceptance.",
            "observed_at": now, "unresolved_issues": [],
            "evidence": [{"path": result.relative_to(tmp_path).as_posix(), "sha256": gate.sha256_file(result)}]})
    receipt = {"schema_version": "1.0.0", "checkpoint": "CP2", "scope": "runtime_acceptance",
               "status": "MET", "build": build, "checks": checks}
    (tmp_path / "evidence/cp2/receipt.json").write_text(json.dumps(receipt))
    return tmp_path, gate, receipt, summary, humans


def save_summary(root, gate, receipt, summary):
    target = root / "evidence/cp2/six_workspace_receipt.json"
    target.write_text(json.dumps(summary))
    next(c for c in receipt["checks"] if c["id"] == "CP2-SIX-WORKSPACES")["evidence"][0]["sha256"] = gate.sha256_file(target)
    (root / "evidence/cp2/receipt.json").write_text(json.dumps(receipt))


def test_complete_synthetic_joint_schema_can_be_validated(joint_fixture):
    root, gate, _, _, _ = joint_fixture
    # Parser-level positive control only; the fixture is not a product receipt.
    assert gate.evaluate_checkpoint(root, "CP2")["status"] == "MET"


def test_agent_summary_cannot_be_wrapped_as_formal_joint_acceptance(joint_fixture):
    root, gate, receipt, summary, _ = joint_fixture
    summary["scope"] = "AGENT_UI_EVIDENCE_ONLY"
    save_summary(root, gate, receipt, summary)
    (root / "evidence/cp2/b/human_handoff.json").unlink()
    report = gate.evaluate_checkpoint(root, "CP2")
    assert report["status"] == "NOT_MET"
    assert any("agent QA is insufficient" in error for error in report["errors"])
    assert any("human_handoff" in error for error in report["errors"])
    checks, _ = checkpoint_checks(root)
    assert next(c for c in checks if c.expected.startswith("CP2-SIX-WORKSPACES:")).status == "NOT_MET"


@pytest.mark.parametrize("mutation", ["missing", "stale", "agent", "same_person", "corrupt_artifact"])
def test_joint_summary_rechecks_underlying_humans_and_artifacts(joint_fixture, mutation):
    root, gate, _, _, humans = joint_fixture
    human_path = root / "evidence/cp2/b/human_handoff.json"
    if mutation == "missing":
        human_path.unlink()
    elif mutation == "corrupt_artifact":
        (root / "evidence/cp2/b/synthetic-observation.txt").write_text("Changed independently of the summary.\n")
    else:
        humans = copy.deepcopy(humans)
        if mutation == "stale":
            humans["build"]["git_commit"] = "old-build"
        elif mutation == "agent":
            humans["operators"][1]["observer_kind"] = "agent"
        else:
            humans["operators"][1]["human_id"] = humans["operators"][0]["human_id"]
        human_path.write_text(json.dumps(humans))
    assert gate.evaluate_checkpoint(root, "CP2")["status"] == "NOT_MET"


@pytest.mark.parametrize("mutation", ["failed", "stale", "wrong_check", "empty"])
def test_formal_summary_itself_must_pass_for_current_build(joint_fixture, mutation):
    root, gate, receipt, summary, _ = joint_fixture
    summary = copy.deepcopy(summary)
    if mutation == "failed":
        summary["status"] = "NOT_MET"
    elif mutation == "stale":
        summary["build"]["source_sha256"] = "old-source"
    elif mutation == "wrong_check":
        summary["check"] = "CP2-CPU-REFERENCE"
    else:
        summary = {}
    save_summary(root, gate, receipt, summary)
    assert gate.evaluate_checkpoint(root, "CP2")["status"] == "NOT_MET"


def test_policy_p_live_queries_preserve_owner_scope_through_contributor_recall(tmp_path, monkeypatch):
    monkeypatch.setenv("FABRIC_B_POLICY", "P")
    actors = {name: m.AuthenticatedActor(actor_id=name, owner_id=name, role=role)
              for name, role in [("alice", "researcher"), ("bob", "researcher"),
                                 ("supplier", "contributor"), ("outsider", "contributor")]}
    actors["admin"] = m.AuthenticatedActor(actor_id="admin", role="administrator")
    resources = m.ResourceAmounts(cpu_millicores=4000, memory_mib=8192,
                                 gpu_count=2, gpu_memory_mib=16384)
    node = m.NodeInventory(node_id="shared-public-node", contributor_owner_id="supplier", state="Ready",
        observed_at=0., capacity=resources, available=resources,
        devices=tuple(m.GPUDevice(device_id=f"gpu-{i}", physical_gpu_id=f"gpu-{i}",
                                 model="reference", memory_mib=8192, state="available") for i in range(2)))
    app = create_app(data_dir=tmp_path, inventory=(node,), worker_enabled=False,
                     scheduling_mode="external", auth_tokens=actors,
                     simulation_duration_seconds=100)

    def headers(owner):
        return {"Authorization": "Bearer " + owner,
                **({"X-Owner": owner} if owner != "admin" else {})}

    with TestClient(app) as client:
        for owner in ("alice", "bob"):
            registration = m.WorkIntentRegistration(intent_id="private-work-" + owner, owner_id=owner,
                execution_mode="logical_simulation", settlement_action="keep_lease",
                specification=m.WorkSpecification(allocation=m.AllocationCreateParameters(
                    kind="allocation.create", owner_id=owner, preemption_mode="checkpoint",
                    request=m.ResourceRequest(cpu_millicores=1000, memory_mib=1024,
                                              gpu=m.GPURequest(count=1))),
                    job=m.JobSpecification(image="private-image-" + owner, command=("not-executed",))))
            result = client.post("/x/v1/work-intents", headers=headers(owner),
                                 json=registration.model_dump(mode="json"))
            assert result.status_code == 202, result.text
        app.state.workflow_worker.tick()
        app.state.workflow_worker.tick()
        own = client.get("/x/v1/work-intents/private-work-alice", headers=headers("alice")).json()
        assert own["item"]["state"] == "running"
        grant = next(e for e in own["events"] if e["event_type"] == "allocation.granted")
        decision_url = "/x/v1/decisions/" + grant["decision_id"]
        raw = client.get(decision_url, headers=headers("admin"))
        assert raw.status_code == 200 and {c["owner_id"] for c in raw.json()["item"]["candidates"]} == {"alice", "bob"}
        for path in ("/v1/allocations", "/v1/jobs", "/v1/events", "/x/v1/work-intents", decision_url):
            value = client.get(path, headers=headers("alice"))
            assert value.status_code == 200, value.text
            assert "private-work-bob" not in value.text and "private-image-bob" not in value.text
            assert value.headers["Cache-Control"] == "no-store"
        assert client.get("/x/v1/work-intents/private-work-bob", headers=headers("alice")).status_code == 404
        assert client.get(decision_url, headers=headers("supplier")).status_code == 404
        assert client.get("/x/v1/contributions", headers=headers("alice")).status_code == 403
        for actor in ("alice", "bob", "supplier", "outsider"):
            for path in ("/x/v1/acceptance", "/x/v1/experiments", "/x/v1/admin/overview"):
                spoof = {**headers(actor), "X-Role": "administrator"}
                assert client.get(path + "?global_scope=true", headers=spoof).status_code == 403
        assert client.get("/x/v1/contributions", headers=headers("outsider")).json()["items"] == []
        response = client.post("/x/v1/contributions/recall", headers=headers("supplier"),
            json={"gpu_ids": ["gpu-0", "gpu-1"], "notice_seconds": 5, "duration_seconds": 10})
        assert response.status_code == 201, response.text
        for owner in ("alice", "bob"):
            events = client.get("/v1/events", headers=headers(owner)).json()["items"]
            assert {event["owner_id"] for event in events} == {owner}
            assert any(event["event_type"] == "allocation.preempt_requested" for event in events)
        assert client.get("/v1/jobs", headers=headers("supplier")).json()["items"] == []
        assert client.get("/v1/allocations", headers=headers("supplier")).json()["items"] == []
