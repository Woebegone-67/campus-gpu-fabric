"""Synthetic entry/gate controls; these never represent product acceptance."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import accept_cp4 as entry  # noqa: E402
import check_checkpoint as gate  # noqa: E402

BUILD = {'git_commit': 'synthetic-control', 'source_sha256': 'synthetic-source', 'source_file_count': 1}


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    config = json.loads((ROOT / 'configs/checkpoints.json').read_text())['checkpoints']['CP4']
    requirement = next(row for row in config['required_checks'] if row['id'] == 'CP4-SCALE')
    config['required_checks'] = [requirement]
    (tmp_path / 'configs').mkdir()
    (tmp_path / 'configs/checkpoints.json').write_text(json.dumps({'checkpoints': {'CP4': config}}))
    out = tmp_path / 'evidence/cp4'
    out.mkdir(parents=True)
    primary = out / 'synthetic-primary.json'
    primary.write_text('{"scope":"synthetic reviewer test only"}')
    receipt = out / 'scale_receipt.json'
    receipt.write_text('{"scope":"old synthetic same-build observation"}')
    validator = tmp_path / requirement['validator_path']
    validator.parent.mkdir(parents=True)
    validator.write_text(
        'def review(root):\n'
        '    return ' + repr({'check': 'CP4-SCALE', 'status': 'PASS', 'build': BUILD,
            'observed_at': '2026-11-14T00:00:00+00:00', 'actual': 'Synthetic retained evidence passes its fixture reviewer',
            'evidence': [{'path': 'evidence/cp4/synthetic-primary.json',
                          'sha256': hashlib.sha256(primary.read_bytes()).hexdigest()}],
            'unresolved_issues': []}) + '\n')
    monkeypatch.setattr(entry, 'ROOT', tmp_path)
    monkeypatch.setattr(entry, 'OUT', out)
    monkeypatch.setattr(entry, 'current_build', lambda root: BUILD)
    monkeypatch.setattr(gate, 'current_build', lambda root: BUILD)
    return tmp_path, out, requirement


@pytest.mark.parametrize('exit_code', [1, 124, -9])
def test_failed_invocation_blocks_collect_only_and_formal_gate(candidate, monkeypatch, exit_code):
    root, out, _ = candidate
    assert entry.collect()['status'] == 'MET'
    original_receipt = (out / 'receipt.json').read_bytes()
    monkeypatch.setattr(entry, 'run_command', lambda *args, **kwargs: exit_code)
    assert entry.invoke('scale') == 1
    latest = json.loads((out / 'invocations/CP4-SCALE.json').read_text())
    assert latest['state'] == 'FAILED' and latest['exit_code'] == exit_code
    # The existing previously passing aggregate is rejected before recollection.
    assert gate.evaluate_checkpoint(root, 'CP4')['status'] == 'NOT_MET'
    assert entry.collect()['status'] == 'NOT_MET'
    assert b'"status": "MET"' in original_receipt


def test_new_success_resolves_failed_attempt_but_receipt_tamper_does_not(candidate, monkeypatch):
    root, out, requirement = candidate
    monkeypatch.setattr(entry, 'run_command', lambda *args, **kwargs: 124)
    entry.invoke('scale')
    previous_id = json.loads((out / 'invocations/CP4-SCALE.json').read_text())['run_id']

    def success(command, stream, **kwargs):
        latest = json.loads((out / 'invocations/CP4-SCALE.json').read_text())
        assert latest['state'] == 'RUNNING'
        assert gate._cp4_invocation_errors(root, requirement, BUILD)
        Path(command[3]).write_text('{"scope":"new synthetic observation"}')
        stream.write('Synthetic child completed successfully\n')
        return 0

    monkeypatch.setattr(entry, 'run_command', success)
    assert entry.invoke('scale') == 0
    latest = json.loads((out / 'invocations/CP4-SCALE.json').read_text())
    assert latest['state'] == 'COMPLETE' and latest['run_id'] != previous_id
    assert entry.collect()['status'] == 'MET'
    (out / 'scale_receipt.json').write_text('{"changed":true}')
    assert gate.evaluate_checkpoint(root, 'CP4')['status'] == 'NOT_MET'
    assert entry.collect()['status'] == 'NOT_MET'


@pytest.mark.parametrize('error', [RuntimeError('synthetic launch error'), KeyboardInterrupt()])
def test_exception_and_interrupt_remain_failed(candidate, monkeypatch, error):
    root, out, requirement = candidate

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(entry, 'run_command', fail)
    with pytest.raises(type(error)):
        entry.invoke('scale')
    latest = json.loads((out / 'invocations/CP4-SCALE.json').read_text())
    assert latest['state'] == 'FAILED' and latest['failure']
    assert gate._cp4_invocation_errors(root, requirement, BUILD)
    assert entry.collect()['status'] == 'NOT_MET'


def test_incomplete_attempt_blocks_but_old_build_failure_is_not_inherited(candidate, monkeypatch):
    root, out, requirement = candidate
    monkeypatch.setattr(entry, 'run_command', lambda *args, **kwargs: 1)
    entry.invoke('scale')
    path = out / 'invocations/CP4-SCALE.json'
    latest = json.loads(path.read_text())
    latest['state'] = 'RUNNING'
    path.write_text(json.dumps(latest))
    assert gate._cp4_invocation_errors(root, requirement, BUILD)
    latest['build'] = {**BUILD, 'source_sha256': 'old-source'}
    path.write_text(json.dumps(latest))
    assert not gate._cp4_invocation_errors(root, requirement, BUILD)
    assert entry.collect()['status'] == 'MET'


def test_main_does_not_return_success_after_timeout_with_old_pass(candidate, monkeypatch):
    entry.collect()
    monkeypatch.setattr(entry, 'run_command', lambda *args, **kwargs: 124)
    monkeypatch.setattr(sys, 'argv', ['accept_cp4.py', '--checks', 'scale'])
    assert entry.main() == 1


def test_older_attempt_finishing_cannot_erase_newer_failure(candidate, monkeypatch):
    root, out, requirement = candidate
    calls = []

    def overlapping(command, stream, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            assert entry.invoke('scale') == 1
            Path(command[3]).write_text('{"scope":"older synthetic successful attempt"}')
            return 0
        return 124

    monkeypatch.setattr(entry, 'run_command', overlapping)
    assert entry.invoke('scale') == 0
    latest = json.loads((out / 'invocations/CP4-SCALE.json').read_text())
    assert latest['state'] == 'FAILED' and latest['exit_code'] == 124
    assert gate._cp4_invocation_errors(root, requirement, BUILD)
    assert entry.collect()['status'] == 'NOT_MET'
