"""Representation and local semantic boundaries, not kernel or HTTP acceptance."""

from __future__ import annotations

import copy
import json
import runpy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from campus_fabric import contracts as c

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "contracts/examples"
MANIFEST = json.loads((EXAMPLES / "manifest.json").read_text())
SCHEMA = json.loads((ROOT / "contracts/generated/contracts.schema.json").read_text())


@pytest.fixture
def catalog() -> dict:
    return json.loads((EXAMPLES / "catalog.valid.json").read_text())


def validate(contract: str, data: dict):
    return getattr(c, contract).model_validate_json(json.dumps(data))


@pytest.mark.parametrize("example", MANIFEST["examples"], ids=lambda item: item["path"])
def test_manifest_examples(example: dict) -> None:
    raw = (EXAMPLES / example["path"]).read_text()
    model = getattr(c, example["contract"])
    schema = copy.deepcopy(SCHEMA)
    if example["contract"] != "ContractCatalog":
        schema = {"$defs": schema["$defs"], "$ref": f"#/$defs/{example['contract']}"}
    errors = list(Draft202012Validator(schema).iter_errors(json.loads(raw)))
    if example["valid"]:
        assert not errors
        instance = model.model_validate_json(raw)
        assert model.model_validate_json(instance.model_dump_json()) == instance
    else:
        with pytest.raises(ValidationError, match=example["expected_error"]):
            model.model_validate_json(raw)
        if example["validation_layer"] == "json_schema_and_pydantic":
            assert errors
        else:
            assert not errors, "semantic examples should expose the JSON Schema validation boundary"


def test_schema_export_is_deterministic_and_current() -> None:
    exporter = runpy.run_path(str(ROOT / "scripts/export_contracts.py"))
    expected = exporter["schema_text"]()
    assert exporter["schema_text"]() == expected
    assert (ROOT / "contracts/generated/contracts.schema.json").read_text() == expected
    Draft202012Validator.check_schema(json.loads(expected))
    assert set(SCHEMA["required"]) == set(c.ContractCatalog.model_fields)


@pytest.mark.parametrize("field", ["epoch", "revision"])
@pytest.mark.parametrize("bad", [-1, True, "1", 1.5, c.MAX_SAFE_INTEGER + 1])
def test_versions_reject_unsafe_or_coerced_values(field: str, bad: object) -> None:
    values = {"epoch": 0, "revision": 0, field: bad}
    with pytest.raises(ValidationError):
        c.VersionStamp.model_validate(values)


def test_epoch_is_not_comparable_as_revision_only() -> None:
    old_epoch = c.VersionStamp(epoch=1, revision=999)
    reset = c.VersionStamp(epoch=2, revision=0)
    assert not old_epoch.is_same_epoch_at_or_before(reset)
    assert c.VersionStamp(epoch=2, revision=0).is_same_epoch_at_or_before(reset)


@pytest.mark.parametrize("field", ["cpu_millicores", "memory_mib", "ephemeral_storage_mib"])
@pytest.mark.parametrize("bad", [-1, True, "4", 1.25, c.MAX_SAFE_INTEGER + 1])
def test_resource_quantities_are_bounded_strict_integers(
    catalog: dict, field: str, bad: object
) -> None:
    request = catalog["command_envelope"]["parameters"]["request"]
    request[field] = bad
    with pytest.raises(ValidationError):
        validate("ResourceRequest", request)


def test_valid_zero_and_upper_resource_bound() -> None:
    model = c.ResourceAmounts(cpu_millicores=0, memory_mib=c.MAX_SAFE_INTEGER)
    assert model.cpu_millicores == 0
    assert model.memory_mib == c.MAX_SAFE_INTEGER
    assert c.GPURequest(count=16).count == 16
    with pytest.raises(ValidationError):
        c.GPURequest(count=17)


