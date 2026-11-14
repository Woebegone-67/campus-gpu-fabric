/** Synthetic browser-only data. Never forwarded to a running Fabric API.
 * This module is imported only by the acceptance runner, never by the product bundle.
 */
export async function installFixtures(context, origin) {
  const requests = [], denied = [], works = new Map();
  const base = {version: {epoch: 0, revision: 1}, now: 33};
  const finished = {intent_id: 'fixture-work-completed', owner_id: 'alice', state: 'completed',
    first_arrival_at: 0, execution_mode: 'logical_simulation', settlement_action: 'release_on_success',
    allocation_ids: ['fixture-lease-1'], job_ids: ['fixture-job-1'], attempts: [], stages: [],
    specification: {allocation: {max_restarts: 2}, job: {max_retries: 0, image: 'python:3.12-slim'}}};
  works.set(finished.intent_id, finished);
  const fixture = {requests, denied, delayMs: 0, works};
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url());
    if (url.origin !== origin) { denied.push(request.url()); await route.abort(); return; }
    const path = url.pathname;
    if (path !== '/healthz' && !path.startsWith('/v1/') && !path.startsWith('/x/v1/')) {
      if (request.method() !== 'GET') { denied.push(request.url()); await route.abort(); return; }
      await route.continue(); return;
    }
    const owner = request.headers()['x-owner'] || 'alice';
    const body = request.postDataJSON();
    requests.push({path: path + url.search, method: request.method(), owner, body, source: 'synthetic-browser-fixture'});
    if (fixture.delayMs) await new Promise(resolve => setTimeout(resolve, fixture.delayMs));
    const send = (data, status = 200) => route.fulfill({status, contentType: 'application/json',
      headers: {'X-Request-ID': 'synthetic-browser-fixture'}, body: JSON.stringify(data)});
    if (request.method() !== 'GET') {
      if (path === '/x/v1/work-intents' && request.method() === 'POST') {
        works.set(body.intent_id, {...body, state: 'running', first_arrival_at: 33,
          allocation_ids: ['fixture-lease-created'], job_ids: ['fixture-job-created'], attempts: [], stages: []});
        return send({receipt: {outcome: 'committed'}, fixture: true}, 201);
      }
      const match = path.match(/^\/x\/v1\/work-intents\/([^/]+)\/actions$/);
      if (match && request.method() === 'POST' && works.has(match[1])) {
        const work = works.get(match[1]);
        if (body.action === 'cancel') work.state = 'cancelled';
        else if (body.action === 'release') work.released = true;
        else return send({error: 'fixture_write_denied'}, 405);
        return send({receipt: {outcome: 'committed'}, fixture: true});
      }
      return send({error: 'fixture_write_denied', detail: 'This browser harness never sends business writes.'}, 405);
    }
    if (path === '/healthz') return send({status: 'ok', control_plane_ready: true});
    if (path === '/v1/version') return send({api_version: '2.0.0', implementation_version: 'browser-fixture',
      checkpoint: 'CP4', execution_mode: 'logical_simulation', capabilities: {contracts: true,
        resource_ledger: true, scheduler: true, checkpoint_recovery: true, real_gpu_execution: false, real_cpu_execution: false}});
    if (path === '/x/v1/work-intents') return send({...base, items: [...works.values()].filter(w => w.owner_id === owner)});
    if (path.startsWith('/x/v1/work-intents/')) {
      const work = works.get(path.split('/').at(-1));
      if (!work || work.owner_id !== owner) return send({error: 'not_found', detail: 'The requested object was not found.'}, 404);
      const released = work.released || work.state === 'completed';
      return send({...base, item: work,
        allocations: [{allocation_id: work.allocation_ids[0], state: released ? 'Released' : 'Running', restart_count: 1, expires_at_logical: 300}],
        jobs: [{job_id: work.job_ids[0], state: work.state === 'completed' ? 'Succeeded' : work.state === 'cancelled' ? 'Cancelled' : 'Running', retry_count: 0,
          attempt: {generation: 2, attempt_id: 'fixture-attempt-2'}}],
        legal_actions: released ? [] : [{operation: 'allocation.release'}, ...(work.state === 'running' ? [{operation: 'job.cancel'}] : [])],
        events: [{event_id: 'fixture-event-1', event_type: 'allocation.granted', logical_time: 23, sequence: 1, decision_id: 'fixture-decision-1'}]});
    }
    if (path === '/v1/capacity') {
      const device = (id, model, memory) => ({device_id: id, model, memory_mib: memory, state: 'available'});
      return send({...base, totals: {available: {gpu_count: 3, cpu_millicores: 56000, memory_mib: 229376},
        capacity: {gpu_count: 3, cpu_millicores: 56000, memory_mib: 229376}}, nodes: [
        {node_id: 'fixture-a10', state: 'Ready', available: {gpu_count: 2}, devices: [device('fixture-a10-gpu0','A10',24576),device('fixture-a10-gpu1','A10',24576)]},
        {node_id: 'fixture-a100', state: 'Ready', available: {gpu_count: 1}, devices: [device('fixture-a100-gpu0','A100',40960)]},
        {node_id: 'fixture-cpu', state: 'Ready', available: {gpu_count: 0}, devices: []}]});
    }
    if (path === '/x/v1/contributions' || path === '/x/v1/admin/overview') {
      return send({error: 'forbidden', detail: 'Contributor or administrator authority is required.'}, 403);
    }
    if (path.startsWith('/x/v1/decisions/')) return send({...base, scope: 'owner', item: {
      decision_id: 'fixture-decision-1', decision_at: 23, selected_candidate_id: 'fixture-candidate-1',
      policy: {name: 'P', version: '0.2.0'}, coverage: {scope: 'partial', examined_count: 2},
      candidates: [{candidate_id: 'fixture-candidate-1', action: 'allocate', feasibility: 'legal', violations: [], scores: []},
        {candidate_id: 'fixture-candidate-2', action: 'allocate', feasibility: 'infeasible', scores: [], violations: [{constraint: 'gpu_model',detail: 'Fixture GPU model does not match.'}]}]},
      actual_events: [{event_id: 'fixture-event-1', event_type: 'allocation.granted', logical_time: 23}], commit_receipts: []});
    if (path === '/v1/events') return send({...base, items: Array.from({length: 36}, (_, i) => ({
      event_id: `fixture-event-${i+1}`, sequence: i + 1, logical_time: i, event_type: i % 3 ? 'job.updated' : 'allocation.granted',
      decision_id: 'fixture-decision-1', entities: [{kind: 'work_intent', entity_id: finished.intent_id}]}))});
    if (path === '/x/v1/acceptance') return send({...base, items: [
      {target_id: 'FIXTURE-NOT-ACCEPTANCE', status: 'NOT_RUN', actual: '浏览器样例，不代表比赛验收'}]});
    return send({...base, items: [], fixture: true});
  });
  return fixture;
}
