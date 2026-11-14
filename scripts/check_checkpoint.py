#!/usr/bin/env python3
"""Fail-closed evidence gate. It validates receipts, not the truth of their claims."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'dist', 'build', 'evidence', '.DS_Store'}
BUILD_ROOTS = ('src', 'contracts', 'configs', 'semantics', 'scripts', 'tests', 'web', 'experiments', 'spec/official', 'docs', 'deploy')
BUILD_FILES = ('pyproject.toml', 'uv.lock', 'requirements.lock', 'Makefile', 'README.md', 'Dockerfile', 'compose.yaml', '.python-version')
PLACEHOLDERS = {'', 'todo', 'tbd', 'placeholder', 'pass', 'met', 'done', 'not_run', 'stub', 'n/a'}


def load_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current_build(root: Path) -> dict:
    """Evidence is excluded so recording a run does not invalidate its own identity."""
    result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True, capture_output=True, check=False)
    commit = result.stdout.strip() if result.returncode == 0 else None
    paths = set()
    for dirname in BUILD_ROOTS:
        base = root / dirname
        if base.exists():
            paths.update(p for p in base.rglob('*') if p.is_file())
    paths.update(root / name for name in BUILD_FILES if (root / name).is_file())
    digest = hashlib.sha256()
    count = 0
    for path in sorted(paths, key=lambda p: p.relative_to(root).as_posix()):
        relative = path.relative_to(root)
        if any(part in IGNORED_PARTS for part in relative.parts) or path.suffix in {'.pyc', '.pyo'}:
            continue
        digest.update(relative.as_posix().encode('utf-8') + b'\0')
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        count += 1
    return {'git_commit': commit, 'source_sha256': digest.hexdigest(), 'source_file_count': count}


def meaningful(value) -> bool:
    return isinstance(value, str) and value.strip().lower() not in PLACEHOLDERS


def _artifact_errors(root: Path, checkpoint: str, entry, receipt_path: Path) -> list[str]:
    if not isinstance(entry, dict) or not {'path', 'sha256'}.issubset(entry):
        return ['evidence entries need path and sha256']
    raw = entry.get('path')
    if not isinstance(raw, str):
        return ['evidence path must be a repository-relative string']
    relative = Path(raw)
    expected_base = (root / 'evidence' / checkpoint.lower()).resolve()
    path = (root / relative).resolve()
    if relative.is_absolute() or '..' in relative.parts or not path.is_relative_to(expected_base):
        return [f'evidence must be stored under evidence/{checkpoint.lower()}/: {raw}']
    if path == receipt_path.resolve():
        return ['a receipt cannot cite itself as execution evidence']
    if not path.is_file() or path.stat().st_size == 0:
        return [f'missing or empty evidence artifact: {raw}']
    if sha256_file(path) != entry.get('sha256'):
        return [f'evidence digest mismatch: {raw}']
    return []


def _cp2_joint_ui_errors(root: Path, check: dict, build: dict, receipt_path: Path) -> list[str]:
    """A hash-valid agent QA receipt cannot establish the joint human UI gate.

    The registered UI validator performs a bounded read-only evidence review;
    it neither launches scenarios nor creates attestations. Rechecking the
    underlying current-build records also invalidates a formerly passing summary
    when its human handoff or screenshots have subsequently changed.
    """
    errors = []
    summaries = []
    for entry in check.get('evidence', []) if isinstance(check.get('evidence'), list) else []:
        if _artifact_errors(root, 'CP2', entry, receipt_path):
            continue
        try:
            value = load_json((root / entry['path']).resolve())
        except (OSError, ValueError, TypeError):
            continue
        if isinstance(value, dict) and value.get('check') == 'CP2-SIX-WORKSPACES':
            summaries.append(value)
    if not summaries or any(
        summary.get('status') != 'PASS'
        or summary.get('scope') != 'JOINT_CP2_UI_EVIDENCE_REVIEW'
        or summary.get('build') != build
        for summary in summaries
    ):
        errors.append('requires a passing current-build JOINT_CP2_UI_EVIDENCE_REVIEW artifact; agent QA is insufficient')
    try:
        path = root / 'tests/acceptance/cp2/cp2_six_workspaces.py'
        spec = importlib.util.spec_from_file_location('campus_cp2_joint_ui_gate', path)
        if spec is None or spec.loader is None:
            raise ValueError('registered UI reviewer unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reviewed_build = module.review(root, require_humans=True)
        if reviewed_build != build:
            raise ValueError('UI evidence review belongs to a different build')
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(f'joint UI and A/B human evidence review failed: {exc}')
    return errors



def _nested_evidence_errors(root: Path, checkpoint: str, entries: list, receipt_path: Path) -> list[str]:
    """Recheck declared nested evidence refs, not just their enclosing summary.

    Only explicitly named evidence fields are traversed. A validator must retain
    references to its underlying inputs; unreferenced files are outside this
    integrity check. This does not authenticate who produced an artifact.
    """
    errors, visited = [], set()

    def inspect(entry):
        problems = _artifact_errors(root, checkpoint, entry, receipt_path)
        if problems:
            errors.extend(problems)
            return
        path = (root / entry['path']).resolve()
        if path in visited or path.suffix.lower() != '.json':
            return
        visited.add(path)
        try:
            value = load_json(path)
        except (OSError, ValueError, UnicodeError) as exc:
            errors.append(f'invalid JSON evidence artifact: {entry["path"]}: {exc}')
            return

        def walk(node):
            if isinstance(node, list):
                for item in node:
                    walk(item)
            elif isinstance(node, dict):
                for key, item in node.items():
                    if key == 'evidence':
                        refs = item if isinstance(item, list) else [item]
                        for ref in refs:
                            inspect(ref)
                    else:
                        walk(item)
        walk(value)

    for entry in entries:
        inspect(entry)
    return errors


def _cp3_human_review(root: Path, build: dict) -> dict:
    """Re-read this round's two human attestations; automatic tests cannot sign."""
    result = {'status': 'NOT_MET', 'errors': []}
    try:
        path = root / 'tests/acceptance/cp3/cp3_human_handoff.py'
        spec = importlib.util.spec_from_file_location('campus_cp3_human_gate', path)
        if spec is None or spec.loader is None:
            raise ValueError('CP3 human evidence reviewer unavailable')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if module.review(root) != build:
            raise ValueError('CP3 human evidence review belongs to another build')
        result['status'] = 'PASS'
    except FileNotFoundError as exc:
        result.update(status='NOT_RUN', errors=[str(exc)])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result['errors'].append(str(exc))
    return result