def test_nested_contracts_and_containers_are_immutable(catalog: dict) -> None:
    model = validate("ContractCatalog", catalog)
    with pytest.raises(ValidationError, match="frozen"):
        model.decision_snapshot.now = 500.0
    with pytest.raises(ValidationError, match="frozen"):
        model.decision_snapshot.inventory[0].capacity.cpu_millicores = 500
    assert isinstance(model.decision_snapshot.inventory, tuple)
    assert isinstance(model.decision_plan.candidates, tuple)
    assert isinstance(model.command_envelope.parameters.node_selector, tuple)
    with pytest.raises(TypeError):
        model.decision_plan.candidates[0] = model.decision_plan.candidates[0]
    catalog["decision_snapshot"]["inventory"][0]["capacity"]["cpu_millicores"] = 999
    assert model.decision_snapshot.inventory[0].capacity.cpu_millicores == 8000


def test_python_input_requires_tuples_and_json_arrays_are_supported() -> None:
    with pytest.raises(ValidationError, match="tuple"):
        c.JobCreateParameters(kind="job.create", allocation_id="a", image="python", command=["run"])
    result = c.JobCreateParameters.model_validate_json(
        '{"kind":"job.create","allocation_id":"a","image":"python","command":["run"]}'
    )
    assert result.command == ("run",)


def test_extra_fields_are_rejected_at_nested_boundaries(catalog: dict) -> None:
    catalog["decision_snapshot"]["demands"][0]["hidden_completion_time"] = 123.0
    with pytest.raises(ValidationError, match="extra_forbidden"):
        validate("ContractCatalog", catalog)


@pytest.mark.parametrize("role", ["researcher", "contributor", "administrator", "system"])
def test_test_control_role_is_separate(role: str) -> None:
    data = {
        "request_id": "request-reset",
        "actor": {"actor_id": "actor", "owner_id": "owner", "role": role},
        "operation": "test.reset",
        "parameters": {"kind": "test.reset"},
    }
    with pytest.raises(ValidationError, match="isolated test driver"):
        validate("CommandEnvelope", data)
    data["actor"]["role"] = "test_driver"
    assert validate("CommandEnvelope", data).operation == "test.reset"


def test_command_operation_and_payload_must_agree(catalog: dict) -> None:
    command = catalog["command_envelope"]
    command["operation"] = "job.create"
    with pytest.raises(ValidationError, match="typed parameter kind"):
        validate("CommandEnvelope", command)


def test_decision_commit_requires_exact_snapshot_version(catalog: dict) -> None:
    command = {
        "request_id": "request-decision",
        "actor": {"actor_id": "scheduler", "role": "system"},
        "operation": "decision.commit",
        "parameters": {"kind": "decision.commit", "plan": catalog["decision_plan"]},
        "expected_version": {"epoch": 1, "revision": 8},
    }
    with pytest.raises(ValidationError, match="expected version"):
        validate("CommandEnvelope", command)
    command["expected_version"]["revision"] = 7
    assert validate("CommandEnvelope", command).expected_version.revision == 7


@pytest.mark.parametrize("bad", [-0.1, float("inf"), float("-inf"), float("nan"), "2", True])
def test_logical_time_is_finite_nonnegative_and_not_coerced(bad: object) -> None:
    with pytest.raises(ValidationError):
        c.EvidenceWindow(start=bad, end=100.0)


@pytest.mark.parametrize("field", ["arrived_at", "available_at", "observed_at", "issued_at"])
def test_snapshots_reject_future_facts(catalog: dict, field: str) -> None:
    snapshot = catalog["decision_snapshot"]
    if field == "arrived_at":
        snapshot["demands"][0][field] = 21.0
    elif field == "available_at":
        snapshot["demands"][0]["known_data"][0][field] = 21.0
    elif field == "observed_at":
        snapshot["inventory"][0][field] = 21.0
    else:
        snapshot["notices"][0][field] = 21.0
    with pytest.raises(ValidationError, match="appeared|causal cutoff"):
        validate("DecisionSnapshot", snapshot)


def test_known_future_deadline_is_available_information(catalog: dict) -> None:
    snapshot = validate("DecisionSnapshot", catalog["decision_snapshot"])
    assert snapshot.notices[0].issued_at <= snapshot.now < snapshot.notices[0].deadline


def test_partial_search_can_explain_but_cannot_claim_pool_infeasible(catalog: dict) -> None:
    plan = catalog["decision_plan"]
    plan["coverage"].update(scope="partial", domain_size=10, truncation_reason="candidate limit")
    assert validate("DecisionPlan", plan).coverage.scope == "partial"
    plan["asserts_pool_infeasible"] = True
    with pytest.raises(ValidationError, match="complete domain"):
        validate("DecisionPlan", plan)


