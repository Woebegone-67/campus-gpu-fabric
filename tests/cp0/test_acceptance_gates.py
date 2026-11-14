"""Tests of acceptance tooling; synthetic receipts here are never product evidence."""
from __future__ import annotations

import copy
import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
gate = importlib.import_module('check_checkpoint')
static = importlib.import_module('check_cp0')


@pytest.fixture
def evidence_repo(tmp_path, monkeypatch):
    (tmp_path / 'configs').mkdir()
    config = gate.load_json(ROOT / 'configs/checkpoints.json')
    (tmp_path / 'configs/checkpoints.json').write_text(json.dumps(config))
    build = {'git_commit': 'a' * 40, 'source_sha256': 'b' * 64, 'source_file_count': 1}
    monkeypatch.setattr(gate, 'current_build', lambda root: build.copy())
    return tmp_path, config, build


def valid_synthetic_receipt(root, config, build):
    """Construct only a temporary parser fixture; it describes no real product run."""
    evidence = root / 'evidence/cp1/isolated-parser-fixture.txt'
    evidence.parent.mkdir(parents=True)
    evidence.write_text('Synthetic temporary input for the receipt validator unit test.\n')
    checks = []
    for requirement in config['checkpoints']['CP1']['required_checks']:
        validator = root / requirement['validator_path']
        validator.parent.mkdir(parents=True, exist_ok=True)
        validator.write_text('# Synthetic temporary registered-validator fixture.\n')
        checks.append({
            'id': requirement['id'], 'target_id': requirement['target_id'], 'status': 'MET',
            'build': build.copy(), 'expected': requirement['expected'],
            'evidence_kind': requirement['allowed_evidence_kinds'][0],
            'validator': {'path': requirement['validator_path'], 'sha256': gate.sha256_file(validator)},
            'input_version': 'synthetic-parser-fixture-v1', 'executor': 'acceptance-tool unit test',
            'command': 'isolated temporary receipt parser fixture',
            'actual': 'Synthetic data accepted by the parser; no product behavior tested.',
            'observed_at': datetime.now(timezone.utc).isoformat(), 'unresolved_issues': [],
            'evidence': [{'path': evidence.relative_to(root).as_posix(), 'sha256': gate.sha256_file(evidence)}],
        })
    receipt = {'schema_version': '1.0.0', 'checkpoint': 'CP1', 'status': 'MET', 'scope': 'runtime_acceptance', 'build': build.copy(), 'checks': checks}
    write_receipt(root, receipt)
    return receipt


def write_receipt(root, receipt):
    path = root / 'evidence/cp1/receipt.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt))


def test_absent_runtime_evidence_is_not_run_and_cli_fails(evidence_repo, capsys):
    root, _, _ = evidence_repo
    result = gate.evaluate_checkpoint(root, 'CP1')
    assert result['status'] == 'NOT_RUN'
    assert 'missing runtime evidence' in result['errors'][0]
    assert gate.main(['CP1', '--root', str(root)]) == 1
    assert 'NOT_RUN' in capsys.readouterr().out


def test_structurally_complete_synthetic_receipt_is_accepted(evidence_repo):
    root, config, build = evidence_repo
    valid_synthetic_receipt(root, config, build)
    assert gate.evaluate_checkpoint(root, 'CP1')['status'] == 'MET'


@pytest.mark.parametrize('mutation,expected', [
    (lambda r: r.update(status='NOT_RUN'), 'receipt status'),
    (lambda r: r['checks'][0].update(status='NOT_RUN'), 'status does not match'),
    (lambda r: r['checks'][0].update(evidence_kind='static'), 'static/skeleton'),
    (lambda r: r['checks'][0].update(actual='PASS'), 'non-placeholder'),
    (lambda r: r['checks'][0].update(evidence=[]), 'actual execution artifacts'),
    (lambda r: r['checks'][0].update(validator={'path': 'tests/cp0/test_acceptance_gates.py', 'sha256': 'f' * 64}), 'registered'),
    (lambda r: r['checks'][0].update(unresolved_issues=['known oversell']), 'unresolved issues'),
    (lambda r: r['checks'].pop(), 'missing check'),
    (lambda r: r['checks'].append(copy.deepcopy(r['checks'][0])), 'duplicate'),
    (lambda r: r['build'].update(source_sha256='c' * 64), 'stale'),
    (lambda r: r['checks'][0]['evidence'][0].update(sha256='d' * 64), 'digest mismatch'),
    (lambda r: r['checks'][0]['evidence'][0].update(path='../outside.log'), 'must be stored under'),
    (lambda r: r['checks'][0].update(observed_at='2026-01-01'), 'timezone-qualified'),
])
def test_runtime_gate_rejects_incomplete_or_misleading_receipts(evidence_repo, mutation, expected):
    root, config, build = evidence_repo
    receipt = valid_synthetic_receipt(root, config, build)
    mutation(receipt)
    write_receipt(root, receipt)
    result = gate.evaluate_checkpoint(root, 'CP1')
    assert result['status'] == 'NOT_MET'
    assert any(expected in error for error in result['errors'])


def test_registered_validator_must_exist(evidence_repo):
    root, config, build = evidence_repo
    receipt = valid_synthetic_receipt(root, config, build)
    (root / receipt['checks'][0]['validator']['path']).unlink()
    assert any('does not exist' in e for e in gate.evaluate_checkpoint(root, 'CP1')['errors'])


def test_source_digest_changes_with_code_but_not_evidence(tmp_path):
    (tmp_path / 'src').mkdir()
    code = tmp_path / 'src/app.py'
    code.write_text('version = 1\n')
    first = gate.current_build(tmp_path)
    (tmp_path / 'evidence').mkdir()
    (tmp_path / 'evidence/run.json').write_text('{"status": "NOT_RUN"}')
    assert gate.current_build(tmp_path) == first
    code.write_text('version = 2\n')
    assert gate.current_build(tmp_path)['source_sha256'] != first['source_sha256']


def test_cp0_gate_does_not_pass_without_observed_receipts(tmp_path):
    result = gate.evaluate_checkpoint(tmp_path, 'CP0')
    assert result['status'] == 'NOT_MET'
    assert result['product_acceptance'] == 'NOT_RUN'
    assert result['scope'] == 'cp0_static_and_host_only'


def test_official_snapshot_mutation_is_rejected(tmp_path):
    official = tmp_path / 'spec/official'
    official.mkdir(parents=True)
    for name in static.OFFICIAL_FILES:
        path = official / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / 'spec/official' / name).read_bytes())
    manifest = {'files': {name: gate.sha256_file(official / name) for name in static.OFFICIAL_FILES}}
    (official / 'manifest.json').write_text(json.dumps(manifest))
    assert static.check_official(tmp_path)['operation_count'] == 21
    with (official / 'api/openapi.yaml').open('a') as handle:
        handle.write('\n# accidental frozen-source edit\n')
    with pytest.raises(ValueError, match='digest mismatch'):
        static.check_official(tmp_path)


def test_mapping_covers_exact_official_operation_set():
    assert static.check_ownership(ROOT)['operation_count'] == 21


def test_all_future_checkpoint_plans_start_not_run():
    config = gate.load_json(ROOT / 'configs/checkpoints.json')
    for name, checkpoint in config['checkpoints'].items():
        assert checkpoint['status'] == 'NOT_RUN'
        assert all(check['status'] == 'NOT_RUN' for check in checkpoint['required_checks'])
        if name != 'CP0':
            assert all(check['validator_path'].startswith('tests/acceptance/') for check in checkpoint['required_checks'])