def _cp4_runtime_review(root: Path, requirement: dict, build: dict) -> list[str]:
    """Recompute CP4 conclusions from primary artifacts, including human boundaries.

    Registered review() functions are read-only. They must never start workloads
    while the product displays checkpoint status.
    """
    path = root / requirement['validator_path']
    original_path = list(sys.path)
    try:
        sys.path.insert(0, str(path.parent))
        sys.path.insert(0, str(root / 'scripts'))
        spec = importlib.util.spec_from_file_location(
            'campus_cp4_gate_' + path.stem, path)
        if spec is None or spec.loader is None:
            return ['registered read-only reviewer unavailable']
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        reviewed = module.review(root)
        if not isinstance(reviewed, dict):
            return ['registered reviewer returned no structured result']
        if (reviewed.get('status') != 'PASS' or reviewed.get('build') != build
                or reviewed.get('check') != requirement['id']):
            return ['primary evidence review did not pass for this build: '
                    + str(reviewed.get('unresolved_issues') or reviewed.get('actual'))]
        return []
    except (OSError, ValueError, KeyError, TypeError, AttributeError, AssertionError,
            ImportError) as exc:
        return [f'primary evidence review failed: {exc}']
    finally:
        sys.path[:] = original_path


def _cp4_invocation_errors(root: Path, requirement: dict, build: dict) -> list[str]:
    """A failed or unfinished latest attempt cannot inherit an older PASS.

    Directly produced primary evidence remains supported when no orchestrator
    invocation exists. Records from another build do not carry failures forward.
    This checks execution consistency, not the authenticity of the operator.
    """
    path = root / 'evidence/cp4/invocations' / (requirement['id'] + '.json')
    if not path.exists():
        return []
    try:
        record = load_json(path)
        if not isinstance(record, dict) or not isinstance(record.get('build'), dict):
            raise ValueError('invalid latest invocation record')
        if record['build'] != build:
            return []
        if record.get('schema') != 'cp4-invocation-v1' or record.get('check') != requirement['id']:
            raise ValueError('wrong latest invocation identity')
        run_id = record.get('run_id', '')
        if len(run_id) != 32 or any(c not in '0123456789abcdef' for c in run_id):
            raise ValueError('latest invocation needs its unique run ID')
        validator = root / requirement['validator_path']
        name = validator.stem.removeprefix('cp4_')
        output = root / 'evidence/cp4' / (name + '_receipt.json')
        command = record.get('command', [])
        if (len(command) != 4 or not command[0]
                or command[1:] != [str(validator), '--output', str(output)]):
            raise ValueError('latest invocation command does not match the registered validator')
        if record.get('state') != 'COMPLETE':
            raise ValueError('latest invocation is ' + str(record.get('state'))
                             + '; a successful new attempt is required')
        if (record.get('exit_code') != 0 or record.get('failure')
                or record.get('build_at_finish') != build):
            raise ValueError('latest invocation did not finish successfully on this build')
        start = datetime.fromisoformat(record['started_at'])
        finish = datetime.fromisoformat(record['finished_at'])
        if (start.tzinfo is None or finish.tzinfo is None or finish < start
                or (finish - datetime.now(timezone.utc)).total_seconds() > 300):
            raise ValueError('latest invocation timestamps are invalid')
        expected_log = f'evidence/cp4/invocations/{requirement["id"]}-{run_id}.log'
        if record.get('log', {}).get('path') != expected_log:
            raise ValueError('latest invocation log was substituted')
        if record.get('receipt', {}).get('path') != output.relative_to(root).as_posix():
            raise ValueError('latest invocation receipt was substituted')
        for entry in (record['log'], record['receipt']):
            problems = _artifact_errors(root, 'CP4', entry, path)
            if problems:
                raise ValueError('; '.join(problems))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return [f'latest invocation failed review: {exc}']
    return []


