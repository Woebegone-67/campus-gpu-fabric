"""Project registered checkpoint receipts without treating a label as evidence.

This is a read adapter for the repository's existing receipt gate. It does not
run acceptance scenarios, create receipts, or infer a human's participation.
Only generated summaries leave this adapter: receipt prose, commands and
artifact contents can contain private work details and are not product data.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from campus_fabric.contracts.models import EvidenceCheck


def _gate_module(root: Path):
    # Load the registered checker by its exact path, without adding scripts/ to
    # process-wide sys.path or duplicating a weaker version of its validation.
    spec = importlib.util.spec_from_file_location(
        "campus_fabric_checkpoint_gate", root / "scripts/check_checkpoint.py"
    )
    if spec is None or spec.loader is None:
        raise OSError("Checkpoint verifier unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _safe_reason(report: dict) -> str:
    """Use only fixed text, never echo paths or arbitrary values from receipts."""
    errors = report.get("errors", [])
    if any("receipt build is stale" in str(error) for error in errors):
        return "回执对应的提交或源码已变化，需要重新验收。"
    if any("evidence digest mismatch" in str(error) for error in errors):
        return "执行证据的内容与回执摘要不一致。"
    if any("validator SHA-256" in str(error) for error in errors):
        return "登记的验收程序已变化，需要重新验收。"
    return "回执缺项、格式错误或执行证据未通过完整性校验。"


def _partial_check_statuses(report: dict, receipt: dict, requirements: list) -> dict[str, str]:
    """Project per-check verification from the same complete registered gate.

    A deliberately leaves the joint CP2 receipt NOT_MET while recording its eight
    backend checks and the missing UI check separately. Only the aggregate status
    mismatch may be ignored for an individual row. Every other global error still
    rejects all rows, and MET needs zero errors for that exact registered check.
    This does not accept the checkpoint or trust an unverified embedded MET label.
    """
    if receipt.get("status") != "NOT_MET" or report.get("status") != "NOT_MET":
        return {}
    required = {r["id"]: r for r in requirements}
    entries = receipt.get("checks")
    if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
        return {}
    if any(not isinstance(e.get("id"), str) for e in entries):
        return {}
    by_id = {e["id"]: e for e in entries}
    if len(by_id) != len(entries) or set(by_id) != set(required):
        return {}
    checked = report.get("checked_ids", [])
    if len(checked) != len(required) or set(checked) != set(required):
        return {}
    aggregate_error = "receipt status must be 'MET'; got 'NOT_MET'"
    errors = report.get("errors", [])
    if aggregate_error not in errors or any(
        error != aggregate_error and not any(error.startswith(f"{identity}: ") for identity in required)
        for error in errors
    ):
        return {}
    statuses = {}
    for identity, entry in by_id.items():
        own_errors = [error for error in errors if error.startswith(f"{identity}: ")]
        status = "NOT_MET"
        if entry.get("status") == "MET" and not own_errors:
            status = "MET"
        elif entry.get("status") == "NOT_RUN" and all(
            entry.get(key) == value for key, value in {
                "target_id": required[identity]["target_id"],
                "build": receipt.get("build"), "expected": required[identity]["expected"],
            }.items()
        ) and not any(entry.get(key) for key in ("actual", "evidence", "command", "executor", "observed_at")):
            status = "NOT_RUN"
        statuses[identity] = status
    # An aggregate NOT_MET with every row passing is contradictory, not a
    # shortcut that may turn the API aggregate green.
    return statuses if any(value != "MET" for value in statuses.values()) else {}


def checkpoint_checks(root: Path) -> tuple[tuple[EvidenceCheck, ...], bool | None]:
    """Return current receipt results; never cache a previously passing build.

    Missing stages cost no source fingerprint scan. Present receipts are checked
    on each explicit read, including their source, validator and artifact hashes.
    Initially passing results receive a second full gate check, including artifact
    hashes, followed by source/receipt consistency checks. The response describes
    that verified snapshot, not an atomic filesystem guarantee. This deliberately
    avoids a time/mtime cache that could retain MET after evidence changes.
    """
    root = root.resolve()
    registry_path = root / "configs/checkpoints.json"
    registry_bytes = registry_path.read_bytes()
    registry = json.loads(registry_bytes)
    checks: list[EvidenceCheck] = []
    present_builds: list[bool] = []
    gate = None
    for name, checkpoint in registry["checkpoints"].items():
        if name == "CP0":
            continue  # CP0 is a design/host gate, not product runtime acceptance.
        receipt_path = root / checkpoint["receipt_path"]
        status = "NOT_RUN"
        reason = "尚未登记完整验收回执。"
        individual_statuses = {}
        if receipt_path.is_file():
            try:
                gate = gate or _gate_module(root)
                before = receipt_path.read_bytes()
                receipt = json.loads(before)
                report = gate.evaluate_checkpoint(root, name)
                partial = _partial_check_statuses(report, receipt, checkpoint["required_checks"])
                if report["status"] == "MET" or "MET" in partial.values():
                    # Evidence files are intentionally excluded from source build
                    # fingerprints. Re-run the registered safe-path/hash checks,
                    # so a changed artifact cannot survive a source-only recheck.
                    report = gate.evaluate_checkpoint(root, name)
                current = gate.current_build(root)
                stable = (before == receipt_path.read_bytes()
                          and registry_bytes == registry_path.read_bytes())
                matching = isinstance(receipt, dict) and receipt.get("build") == current
                stable = stable and report.get("current_build") == current
                present_builds.append(bool(matching and stable))
                if report["status"] == "MET" and matching and stable:
                    status = "MET"
                    reason = "当前构建的完整回执、验收程序及执行证据摘要均已核对。"
                else:
                    status = "NOT_MET"
                    reason = _safe_reason(report) if stable else "构建或回执已变化，请重新核验。"
                    if matching and stable:
                        individual_statuses = _partial_check_statuses(report, receipt, checkpoint["required_checks"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                # A malformed/unreadable receipt is a failed verification, not
                # a 500, a previous cached pass, or a claimed scenario failure.
                status = "NOT_MET"
                reason = "验收回执无法完整读取或校验，尚不能确认通过。"
                present_builds.append(False)
        for requirement in checkpoint["required_checks"]:
            row_status = individual_statuses.get(requirement["id"], status)
            row_reason = reason
            if individual_statuses:
                if row_status == "MET":
                    row_reason = "本项的当前构建、登记验证器及执行证据摘要均已核对；联合检查点仍有未通过或未运行项。"
                elif row_status == "NOT_RUN":
                    row_reason = "本项尚未登记完整执行证据，联合检查点未通过。"
                else:
                    row_reason = _safe_reason({"errors": [e for e in report["errors"]
                        if e.startswith(f"{requirement['id']}: ")]})
            common = {
                "target_id": requirement["target_id"],
                "status": row_status,
                "expected": f"{requirement['id']}: {requirement['expected']}",
            }
            if row_status == "NOT_RUN":
                checks.append(EvidenceCheck(**common, unresolved=(f"{name}：{row_reason}",)))
                continue
            # These fields describe receipt verification, not a fresh scenario
            # execution. Keep raw actual/executor/command strings and artifact
            # payloads out of the response even for the administrator UI.
            checks.append(EvidenceCheck(
                **common,
                actual=f"{requirement['id']}：{row_reason} 此状态来自回执校验，不是本次重新执行测试。",
                executor="checkpoint-receipt-verifier",
                command_or_action=f"校验 {name} 的构建、登记验证器和执行证据摘要",
                evidence_uris=(f"repo:{checkpoint['receipt_path']}",),
                unresolved=() if row_status == "MET" else (row_reason,),
            ))
    return tuple(checks), all(present_builds) if present_builds else None
