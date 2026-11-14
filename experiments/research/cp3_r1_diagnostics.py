#!/usr/bin/env python3
"""Read sealed CP3 evidence; never execute a scenario or modify source evidence.

Usage: python3 cp3_r1_diagnostics.py --suite OLD_SUITE --output /tmp/report.json
The old validation set is diagnostic evidence, not a new tuning/validation split.
"""
import argparse
import collections
import hashlib
import itertools
import json
from pathlib import Path


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def chosen(plan):
    return next((c for c in plan['candidates']
                 if c['candidate_id'] == plan['selected_candidate_id']), None)


def signature(candidate):
    if candidate is None:
        return None
    return {key: candidate[key] for key in ('demand_id', 'action', 'placement')}


def scores(candidate):
    return {s['name']: s['value'] for s in candidate['scores']}


def baseline_key(candidate):
    s = scores(candidate)
    p = candidate['placement']['slices'][0]
    return (s['compatibility_pressure'], s['remaining_gpu_count'],
            s['excess_gpu_memory_mib'], s['remaining_cpu_millicores'],
            s['remaining_memory_mib'], p['node_id'], p['device_ids'])


def current_effect_class(candidate, snapshot):
    p = candidate['placement']['slices'][0]
    node = next(n for n in snapshot['inventory'] if n['node_id'] == p['node_id'])
    devices = {d['device_id']: d for d in node['devices']}
    shapes = [(devices[i]['model'], devices[i]['memory_mib'], devices[i]['profile'])
              for i in p['device_ids']]
    return json.dumps({'node': p['node_id'], 'resources': p['resources'],
                       'device_shapes': sorted(shapes, key=str)}, sort_keys=True)


def area(work, horizon):
    end = work['completed_at'] if work['status'] == 'completed' else horizon
    return max(0, min(end, horizon) - work['arrived_at'])