def evaluate_checkpoint(root: Path, checkpoint: str, receipt_path: Path | None = None) -> dict:
    root = root.resolve()
    checkpoint = checkpoint.upper()
    report = {'checkpoint': checkpoint, 'status': 'NOT_MET', 'scope': 'receipt_evidence_gate', 'errors': [], 'checked_ids': []}
    errors = report['errors']
    if checkpoint == 'CP0':
        report['scope'] = 'cp0_static_and_host_only'
        report['product_acceptance'] = 'NOT_RUN'
        build = current_build(root)
        report['current_build'] = build
        required_ids = {
            'static_receipt.json': {'CP0-OFFICIAL-INTEGRITY', 'CP0-API-MAPPING', 'CP0-SEMANTICS', 'CP0-CONTRACT-FIXTURES', 'CP0-DOCUMENTATION', 'CP0-EVIDENCE-POLICY'},
            'runtime_receipt.json': {'host-health', 'version-capability-disclosure', 'unauthenticated-command-rejected', 'disabled-test-control-rejected', 'missing-checkpoint-object-rejected'},
        }
        for filename, expected_ids in required_ids.items():
            try:
                receipt = load_json(root / 'evidence/cp0' / filename)
                checks = receipt['checks']
                expected_status = 'MET' if filename == 'static_receipt.json' else 'PASS'
                if not isinstance(checks, list) or len(checks) != len(expected_ids) or {c['id'] for c in checks} != expected_ids:
                    raise ValueError('required CP0 check IDs are incomplete or duplicated')
                if any(c.get('status') != expected_status for c in checks):
                    raise ValueError('some CP0 checks did not succeed')
                if receipt.get('build') != build:
                    raise ValueError('receipt belongs to a different commit or source-content build')
                if filename == 'static_receipt.json':
                    if receipt.get('scope') != 'static_design_only' or receipt.get('claims', {}).get('product_acceptance') != 'NOT_RUN':
                        raise ValueError('static receipt must preserve the product NOT_RUN boundary')
                elif receipt.get('product_checks') != 'NOT_RUN' or receipt.get('scope') != 'CP0 local HTTP startup only, NOT product conformance':
                    raise ValueError('host receipt must preserve the product NOT_RUN boundary')
                report['checked_ids'].extend(sorted(expected_ids))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f'{filename}: {exc}')
        if not errors:
            report['status'] = 'MET'
        return report
    try:
        config = load_json(root / 'configs/checkpoints.json')['checkpoints'][checkpoint]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        errors.append(f'unknown checkpoint or invalid configuration: {exc}')
        return report
    receipt_path = receipt_path or root / config['receipt_path']
    if not receipt_path.is_file():
        report['status'] = 'NOT_RUN'
        errors.append(f'missing runtime evidence receipt: {receipt_path.relative_to(root) if receipt_path.is_relative_to(root) else receipt_path}')
        return report
    try:
        receipt = load_json(receipt_path)
    except (OSError, ValueError) as exc:
        errors.append(f'invalid receipt JSON: {exc}')
        return report
    if not isinstance(receipt, dict):
        errors.append('receipt must be an object')
        return report
    for key, expected in {'schema_version': '1.0.0', 'checkpoint': checkpoint, 'status': 'MET', 'scope': 'runtime_acceptance'}.items():
        if receipt.get(key) != expected:
            errors.append(f'receipt {key} must be {expected!r}; got {receipt.get(key)!r}')
    build = current_build(root)
    report['current_build'] = build
    if not build['git_commit']:
        errors.append('runtime acceptance requires a git commit identity')
    if receipt.get('build') != build:
        errors.append('receipt build is stale or incomplete; git commit and source content must match the current build')
    checks = receipt.get('checks', [])
    if not isinstance(checks, list) or any(not isinstance(c, dict) for c in checks):
        errors.append('receipt checks must be a list of per-check objects')
        return report
    by_id = {check.get('id'): check for check in checks}
    if len(by_id) != len(checks):
        errors.append('duplicate per-check IDs')
    required_ids = {c['id'] for c in config['required_checks']}
    if set(by_id) - required_ids:
        errors.append(f'unknown checks: {sorted(str(x) for x in set(by_id) - required_ids)}')
    for requirement in config['required_checks']:
        check_id = requirement['id']
        check = by_id.get(check_id)
        if check is None:
            errors.append(f'{check_id}: NOT_RUN (missing check)')
            continue
        report['checked_ids'].append(check_id)
        for key, expected in {'target_id': requirement['target_id'], 'status': 'MET', 'build': build, 'expected': requirement['expected']}.items():
            if check.get(key) != expected:
                errors.append(f'{check_id}: {key} does not match the required value')
        if check.get('evidence_kind') not in requirement['allowed_evidence_kinds']:
            errors.append(f'{check_id}: evidence kind must be one of {requirement["allowed_evidence_kinds"]}; static/skeleton results cannot establish runtime acceptance')
        validator_path = requirement.get('validator_path')
        validator = check.get('validator', {})
        if not validator_path or not isinstance(validator, dict) or validator.get('path') != validator_path:
            errors.append(f'{check_id}: receipt must identify its validator registered in configs/checkpoints.json')
        else:
            validator_file = (root / validator_path).resolve()
            if not validator_file.is_relative_to((root / 'tests/acceptance').resolve()) or not validator_file.is_file():
                errors.append(f'{check_id}: registered runtime validator does not exist')
            elif sha256_file(validator_file) != validator.get('sha256'):
                errors.append(f'{check_id}: registered validator SHA-256 does not match')
        for key in ('input_version', 'executor', 'command', 'actual'):
            if not meaningful(check.get(key)):
                errors.append(f'{check_id}: {key} needs a concrete, non-placeholder value')
        try:
            observed = datetime.fromisoformat(check.get('observed_at', '').replace('Z', '+00:00'))
            if observed.tzinfo is None or (observed - datetime.now(timezone.utc)).total_seconds() > 300:
                raise ValueError('timestamp needs a timezone and cannot be in the future')
        except (ValueError, TypeError, AttributeError):
            errors.append(f'{check_id}: observed_at must be a real timezone-qualified execution time')
        if check.get('unresolved_issues') != []:
            errors.append(f'{check_id}: unresolved issues must be explicitly empty before MET')
        artifacts = check.get('evidence')
        if not isinstance(artifacts, list) or not artifacts:
            errors.append(f'{check_id}: needs actual execution artifacts with SHA-256 digests')
        else:
            for artifact in artifacts:
                errors.extend(f'{check_id}: {err}' for err in _artifact_errors(root, checkpoint, artifact, receipt_path))
        if checkpoint in {'CP3', 'CP4'} and isinstance(artifacts, list):
            errors.extend(f'{check_id}: nested {err}' for err in
                          _nested_evidence_errors(root, checkpoint, artifacts, receipt_path))
        if checkpoint == 'CP4' and check.get('status') == 'MET':
            errors.extend(f'{check_id}: {err}' for err in
                          _cp4_invocation_errors(root, requirement, build))
            errors.extend(f'{check_id}: {err}' for err in
                          _cp4_runtime_review(root, requirement, build))
        if checkpoint == 'CP2' and check_id == 'CP2-SIX-WORKSPACES' and check.get('status') == 'MET':
            errors.extend(f'{check_id}: {err}' for err in _cp2_joint_ui_errors(root, check, build, receipt_path))
    if checkpoint == 'CP3':
        report['technical_status'] = 'MET' if not errors else 'NOT_MET'
        human_review = _cp3_human_review(root, build)
        report['human_acceptance'] = human_review['status']
        errors.extend(f'CP3-HUMAN-HANDOFF: {error}' for error in human_review['errors'])
    if not errors:
        report['status'] = 'MET'
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('checkpoint', nargs='?', help='CP1 through CP5')
    parser.add_argument('--checkpoint', dest='checkpoint_option')
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--receipt', type=Path)
    parser.add_argument('--build-id', action='store_true', help='Print current commit plus source digest without accepting any checkpoint')
    args = parser.parse_args(argv)
    if args.build_id:
        print(json.dumps(current_build(args.root.resolve()), ensure_ascii=False, indent=2))
        return 0
    checkpoint = args.checkpoint_option or args.checkpoint
    if not checkpoint:
        parser.error('checkpoint is required unless --build-id is used')
    report = evaluate_checkpoint(args.root, checkpoint, args.receipt)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'MET' else 1


if __name__ == '__main__':
    sys.exit(main())