def test_complete_exclusion_requires_each_candidate_violation(catalog: dict) -> None:
    plan = catalog["decision_plan"]
    plan["candidates"][0].update(feasibility="infeasible", violations=[])
    plan["selected_candidate_id"] = None
    plan["asserts_pool_infeasible"] = True
    with pytest.raises(ValidationError, match="violated constraint"):
        validate("DecisionPlan", plan)
    plan["candidates"][0]["violations"] = [
        {"constraint": "gpu_memory", "detail": "Insufficient VRAM."}
    ]
    assert validate("DecisionPlan", plan).asserts_pool_infeasible


def test_selection_must_refer_to_recorded_legal_candidate(catalog: dict) -> None:
    plan = catalog["decision_plan"]
    plan["selected_candidate_id"] = "missing"
    with pytest.raises(ValidationError, match="recorded legal candidate"):
        validate("DecisionPlan", plan)


def test_attempt_generations_keep_one_intent_and_distinct_attempts(catalog: dict) -> None:
    intent = catalog["work_intent"]
    attempt = {
        "ref": {
            "intent_id": "intent-1",
            "attempt_id": "attempt-1",
            "generation": 1,
            "allocation_id": "allocation-1",
        },
        "state": "waiting",
        "created_at": 1.0,
    }
    intent["attempts"] = [attempt]
    assert len(validate("WorkIntent", intent).attempts) == 1
    duplicate_generation = copy.deepcopy(attempt)
    duplicate_generation["ref"]["attempt_id"] = "attempt-2"
    intent["attempts"].append(duplicate_generation)
    with pytest.raises(ValidationError, match="attempt generations must be unique"):
        validate("WorkIntent", intent)
    duplicate_generation["ref"]["generation"] = 2
    duplicate_generation["ref"]["intent_id"] = "other-intent"
    with pytest.raises(ValidationError, match="stable intent"):
        validate("WorkIntent", intent)


def test_failed_job_stage_preserves_allocation_partial_success(catalog: dict) -> None:
    intent = validate("WorkIntent", catalog["work_intent"])
    assert intent.allocation_ids == ("allocation-1",)
    assert intent.job_ids == ()
    assert [item.state for item in intent.stages] == ["committed", "failed"]
    assert intent.stages[1].receipt.error.code == "lease_unavailable"


def test_durable_stage_receipt_precedes_success(catalog: dict) -> None:
    intent = catalog["work_intent"]
    intent["stages"][0]["receipt"] = None
    with pytest.raises(ValidationError, match="durable receipt"):
        validate("WorkIntent", intent)


def test_registration_persists_inputs_and_cannot_backdate_arrival() -> None:
    command = json.loads((EXAMPLES / "workflow_register.valid.json").read_text())
    registered = validate("CommandEnvelope", command)
    assert registered.parameters.registration.specification.job.command == ("python", "main.py")
    command["parameters"]["registration"]["first_arrival_at"] = -100.0
    with pytest.raises(ValidationError, match="extra_forbidden"):
        validate("CommandEnvelope", command)


def test_workflow_commands_require_version_and_authenticated_owner() -> None:
    command = json.loads((EXAMPLES / "workflow_register.valid.json").read_text())
    command["expected_version"] = None
    with pytest.raises(ValidationError, match="expected version"):
        validate("CommandEnvelope", command)
    command["expected_version"] = {"epoch": 1, "revision": 0}
    command["actor"]["owner_id"] = "owner-bob"
    with pytest.raises(ValidationError, match="authenticated actor owner"):
        validate("CommandEnvelope", command)


def test_stored_specification_cannot_switch_owner(catalog: dict) -> None:
    intent = catalog["work_intent"]
    intent["specification"]["allocation"]["owner_id"] = "owner-bob"
    with pytest.raises(ValidationError, match="stored work specification"):
        validate("WorkIntent", intent)