def pack_witness(candidate, row):
    """Sufficient current-state witness only, not a gain or fairness proof."""
    p = candidate['placement']['slices'][0]
    if p['resources']['gpu_count'] != 2:
        return None
    node = next(n for n in row['snapshot']['inventory'] if n['node_id'] == p['node_id'])
    devices = {d['device_id']: d for d in node['devices']}
    if any(devices[i]['profile'] is not None for i in p['device_ids']):
        return None
    alternatives = []
    for c in row['plan']['candidates']:
        if c['feasibility'] != 'legal' or c['demand_id'] == candidate['demand_id']:
            continue
        parts = c['placement']['slices']
        if len(parts) != 1 or parts[0]['node_id'] != node['node_id']:
            continue
        ids = parts[0]['device_ids']
        if len(ids) == 1 and devices[ids[0]]['profile'] is None:
            alternatives.append(c)
    for a, b in itertools.combinations(alternatives, 2):
        ap, bp = a['placement']['slices'][0], b['placement']['slices'][0]
        if a['demand_id'] == b['demand_id'] or ap['device_ids'] == bp['device_ids']:
            continue
        fields = ('cpu_millicores', 'memory_mib', 'ephemeral_storage_mib',
                  'gpu_count', 'gpu_memory_mib')
        if all(ap['resources'][k] + bp['resources'][k] <= node['available'][k]
               for k in fields):
            return {'alternative_works': [a['intent_id'], b['intent_id']],
                    'alternative_owners': [a['owner_id'], b['owner_id']],
                    'alternative_placements': [a['placement'], b['placement']]}
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    suite = args.suite.resolve()
    if args.output.resolve() == suite or suite in args.output.resolve().parents:
        raise SystemExit('Refuse to write into the sealed source suite')
    index = json.loads((suite / 'suite-index.json').read_bytes())
    manifest_raw = (suite / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_raw)
    assert digest(manifest_raw) == index['manifest_sha256'] == (suite / 'manifest.sha256').read_text().strip()
    assert manifest['partition'] == 'validation' and index['expected_runs'] == 125
    refs = {a['path']: a['sha256'] for r in index['runs'] for a in r['artifacts'].values()}
    artifacts_read = []

    def read(relative):
        raw = (suite / relative).read_bytes()
        actual = digest(raw)
        assert actual == refs[relative], relative
        artifacts_read.append({'path': relative, 'sha256': actual})
        return json.loads(raw)

    result = {'schema': 'cp3-r1-readonly-diagnostics-v1', 'build': manifest['build'],
              'source_suite': str(suite), 'global': collections.Counter(),
              'by_family': {}, 'pairs': [], 'changed_actions': [],
              'pack_witnesses': [], 'B2_F_by_family': {}, 'artifacts_read': artifacts_read,
              'limitations': [
                  'No scenario rerun; outcome differences are from sealed paired runs.',
                  'Current effect classes ignore interchangeable current device IDs; unknown future recalls may distinguish them.',
                  'Packing witnesses establish legal coexistence, not end-to-end gain or fairness.',
                  'Completed F minus 10 per completed work includes queueing, interruption and other elapsed time; it is not pure waiting.',
                  'Failed/unfinished horizon-minus-arrival contributions are metric shares, not automatically irreducible lower bounds.',
                  'A freeze of the current placement approach does not prove all scheduling approaches lack headroom.']}
    for record in index['runs']:
        if record['policy'] != 'P':
            continue
        run_id, family = record['run_id'], record['scenario_family']
        prefix = run_id.rsplit('--', 1)[0]
        rows = read(record['artifacts']['decisions']['path'])
        b_rows = read(f'runs/{prefix}--B2/decisions.json')
        p = read(f'runs/{run_id}/summary.json')
        b = read(f'runs/{prefix}--B2/summary.json')
        off = read(f'runs/{prefix}--P-no-opportunity/summary.json')
        family_counts = result['by_family'].setdefault(family, collections.Counter())
        for row_no, row in enumerate(rows):
            plan, snap = row['plan'], row['snapshot']
            legal = [c for c in plan['candidates'] if c['feasibility'] == 'legal']
            selected = chosen(plan)
            c = collections.Counter(plans=1)
            c['candidate_budget_truncations'] = 'Candidate budget' in (plan['coverage']['truncation_reason'] or '')
            c['any_nonzero_opportunity'] = any(scores(x)['observed_opportunity_loss'] > 0 for x in legal)
            c['history_or_metadata_present'] = bool(snap['history'] or snap['metadata'] or any(d['known_data'] for d in snap['demands']))
            if selected:
                c['selected'] = 1
                alternatives = [x for x in legal if x['demand_id'] == selected['demand_id']]
                values = [scores(x)['observed_opportunity_loss'] for x in alternatives]
                c['admitted_multi_candidate'] = len(alternatives) > 1
                c['admitted_multiple_current_effect_classes'] = len({current_effect_class(x, snap) for x in alternatives}) > 1
                c['admitted_score_varies'] = max(values) > min(values)
                c['admitted_score_all_zero'] = all(v == 0 for v in values)
                c['admitted_score_nonzero_equal'] = len(set(values)) == 1 and values[0] > 0
                same_state_b2 = min(alternatives, key=baseline_key)
                changed = selected['candidate_id'] != same_state_b2['candidate_id']
                c['changed_action'] = changed
                assert changed == row['same_state_diagnostic']['action_changed_from_b2']
                assert signature(same_state_b2) == row['same_state_diagnostic']['same_state_b2_action']
                if changed:
                    result['changed_actions'].append({'run_id': run_id, 'row': row_no,
                        'time': snap['now'], 'work': selected['intent_id'],
                        'P': signature(selected), 'same_state_B2': signature(same_state_b2)})
                witness = pack_witness(selected, row)
                if witness:
                    c['two_one_gpu_instead_of_selected_two_gpu_witnesses'] = 1
                    result['pack_witnesses'].append({'run_id': run_id, 'row': row_no,
                        'time': snap['now'], 'chosen_work': selected['intent_id'], **witness})
            else:
                c['no_selection'] = 1
            family_counts.update(c)
            result['global'].update(c)
        def action_rows(rs):
            return [(r['snapshot']['now'], signature(chosen(r['plan'])))
                    for r in rs if chosen(r['plan']) is not None]
        pa, ba = action_rows(rows), action_rows(b_rows)
        differences = [{'work': pw['intent_id'], 'fields': {k: {'P': pw[k], 'B2': bw[k]}
                       for k in pw if pw[k] != bw[k]}}
                       for pw, bw in zip(p['works'], b['works']) if pw != bw]
        area_p = sum(area(w, p['horizon']) for w in p['works'])
        area_b = sum(area(w, b['horizon']) for w in b['works'])
        result['pairs'].append({'family': family, 'seed': record['seed'],
            'P_area': area_p, 'B2_area': area_b, 'R': area_p / area_b,
            'work_records_equal': p['works'] == b['works'], 'work_differences': differences,
            'full_actions_equal': pa == ba,
            'admission_time_order_equal': [(t, a['demand_id']) for t, a in pa] == [(t, a['demand_id']) for t, a in ba],
            'off_works_equal_B2': off['works'] == b['works'],
            'event_counts_equal': p['event_counts'] == b['event_counts'],
            'occupancy_integrals_equal': p['occupancy_integrals'] == b['occupancy_integrals'],
            'P_plan_elapsed_ns': p['instrumentation']['plan_elapsed_ns'],
            'B2_plan_elapsed_ns': b['instrumentation']['plan_elapsed_ns']})
        f = result['B2_F_by_family'].setdefault(family, collections.Counter())
        f['runs'] += 1
        for work in b['works']:
            contribution = area(work, b['horizon'])
            f['total_F'] += contribution
            f['works'] += 1
            f[work['status'] + '_count'] += 1
            if work['status'] == 'completed':
                f['completed_fixed_10s_service_base'] += 10
                f['completed_queueing_interruption_other_elapsed'] += contribution - 10
                f['completed_first_grant_wait'] += work['allocation_wait_seconds']
            else:
                f[work['status'] + '_H_minus_arrival'] += contribution
    for f in result['B2_F_by_family'].values():
        for name in ('failed', 'unfinished', 'cancelled'):
            f[name + '_F_fraction'] = f[name + '_H_minus_arrival'] / f['total_F']
        assert f['total_F'] == (f['completed_fixed_10s_service_base'] +
            f['completed_queueing_interruption_other_elapsed'] +
            sum(f[name + '_H_minus_arrival'] for name in ('failed', 'unfinished', 'cancelled')))
    result['pair_summary'] = {'pairs': len(result['pairs']),
        'work_records_equal_pairs': sum(p['work_records_equal'] for p in result['pairs']),
        'work_records_different': sum(len(p['work_differences']) for p in result['pairs']),
        'full_action_sequences_equal_pairs': sum(p['full_actions_equal'] for p in result['pairs']),
        'admission_time_sequences_equal_pairs': sum(p['admission_time_order_equal'] for p in result['pairs']),
        'all_R_exactly_1': all(p['R'] == 1 for p in result['pairs'])}
    result['registered_digests_all_match'] = True
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'output': str(args.output), 'global': result['global'],
                     'pair_summary': result['pair_summary'],
                     'B2_F_by_family': result['B2_F_by_family']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
