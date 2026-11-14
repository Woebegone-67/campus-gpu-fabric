"""Strict read-only bindings for inherited reliability evidence.

These checks reject substituted summaries and recompute retained CPU arithmetic;
they do not authenticate the producer of an observation or execute workloads.
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

CP2_SCOPE = "Individual CP2 check only; no joint UI or complete CP2 certification"
SELECTIONS = {
    "clock-boundaries": [
        "test_large_advance_equals_every_boundary_with_recall_retry_and_expiry",
        "test_expiry_beats_recall_and_completion_at_same_instant",
        "test_expiry_beats_due_retry_without_charging_nonexistent_attempt",
        "test_total_job_timeout_beats_retry_and_does_not_restart_timeout",
        "test_cpu_protocol_clock_and_unknown_observation_cannot_fabricate_stop",
        "test_cpu_protocol_timeout_and_recall_stop_complete_in_both_orders",
    ],
    "reset-isolation": [
        "test_reset_fences_old_epoch_callback_even_after_new_resources_and_job",
        "test_cpu_protocol_reset_quarantine_cannot_be_bypassed_by_renaming_node",
        "test_stale_attempt_cannot_complete_or_fail_replacement",
        "test_restart_from_disk_preserves_pending_resume_and_its_deadline",
    ],
}
CPU_CASES = {
    "ordinary": {"seed": 17, "total_steps": 100_003, "batch_steps": 997},
    "uint64-edge": {"seed": 2**64 - 1, "total_steps": 65_537, "batch_steps": 509},
    "zero-seed": {"seed": 0, "total_steps": 131_071, "batch_steps": 1237},
}
CHECK_IDS = {
    "official-http": {
        "test-control-authority", "checkpoint-resume-lease", "owner-isolation",
        "node-state-no-revival", "contributor-product-authority", "fault-statuses-no-side-effects",
        "fault-concurrent-count", "fault-sigkill-persistence", "fault-route-template",
        "fault-latency-clock", "reset-clears-faults", "all-21-operation-journeys"},
    "preemption": {"ready-pending-resume-budget", "failed", "timeout", "exact-deadline",
                   "overlapping-recall-union", "expiry-before-recall-timeout", "notice-blocks-new-work"},
    "node-failure": {"draining-preserves-existing", "node-generation-no-revival", "ready-keeps-recall-block"},
    "crash-recovery": {*("launch-" + p for p in ("reserved", "after_launch", "delayed_launch", "inside_launch")),
                       "saving-control-and-resume-crashes", "saving-unpublished-artifact",
                       *(f"transaction-{op}-{when}" for op in ("allocation", "workflow_allocation", "workflow_job")
                         for when in ("before_commit", "after_commit"))},
    "cpu-equivalence": {f"{case}:{check}" for case in CPU_CASES for check in (
        "two-real-resumes", "three-way-equality", "settled-persistence-isolation")},
    "independent-model": {"oracle-controls", "deadline-and-resource-boundaries",
                          "generated-sequential-histories", "real-concurrent-linearizable-histories"},
    "prefix-replay": {*(f"{p}-online-prefix" for p in ("B0", "B1", "B2", "P")),
                      "independent-resource-event-fold", "full-final-state-replay"},
    "decision-coverage": {f"{policy}-{case}" for policy in ("B0", "B1", "B2", "P")
                          for case in ("accepted", "budget", "stale", "illegal-placement")},
    "overview": {f"{mode}:{check}" for mode in ("logical_simulation", "cpu_reference") for check in (
        "two-owner-snapshot", "fifty-consecutive-read-only-overviews", "nonadmin-denied")},
}
SCOPES = {
    "official-http": "A_CP2_LOOPBACK_HTTP_ONLY",
    "preemption": CP2_SCOPE, "node-failure": CP2_SCOPE,
    "clock-boundaries": CP2_SCOPE, "reset-isolation": CP2_SCOPE,
    "crash-recovery": "actual_CPU_executor_and_kernel_crash_windows_not_UI_or_GPU",
    "cpu-equivalence": "A_CP3_REAL_CPU_EQUIVALENCE_HTTP_ONLY",
    "overview": "SUPPLEMENTAL_BACKEND_HTTP_REGRESSION",
    "independent-model": {
        "oracle": "Standalone anonymous resource model with unit time ticks; no production placement, counters or event reducer reused",
        "fixture": "One homogeneous two-GPU node; CPU=4, RAM=8, disk=6, each A10 GPU VRAM=8",
        "executor": "Built-in scheduler and logical simulation, separate real SQLite connections in worker threads",
        "semantics": "Owner isolation, request replay/conflict, four-dimensional capacity, GPU model/VRAM, grant-based TTL, timeout/completion precedence, cancellation, release, node failures, restart persistence",
        "linearizability": "Exhaustive search over observed operations preserving invocation/response precedence and matching final normalized snapshot; source revision not used as oracle",
        "identity": "Creation replies bind opaque IDs only; all states, resource amounts and deadlines are calculated independently",
        "limits": "Bounded tested histories only; not a universal linearizability proof, multi-node/MIG proof, HTTP acceptance or CPU executor continuity validation",
        "counterexamples": "Unmodified failing operation history and traceback retained; not claimed minimized",
    },
    "prefix-replay": {
        "online": "Actual B0/B1/B2/P plans submitted to isolated A kernels; only the current DecisionSnapshot reaches policy.plan.",
        "prefix": "Repeated production runs establish nonanticipation under the declared attacks, not independent state replay.",
        "replay": "Independent standard-library committed-state journal reducer reconstructs all persisted JSON and compares each write prefix against observed SQL; separate public-event fold checks resource claims.",
        "execution": "Logical simulation with fixed instance-wide hidden duration; no real CPU/GPU claim.",
    },
    "decision-coverage": {
        "transport": "Real loopback TCP product registration and decision/capacity queries; not TestClient.",
        "acceptance": "Product worker submits plans through the unchanged A command port; receipt and before/after snapshot captured by test-only assembly.",
        "fault_injection": "Test policy adapter proposes a stale version or illegal placement; no new HTTP endpoint or production override.",
        "limits": "Finite fixtures for B0/B1/B2/P; candidate-budget declarations, actual grant and rejection integrity, and owner isolation. Browser rendering and exhaustive domain completeness are not certified.",
    },
}
CHILD_IDENTITIES = {
    "preemption": ("check_id", "CP2-PREEMPTION"),
    "node-failure": ("check_id", "CP2-NODE-FAILURE"),
    "clock-boundaries": ("check_id", "CP2-CLOCK-BOUNDARIES"),
    "reset-isolation": ("check_id", "CP2-RESET-ISOLATION"),
    "crash-recovery": ("check", "CP3-CRASH-RECOVERY"),
    "cpu-equivalence": ("check", "CP3-CPU-EQUIVALENCE"),
    "independent-model": ("check", "CP3-INDEPENDENT-MODEL"),
    "prefix-replay": ("check", "CP3-PREFIX-REPLAY"),
    "decision-coverage": ("check", "CP3-DECISION-COVERAGE"),
    "overview": ("check", "CP3-OVERVIEW-SNAPSHOT"),
}


def check_set(rows, expected):
    assert isinstance(rows, list) and len(rows) == len(expected), "Required checks missing or duplicated"
    assert {row["id"] for row in rows} == expected, "Required check identities differ"
    assert all(row["status"] == "PASS" for row in rows), "A required check failed"
    return {row["id"]: row for row in rows}


def validate_binding(row, child, build, relative, options, *, root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parents[3]
    name = row["id"]
    expected_receipt = f"evidence/cp4/reliability-{name}.json"
    assert row["receipt"]["path"] == expected_receipt, "A different child receipt was substituted"
    command = row.get("command", [])
    assert len(command) == 4 + len(options), "Missing or extra validator arguments"
    assert command[0] and Path(command[1]).resolve() == (root / "tests/acceptance" / relative).resolve(), "Validator is not from the bound source tree"
    assert command[2] == "--output" and Path(command[3]).resolve() == (root / expected_receipt).resolve()
    assert command[4:] == list(options), "Declared validator parameters were changed or omitted"
    assert row["exit_code"] == 0 and child["status"] == "PASS", "Child run did not pass"
    assert child["build"] == child["build_at_finish"] == build, "Child build differs"
    assert not child.get("failure") and not child.get("unresolved_issues"), "Child has unresolved issues"
    if name in CHILD_IDENTITIES:
        field, value = CHILD_IDENTITIES[name]
        assert child.get(field) == value, "Wrong inherited check identity"
    assert child.get("scope") == SCOPES[name], "Wrong inherited evidence scope"
    if name in SELECTIONS:
        expected_tests = ["tests/domain/test_cp2_adversarial.py::" + test for test in SELECTIONS[name]]
        assert child.get("selected_tests") == expected_tests, "Boundary test selection changed"
        assert child.get("command", [])[1:] == ["-m", "pytest", "-q", *expected_tests]
        assert child.get("returncode") == 0
        assert re.search(rf"\b{len(expected_tests)} passed\b", child.get("stdout", "")), "Boundary tests did not all execute"
        assert not child.get("checks"), "Pytest-backed evidence cannot substitute unrelated checks"
    else:
        check_set(child.get("checks"), CHECK_IDS[name])
    if name == "independent-model":
        assert child.get("evidence_kind") == "independent_history"
        assert child.get("configuration") == {
            "seeds": list(range(8)), "steps_per_seed": 80,
            "concurrent_seeds": list(range(8)), "concurrent_operations_per_history": 6}, "Independent model workload was reduced"
        checks = {x["id"]: x for x in child["checks"]}
        assert checks["generated-sequential-histories"]["commands"] == 640
        assert checks["real-concurrent-linearizable-histories"]["commands"] == 48
        assert checks["real-concurrent-linearizable-histories"]["overlapping_pairs"] > 0
        assert len(checks["generated-sequential-histories"]["evidence"]) == 8
        assert len(checks["real-concurrent-linearizable-histories"]["evidence"]) == 8
    if name == "prefix-replay":
        assert child.get("evidence_kind") == "independent_history"
        assert "committed-state journal" in child["scope"].get("replay", "")
    if name == "decision-coverage":
        assert child.get("evidence_kind") == "http_blackbox"


def validate_official(root, child):
    operations = json.loads((root / "contracts/api_ownership.json").read_text())["operations"]
    asserted = {row["operation_id"]: row for row in child["operations"]}
    assert len(asserted) == len(child["operations"]) == len(operations) == 21
    assert set(asserted) == {row["operation_id"] for row in operations}
    for operation in operations:
        pattern = re.escape(operation["path"])
        pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", pattern)
        observed = [row for row in child["history"] if row.get("kind") == "http"
                    and row["method"] == operation["method"] and re.fullmatch(pattern, row["path"])
                    and 200 <= row["status"] < 300]
        summary = asserted[operation["operation_id"]]
        assert observed and summary["status"] == "PASS", "Official operation lacks actual successful HTTP"
        assert summary["successful_request_ids"] == [row["request_id"] for row in observed]


def validate_cpu(child):
    """Recompute the specified recurrence, independent of the matrix test oracle."""
    assert len(child["cases"]) == 3 and {case["name"] for case in child["cases"]} == set(CPU_CASES)
    for case in child["cases"]:
        name, configuration = case["name"], CPU_CASES[case["name"]]
        assert case["configuration"] == {**configuration, "batch_pause_seconds": .03}
        assert not case.get("failure")
        checks = check_set(case["checks"], {f"{name}:{k}" for k in (
            "two-real-resumes", "three-way-equality", "settled-persistence-isolation")})
        resumed = checks[name + ":two-real-resumes"]["observed"]
        equality = checks[name + ":three-way-equality"]["observed"]
        saves = resumed["saves"]
        assert len(saves) == 2 and resumed["result"] == equality["resumed"]
        payloads = [save["public_artifact"] for save in saves] + [equality["resumed"], equality["uninterrupted"]]
        steps = [payload["state"]["step"] for payload in payloads]
        assert 0 < steps[0] < steps[1] < steps[2] == steps[3] == configuration["total_steps"]
        values, value = {0: configuration["seed"]}, configuration["seed"]
        for step in range(configuration["total_steps"]):
            value = (1664525 * value + 1013904223 + step) % 2**64
            if step + 1 in steps:
                values[step + 1] = value
        for payload in payloads:
            state = payload["state"]
            assert type(state["step"]) is int and type(state["value"]) is int
            assert payload["algorithm"] == "lcg64-indexed-v1"
            assert state == {"step": state["step"], "value": values[state["step"]]}, "Retained CPU result differs from independent arithmetic"
            assert any(row.get("kind") == "http" and row.get("status") == 200
                       and row.get("path", "").startswith("/x/v1/artifacts?")
                       and row.get("response_body") == payload for row in case["history"]), "Result absent from retained artifact HTTP"
        assert equality["oracle"] == equality["resumed"]["state"] == equality["uninterrupted"]["state"]
        pids = equality["distinct_actual_processes"]
        assert len(pids) == len(set(pids)) == 4 and all(type(pid) is int and pid > 0 for pid in pids)
        for save in saves:
            assert save["ready_sequence"] < save["stop_sequence"]
            assert save["physical_exit_observed"] is True and save["source_process"]["stop_confirmed"] is True
            assert save["source_process"]["exit_code"] == 0
            assert save["checkpoint"]["verification_status"] == "verified"
        assert resumed["lease"]["state"] == "Released" and resumed["lease"]["restart_count"] == 2
        assert resumed["final_attempt"]["checkpoint"]["continuation_status"] == "applied"
        assert checks[name + ":settled-persistence-isolation"]["observed"]["bob_status"] == 404


def validate_prefix(root, child):
    path = root / "tests/acceptance/cp3/cp3_state_replay.py"
    spec = importlib.util.spec_from_file_location("cp4_independent_state_replay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    names = {Path(ref["path"]).stem for ref in child["evidence"]}
    variants = {"baseline", "changed-future-arrival", "deleted-future-arrival",
                "changed-future-recall", "deleted-future-recall", "changed-hidden-duration", "visible-control"}
    assert names == {f"{policy}-{variant}" for policy in ("B0", "B1", "B2", "P") for variant in variants}
    assert len(child["evidence"]) == len(names)
    for ref in child["evidence"]:
        if Path(ref["path"]).stem.endswith("visible-control"):
            continue
        record = json.loads((root / ref["path"]).read_text())
        for key in ("state_replay", "cutoff_state_replay"):
            retained = record[key]
            observations = {int(k): v for k, v in retained["observed_commit_prefixes"].items()}
            journal = retained["journal"]
            assert set(observations) == set(range(1, journal["head_sequence"] + 1))
            rebuilt = module.replay(journal, observed_prefixes=observations)
            assert module.checksum(rebuilt) == retained["rebuilt_sha256"] == module.checksum(retained["final_persisted_state"])


def validate_overview(child):
    assert len(child["cases"]) == 2
    assert {case["mode"] for case in child["cases"]} == {"logical_simulation", "cpu_reference"}
    for case in child["cases"]:
        assert not case.get("failure")
        check_set(case["checks"], {key for key in CHECK_IDS["overview"] if key.startswith(case["mode"] + ":")})
        summary = next(row["observed"] for row in case["checks"] if row["id"].endswith("fifty-consecutive-read-only-overviews"))
        assert summary["count"] == 50 and summary["retry_count"] == 0 and summary["all_statuses"] == 200
        assert summary["business_commands_added"] == 0 and summary["business_state_before_after_equal"] is True
        requests = [row for row in case["history"] if row.get("kind") == "http"
                    and row.get("path") == "/x/v1/admin/overview" and row.get("actor_alias") == "admin"]
        assert len(requests) >= 50 and all(row["status"] == 200 for row in requests)


def validate_decision_record(name, record):
    """Cross-check actual queries against captured proposals and commit receipts."""
    policy, case = name.split("-", 1)
    trace, history = record["server_capture"], record["http_history"]
    assert not trace.get("failure") and trace["case"] == case
    assert len(trace["submissions"]) == 1, "Decision case did not retain its unique submission"
    attempted = trace["submissions"][0]
    assert attempted["command"]["operation"] == "decision.commit"
    plan = attempted["command"]["parameters"]["plan"]
    assert plan["policy"]["name"] == policy, "A different policy case was substituted"
    assert any(row["plan"] == plan for row in trace["plans"]), "Submitted plan absent from captured planning"

    def queried(path, actor="a", status=200, method="GET"):
        rows = [row for row in history if row.get("kind") == "http" and row.get("method") == method
                and row.get("path") == path and row.get("actor_alias") == actor]
        assert len(rows) == 1 and rows[0]["status"] == status, "Required decision HTTP observation missing or inconsistent"
        return rows[0]

    def coverage(item):
        examined = [candidate for candidate in item["candidates"] if candidate["feasibility"] != "unexamined"]
        assert item["coverage"]["examined_count"] == len(examined)
        assert item["coverage"]["scope"] == "partial" and item["coverage"]["truncation_reason"]
        assert item["asserts_pool_infeasible"] is False
        return examined

    intent = f"coverage-{policy.lower()}-{case}"
    assert queried("/x/v1/work-intents", status=202, method="POST")["request_body"]["intent_id"] == intent
    detail = queried(f"/x/v1/work-intents/{intent}")["response_body"]
    available = queried("/v1/capacity")["response_body"]["totals"]["available"]["gpu_count"]
    examined = coverage(plan)
    assert len(plan["candidates"]) <= (1 if case == "budget" else 32)
    decision_path = "/x/v1/decisions/" + plan["decision_id"]
    receipt = attempted["receipt"]
    if case in {"stale", "illegal-placement"}:
        expected_error = "stale_version" if case == "stale" else "insufficient_resources"
        assert receipt["outcome"] == "rejected" and receipt["error"]["code"] == expected_error
        assert attempted["before"] == attempted["after"], "Rejected decision changed authoritative snapshot"
        queried(decision_path, actor="admin", status=404)
        assert detail["allocations"][0]["state"] == "Pending" and available == 2
        assert not any(event.get("decision_id") == plan["decision_id"] for event in detail["events"])
        return
    assert case in {"accepted", "budget"} and receipt["outcome"] == "committed"
    admin = queried(decision_path, actor="admin")["response_body"]
    owner = queried(decision_path)["response_body"]
    queried(decision_path, actor="b", status=404)
    assert admin["item"] == plan, "HTTP explanation differs from the submitted plan"
    assert admin["commit_status"] == owner["commit_status"] == "committed"
    selected = [candidate for candidate in examined if candidate["candidate_id"] == plan["selected_candidate_id"]]
    assert len(selected) == 1 and selected[0]["feasibility"] == "legal"
    selected = selected[0]
    grants = [event for event in admin["actual_events"] if event["event_type"] == "allocation.granted"]
    assert len(grants) == 1
    grant = grants[0]
    assert grant["event_id"] in receipt["event_ids"]
    assert grant["decision_snapshot_version"] == plan["snapshot_version"] and grant["decision_id"] == plan["decision_id"]
    deltas, slices = grant["resource_deltas"], selected["placement"]["slices"]
    assert {delta["allocation_id"] for delta in deltas} == {selected["allocation_id"]}
    assert sorted(device for delta in deltas for device in delta["device_ids_added"]) == sorted(
        device for piece in slices for device in piece["device_ids"]), "Granted devices differ from selected placement"
    for dimension in ("cpu_millicores", "memory_mib", "ephemeral_storage_mib"):
        assert sum(delta[dimension] for delta in deltas) == sum(piece["resources"][dimension] for piece in slices), "Granted resources differ from selected placement"
    allocation = [row for row in detail["allocations"] if row["id"] == selected["allocation_id"]]
    assert len(allocation) == 1 and allocation[0]["state"] == "Running" and available == 1
    coverage(owner["item"])
    assert all(candidate["owner_id"] == "alice" and not candidate["scores"] for candidate in owner["item"]["candidates"])
    assert owner["item"]["selected_candidate_id"] == plan["selected_candidate_id"]
    if case == "budget":
        assert len(examined) == 1 and "budget" in plan["coverage"]["truncation_reason"].lower()
    else:
        assert any(candidate["feasibility"] == "infeasible" for candidate in examined)


def validate_decisions(root, child):
    expected = CHECK_IDS["decision-coverage"]
    refs = child["evidence"]
    names = [Path(entry["path"]).stem for entry in refs]
    assert len(names) == len(expected) and set(names) == expected, "All 16 unique decision histories are required"
    for entry, name in zip(refs, names, strict=True):
        assert entry["path"] == f"evidence/cp4/reliability-decision-coverage_histories/{name}.json"
        validate_decision_record(name, json.loads((root / entry["path"]).read_text()))
