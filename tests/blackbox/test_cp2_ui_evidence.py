"""Evidence-review attack fixtures, not observed product/human acceptance."""
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "cp2_ui_gate", ROOT / "tests/acceptance/cp2/cp2_six_workspaces.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    build = {"git_commit": "fixture", "source_sha256": "fixture", "source_file_count": 1}
    monkeypatch.setattr(gate, "current_build", lambda _: build)
    base = tmp_path / "evidence/cp2/b"
    base.mkdir(parents=True)
    data = base / "observed.txt"
    data.write_text("Synthetic parser fixture only; never a real acceptance artifact.")
    observation = {"status": "PASS", "actual": "Synthetic fixture with sufficiently explicit expected content.",
        "evidence": [{"path": "evidence/cp2/b/observed.txt", "sha256": hashlib.sha256(data.read_bytes()).hexdigest()}]}
    common = {"build": build, "observed_at": datetime.now(timezone.utc).isoformat()}
    ui = {**common, "observer_kind": "agent", "observer_ids": ["fixture"],
        "workspaces": sorted(gate.WORKSPACES), "roles": ["researcher", "contributor", "administrator"],
        "checks": [{**observation, "id": name} for name in sorted(gate.JOURNEYS)]}
    humans = {**common, "operators": [{**observation, "human_id": name,
        "role": name, "observer_kind": "human", "workspaces": sorted(gate.WORKSPACES)} for name in ["A", "B"]]}
    (base / "ui_receipt.json").write_text(json.dumps(ui))
    (base / "human_handoff.json").write_text(json.dumps(humans))
    return tmp_path, base, ui, humans


def test_agent_qa_cannot_implicitly_close_human_handoff(evidence):
    root, base, _, _ = evidence
    (base / "human_handoff.json").unlink()
    gate.review(root, require_humans=False)
    with pytest.raises(FileNotFoundError):
        gate.review(root)


def test_changed_artifact_and_other_build_are_rejected(evidence):
    root, base, ui, _ = evidence
    ui["build"] = {"git_commit": "old"}
    (base / "ui_receipt.json").write_text(json.dumps(ui))
    with pytest.raises(ValueError, match="current build"):
        gate.review(root)
    ui["build"] = gate.current_build(root)
    (base / "ui_receipt.json").write_text(json.dumps(ui))
    (base / "observed.txt").write_text("Changed after recording")
    with pytest.raises(ValueError, match="changed"):
        gate.review(root)


def test_role_switching_is_not_two_humans(evidence):
    root, base, _, humans = evidence
    humans["operators"][1]["observer_kind"] = "agent"
    (base / "human_handoff.json").write_text(json.dumps(humans))
    with pytest.raises(ValueError, match="agent"):
        gate.review(root)


def test_missing_workspace_and_path_escape_rejected(evidence):
    root, base, ui, _ = evidence
    ui["workspaces"].pop()
    (base / "ui_receipt.json").write_text(json.dumps(ui))
    with pytest.raises(ValueError, match="six workspaces"):
        gate.review(root)
    with pytest.raises(ValueError, match="outside"):
        gate.artifact(root, {"path": "../elsewhere", "sha256": "x"})


def test_complete_parser_fixture_is_valid(evidence):
    root, _, _, _ = evidence
    assert gate.review(root) == gate.current_build(root)


def test_experiment_code_changes_invalidate_build(tmp_path):
    directory = tmp_path / "experiments"
    directory.mkdir()
    source = directory / "runner.py"
    source.write_text("MODEL = 'first'\n")
    first = gate.current_build(tmp_path)
    source.write_text("MODEL = 'changed'\n")
    second = gate.current_build(tmp_path)
    assert first["source_sha256"] != second["source_sha256"]
    assert first["source_file_count"] == second["source_file_count"] == 1
