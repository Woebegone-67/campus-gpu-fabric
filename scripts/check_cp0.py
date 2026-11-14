#!/usr/bin/env python3
"""Check CP0 design artifacts only; this is not API or product acceptance."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import yaml
from check_checkpoint import current_build, evaluate_checkpoint, load_json, meaningful, sha256_file

ROOT = Path(__file__).resolve().parents[1]
HTTP_METHODS = {'get', 'post', 'put', 'patch', 'delete', 'options', 'head', 'trace'}
OFFICIAL_FILES = {'challenge.md', 'api/openapi.yaml', 'participant_supplement.pdf'}
REQUIRED_DOCS = (
    'docs/blueprint/00_START_HERE.md', 'docs/blueprint/01_终态产品与独立复审.md',
    'docs/blueprint/02_双人24小时正向开发作战书.md', 'docs/blueprint/03_交汇契约与验收规格.md',
    'docs/handoff/OWNER_A.md', 'docs/handoff/OWNER_B.md', 'docs/handoff/CHECKPOINTS.md',
    'docs/handoff/HANDOFF_TEMPLATE.md', 'docs/SEMANTICS.md', 'docs/STATE_MODEL.md',
    'docs/PRODUCT.md', 'docs/ACCEPTANCE.md',
)
CONTRACT_NAMES = {'CommandEnvelope', 'CommitReceipt', 'WorkIntent', 'DecisionSnapshot', 'DecisionPlan', 'CommittedEvent', 'ReadSnapshot', 'EvidenceRun'}


def official_operations(spec: dict) -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, body in spec['paths'].items()
            for method in body if method in HTTP_METHODS}


def check_official(root: Path) -> dict:
    base = root / 'spec/official'
    manifest = load_json(base / 'manifest.json')
    files = manifest['files']
    if set(files) != OFFICIAL_FILES:
        raise ValueError('official manifest must list exactly challenge.md, api/openapi.yaml and participant_supplement.pdf')
    for relative, digest in files.items():
        if not isinstance(digest, str) or len(digest) != 64 or sha256_file(base / relative) != digest:
            raise ValueError(f'official snapshot digest mismatch: {relative}')
    spec = yaml.safe_load((base / 'api/openapi.yaml').read_text(encoding='utf-8'))
    operations = official_operations(spec)
    if len(operations) != 21:
        raise ValueError(f'expected exactly 21 frozen official operations, found {len(operations)}')
    return {'files_verified': sorted(files), 'operation_count': len(operations), 'provenance_limit': 'Checks preservation against the local snapshot manifest; not an organizer signature or external verification.'}


def check_ownership(root: Path) -> dict:
    spec = yaml.safe_load((root / 'spec/official/api/openapi.yaml').read_text(encoding='utf-8'))
    official = official_operations(spec)
    catalogue = load_json(root / 'contracts/api_ownership.json')
    rows = catalogue['operations']
    if not isinstance(rows, list):
        raise ValueError('operations must be a list')
    mapped = {(row['method'], row['path']) for row in rows}
    if len(rows) != 21 or len(mapped) != len(rows) or mapped != official:
        raise ValueError(f'catalogue mismatch: missing={sorted(official-mapped)}, extra={sorted(mapped-official)}, rows={len(rows)}')
    if len({row['operation_id'] for row in rows}) != len(rows):
        raise ValueError('operation IDs must be unique')
    workspaces = catalogue['workspaces']
    if len(workspaces) != 6:
        raise ValueError('catalogue must name the six product workspaces')
    for row in rows:
        name = f'{row["method"]} {row["path"]}'
        if row['owner'] not in {'A', 'B'}:
            raise ValueError(f'{name}: a single A/B owner is required')
        for field in ('domain_objects', 'workspaces', 'test_ids'):
            if not isinstance(row.get(field), list) or not row[field] or any(not meaningful(v) for v in row[field]):
                raise ValueError(f'{name}: {field} must explicitly map the operation')
        if not set(row['workspaces']).issubset(workspaces):
            raise ValueError(f'{name}: workspace references are not defined')
        events = row.get('events')
        if not isinstance(events, list) or any(not meaningful(v) for v in events):
            raise ValueError(f'{name}: events must be an explicit list')
        if not events and row.get('event_semantics') != 'read_only_no_domain_event':
            raise ValueError(f'{name}: no-event operations need explicit read-only semantics')
        if row['method'] != 'GET' and not events:
            raise ValueError(f'{name}: mutation needs planned committed-event mapping')
        if row['path'].startswith('/_test/'):
            if row.get('audience') != 'test_operator_only' or set(row['workspaces']) - {'events', 'acceptance'}:
                raise ValueError(f'{name}: test controls cannot be an ordinary contributor/user action')
        if row.get('implementation_status') not in {'IMPLEMENTED', 'CP0_STUB'}:
            raise ValueError(f'{name}: implementation status is not explicit')
        if row.get('verification_status') not in {'NOT_RUN', 'MET', 'NOT_MET'}:
            raise ValueError(f'{name}: verification status is not explicit')
    implemented = [f'{row["method"]} {row["path"]}' for row in rows if row['implementation_status'] == 'IMPLEMENTED']
    stubs = [f'{row["method"]} {row["path"]}' for row in rows if row['implementation_status'] == 'CP0_STUB']
    return {'operation_count': len(rows), 'declared_implemented': implemented, 'declared_stubs': stubs, 'runtime_verification': 'NOT_RUN by this static check'}


def check_semantics(root: Path) -> dict:
    config = load_json(root / 'configs/semantics.json')
    if config.get('official_source') != 'spec/official/api/openapi.yaml':
        raise ValueError('semantics must identify the frozen official source')
    rules = config['rules']
    if not isinstance(rules, list) or len(rules) < 12:
        raise ValueError('semantics must contain concrete rules for the declared uncertain behaviors')
    ids = []
    for rule in rules:
        rule_id = rule.get('id')
        ids.append(rule_id)
        if not meaningful(rule_id) or rule.get('source') not in {'official', 'team_provisional'}:
            raise ValueError('every semantic rule needs a unique ID and an official/team_provisional source')
        for field in ('reason', 'replace_at'):
            if not meaningful(rule.get(field)):
                raise ValueError(f'{rule_id}: {field} is missing or a placeholder')
        if 'value' not in rule or rule['value'] in ('', {}, []):
            raise ValueError(f'{rule_id}: a concrete provisional/official value is required')
        for field in ('operations', 'validation'):
            if not isinstance(rule.get(field), list) or not rule[field] or any(not meaningful(x) for x in rule[field]):
                raise ValueError(f'{rule_id}: {field} must contain concrete entries')
    if len(set(ids)) != len(ids):
        raise ValueError('semantic rule IDs must be unique')
    if not any(rule['source'] == 'team_provisional' for rule in rules):
        raise ValueError('unresolved official semantics cannot all be labeled official')
    return {'rule_count': len(rules), 'status': config.get('status'), 'implementation_validation': 'NOT_RUN; configuration review only'}


def check_fixtures(root: Path) -> dict:
    schema_path = root / 'contracts/generated/contracts.schema.json'
    schema = load_json(schema_path)
    jsonschema.Draft202012Validator.check_schema(schema)
    if not CONTRACT_NAMES.issubset(schema.get('$defs', {})):
        raise ValueError('generated schema must contain all eight shared contract definitions')
    manifest = load_json(root / 'contracts/examples/manifest.json')
    fixtures = manifest.get('fixtures', manifest.get('examples')) if isinstance(manifest, dict) else manifest
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError('fixture manifest must list explicit positive and negative examples')
    positive = 0
    negative = 0
    schema_negative = 0
    for fixture in fixtures:
        relative = Path(fixture['path'])
        path = root / relative if relative.parts[:2] == ('contracts', 'examples') else root / 'contracts/examples' / relative
        if not path.resolve().is_relative_to((root / 'contracts/examples').resolve()):
            raise ValueError('fixture paths must remain inside contracts/examples')
        data = load_json(path)
        contract = fixture.get('contract', 'ContractCatalog')
        selected_schema = schema if contract == 'ContractCatalog' else {'$ref': f'#/$defs/{contract}', '$defs': schema['$defs']}
        errors = list(jsonschema.Draft202012Validator(selected_schema).iter_errors(data))
        if fixture.get('valid') is True:
            if errors:
                raise ValueError(f'{fixture["path"]}: positive fixture fails schema: {errors[0].message}')
            positive += 1
        elif fixture.get('valid') is False:
            negative += 1
            if not meaningful(fixture.get('expected_error')):
                raise ValueError(f'{fixture["path"]}: negative fixture needs its expected error')
            if fixture.get('validation_layer') == 'json_schema_and_pydantic':
                if not errors:
                    raise ValueError(f'{fixture["path"]}: expected structural error was not rejected by JSON Schema')
                schema_negative += 1
        else:
            raise ValueError('each fixture must state valid true or false')
    if positive < 1 or negative < 1:
        raise ValueError('both positive and negative contract examples are required')
    return {'schema_valid_positive_examples': positive, 'schema_rejected_negative_examples': schema_negative, 'negative_examples_catalogued': negative, 'semantic_negative_validation': 'Executed by the Pydantic contract tests in the separate pytest step, not inferred here.'}


def check_docs(root: Path) -> dict:
    for relative in REQUIRED_DOCS:
        path = root / relative
        if not path.is_file() or len(path.read_text(encoding='utf-8').strip()) < 80:
            raise ValueError(f'missing or empty design/handoff documentation: {relative}')
    return {'required_paths_present': list(REQUIRED_DOCS)}


def check_checkpoints(root: Path) -> dict:
    config = load_json(root / 'configs/checkpoints.json')
    checkpoints = config['checkpoints']
    if set(checkpoints) != {f'CP{i}' for i in range(6)}:
        raise ValueError('checkpoints must contain CP0 through CP5')
    count = 0
    for name, checkpoint in checkpoints.items():
        if checkpoint.get('status') not in {'NOT_RUN', 'MET', 'NOT_MET'}:
            raise ValueError(f'{name}: invalid declared status')
        checks = checkpoint.get('required_checks')
        if not isinstance(checks, list) or not checks:
            raise ValueError(f'{name}: explicit required checks are missing')
        if len({c['id'] for c in checks}) != len(checks):
            raise ValueError(f'{name}: duplicate check IDs')
        for check in checks:
            if check.get('status') not in {'NOT_RUN', 'MET', 'NOT_MET'} or not meaningful(check.get('expected')) or not check.get('allowed_evidence_kinds'):
                raise ValueError(f'{name}/{check.get("id")}: check lacks explicit status, expectation or evidence kind')
        count += len(checks)
        if name != 'CP0' and (checkpoint['status'] == 'MET' or any(c['status'] == 'MET' for c in checks)):
            gate = evaluate_checkpoint(root, name)
            if gate['status'] != 'MET':
                raise ValueError(f'{name}: declared MET without valid current-build runtime evidence: {gate["errors"]}')
    return {'checkpoint_count': len(checkpoints), 'required_check_count': count, 'product_status': 'NOT_RUN unless a separate current-build runtime evidence receipt is validated'}


def run_checks(root: Path) -> dict:
    checks = []
    for name, function in [('CP0-OFFICIAL-INTEGRITY', check_official), ('CP0-API-MAPPING', check_ownership), ('CP0-SEMANTICS', check_semantics), ('CP0-CONTRACT-FIXTURES', check_fixtures), ('CP0-DOCUMENTATION', check_docs), ('CP0-EVIDENCE-POLICY', check_checkpoints)]:
        try:
            detail = function(root)
            checks.append({'id': name, 'status': 'MET', 'actual': detail})
        except (OSError, ValueError, KeyError, TypeError, AttributeError, jsonschema.exceptions.SchemaError, yaml.YAMLError) as exc:
            checks.append({'id': name, 'status': 'NOT_MET', 'actual': str(exc)})
    return {'schema_version': '1.0.0', 'checkpoint': 'CP0', 'scope': 'static_design_only', 'status': 'MET' if all(check['status'] == 'MET' for check in checks) else 'NOT_MET', 'observed_at': datetime.now(timezone.utc).isoformat(), 'build': current_build(root), 'command': 'python scripts/check_cp0.py', 'claims': {'api_conformance': 'NOT_RUN', 'product_acceptance': 'NOT_RUN', 'real_cpu_recovery': 'NOT_RUN', 'external_validation': 'NOT_RUN'}, 'checks': checks}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path, help='Default: ROOT/evidence/cp0/static_receipt.json')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    receipt = run_checks(root)
    output = args.output or root / 'evidence/cp0/static_receipt.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'status': receipt['status'], 'scope': receipt['scope'], 'receipt': str(output), 'checks': [{'id': c['id'], 'status': c['status'], **({'actual': c['actual']} if c['status'] != 'MET' else {})} for c in receipt['checks']]}, ensure_ascii=False, indent=2))
    return 0 if receipt['status'] == 'MET' else 1


if __name__ == '__main__':
    sys.exit(main())