def test_ready_report_is_not_verified_or_applied() -> None:
    checkpoint = c.CheckpointFact(checkpoint_id="cp", report_status="ready", reported_at=1.0)
    assert checkpoint.artifact_uri is None
    assert checkpoint.verification_status == "unverified"
    assert checkpoint.continuation_status == "not_attempted"
    with pytest.raises(ValidationError, match="verified artifacts"):
        c.CheckpointFact(
            checkpoint_id="cp",
            report_status="ready",
            reported_at=1.0,
            verification_status="verified",
        )
    with pytest.raises(ValidationError, match="applied continuation"):
        c.CheckpointFact(
            checkpoint_id="cp",
            report_status="ready",
            reported_at=1.0,
            continuation_status="applied",
            resumed_attempt_id="attempt-2",
        )


@pytest.mark.parametrize("sequence", [8, 9])
def test_event_cause_must_be_an_earlier_event(catalog: dict, sequence: int) -> None:
    event = catalog["committed_event"]
    event["cause"] = {
        "kind": "event",
        "event_id": "event-cause",
        "sequence": sequence,
        "version": {"epoch": 1, "revision": 7},
    }
    with pytest.raises(ValidationError, match="precede"):
        validate("CommittedEvent", event)


def test_read_snapshot_rejects_cross_owner_data_and_epoch(catalog: dict) -> None:
    read = catalog["read_snapshot"]
    read["allocations"][0]["owner_id"] = "owner-bob"
    with pytest.raises(ValidationError, match="another owner's"):
        validate("ReadSnapshot", read)
    read["global_scope"] = True
    with pytest.raises(ValidationError, match="administrator"):
        validate("ReadSnapshot", read)
    read["viewer"]["role"] = "administrator"
    assert validate("ReadSnapshot", read).global_scope
    read["version"]["epoch"] = 2
    with pytest.raises(ValidationError, match="snapshot epoch"):
        validate("ReadSnapshot", read)


def test_read_projection_rejects_unavailable_intent_times(catalog: dict) -> None:
    read = catalog["read_snapshot"]
    read["now"] = 19.0
    with pytest.raises(ValidationError):
        validate("ReadSnapshot", read)


def test_mig_instance_has_a_distinct_parent_identity() -> None:
    with pytest.raises(ValidationError, match="distinct physical parent"):
        c.GPUDevice(
            device_id="gpu-1",
            physical_gpu_id="gpu-1",
            model="A100",
            memory_mib=20480,
            profile="mig-2g.20gb",
            state="available",
        )
    instance = c.GPUDevice(
        device_id="gpu-1/mig-1",
        physical_gpu_id="gpu-1",
        model="A100",
        memory_mib=20480,
        profile="mig-2g.20gb",
        state="available",
    )
    assert instance.physical_gpu_id == "gpu-1"


def test_wall_clock_driver_requires_tick_identity() -> None:
    with pytest.raises(ValidationError, match="tick identity"):
        c.ClockAdvanceParameters(
            kind="clock.advance", advance_seconds=1.0, source="wall_clock_driver"
        )
    assert (
        c.ClockAdvanceParameters(
            kind="clock.advance", advance_seconds=1.0, source="manual"
        ).driver_tick_id
        is None
    )


def test_simulation_cannot_claim_measured_cpu_execution() -> None:
    attempt = c.AttemptRef(
        intent_id="intent", attempt_id="attempt", generation=1, allocation_id="allocation"
    )
    with pytest.raises(ValidationError, match="measured physical"):
        c.ExecutorEvent(
            callback_id="callback",
            version=c.VersionStamp(epoch=1, revision=1),
            attempt=attempt,
            kind="stopped",
            mode="logical_simulation",
            observed_at=5.0,
            physical_elapsed_seconds=5.0,
        )


def test_unexecuted_evidence_stays_not_run(catalog: dict) -> None:
    evidence = validate("EvidenceRun", catalog["evidence_run"])
    assert evidence.status == "not_run"
    assert evidence.outputs == ()
    assert all(check.status == "NOT_RUN" for check in evidence.checks)
    with pytest.raises(ValidationError, match="actual result"):
        c.EvidenceCheck(target_id="T-CONTRACT", status="MET", expected="All HTTP operations pass.")


def test_runtime_ports_do_not_implement_mutation() -> None:
    for port in (
        c.CommandPort,
        c.SnapshotPort,
        c.ReadPort,
        c.PolicyPort,
        c.ExecutorPort,
        c.WorkflowPort,
    ):
        assert port._is_protocol
