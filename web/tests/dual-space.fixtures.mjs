/** Browser acceptance fixtures only. No request in this module reaches a Fabric API.
 * Session/capability projections are synthetic tests of the UI, not real permissions.
 * Extends the previous visual harness without modifying its evidence or fixtures.
 */
import {installFixtures as installBaseFixtures} from './starwave.fixtures.mjs';

export const ids = Object.freeze({
  active: 'fixture-alice-active', waiting: 'fixture-alice-waiting',
  attention: 'fixture-alice-attention', completed: 'fixture-alice-completed',
  retained: 'fixture-alice-retained', bob: 'fixture-bob-active',
  notice: 'fixture-alice-recall-notice', stopping: 'fixture-alice-stopping',
  resumable: 'fixture-alice-resumable',
});
const snapshot = {version: {epoch: 0, revision: 42}, now: 33};
const clone = value => structuredClone(value);

function work(id, state, image, extra = {}) {
  return {intent_id: id, owner_id: id.includes('-bob-') ? 'bob' : 'alice', state,
    first_arrival_at: 3, execution_mode: 'logical_simulation',
    settlement_action: 'release_on_success', allocation_ids: [`${id}-lease`],
    job_ids: [`${id}-job`], attempts: [], stages: [],
    specification: {
      allocation: {max_restarts: 2, preemption_mode: 'checkpoint', ttl_seconds: 300},
      job: {image, command: ['python', '-V'], max_retries: 0},
    }, ...extra};
}
export const sampleWorks = [
  work(ids.active, 'running', '校园实验室多模态识别验证任务 / research-validation-2026.py'),
  work(ids.waiting, 'waiting', 'checkpoint-recovery-waiting.py', {
    attempts: [{ref: {job_id: `${ids.waiting}-job`, attempt_id: `${ids.waiting}-attempt-1`, generation: 1},
      state: 'stopped', created_at: 3, started_at: 3, finished_at: 13,
      checkpoint: {report_status: 'ready', reported_at: 12, verification_status: 'verified',
        continuation_status: 'not_attempted'}}],
  }),
  work(ids.attention, 'manual_action', '任务创建失败，需要检查已有租约', {
    stages: [{stage_id: 'fixture-stage-failed', operation: 'workflow.create_job_and_bind',
      state: 'failed', receipt: {outcome: 'rejected', message: 'Synthetic job creation failure'}}],
    job_ids: [],
  }),
  work(ids.retained, 'completed', '完成后保留租约的工作', {
    settlement_action: 'keep_lease', result: {kind: 'success', recorded_at: 31, artifact_uris: []},
  }),
  work(ids.completed, 'completed', '已完成的逻辑模拟工作', {
    result: {kind: 'success', recorded_at: 33, artifact_uris: []},
  }),
  work(ids.notice, 'running', '资源收回通知中的工作'),
  work(ids.stopping, 'running', '资源停止等待确认的工作'),
  work(ids.resumable, 'waiting', '已授权恢复的中断工作'),
  ...Array.from({length: 10}, (_, index) => work(`fixture-alice-queued-${index + 1}`, 'waiting',
    `排队工作 ${index + 1} / long-descriptive-analysis-dataset-${index + 1}.py`)),
  work(ids.bob, 'running', 'Bob 独立工作，仅 Bob 可见'),
];

export function sampleAllocation(item) {
  const completed = item.state === 'completed', released = completed && item.settlement_action !== 'keep_lease';
  const state = item.intent_id === ids.resumable ? 'Preempted'
    : [ids.notice, ids.stopping].includes(item.intent_id) ? 'PreemptRequested'
    : released ? 'Released' : item.state === 'waiting' ? 'Pending' : 'Running';
  return {allocation_id: item.allocation_ids[0], owner_id: item.owner_id, state,
    request: {cpu_millicores: 1000, memory_mib: 4096, gpu: {count: 1, model: 'A10', memory_mib: 0, profile: null}, ephemeral_storage_mib: 0},
    created_at: item.first_arrival_at, created_at_logical: item.first_arrival_at,
    expires_at: 300, expires_at_logical: 300, restart_count: item.state === 'waiting' ? 0 : 1,
    ...(state === 'PreemptRequested' ? {preemption: {requested_at: 30, deadline: 43, checkpoint_due: 40, reason: 'contributor_recall'}} : {}),
    ...(item.intent_id === ids.stopping ? {pending_terminal: 'Released'} : {}),
  };
}

function detail(item, currentSnapshot = snapshot) {
  const id = item.intent_id, completed = item.state === 'completed';
  const released = completed && item.settlement_action !== 'keep_lease';
  const waiting = item.state === 'waiting', action = item.state === 'manual_action';
  const allocation = sampleAllocation(item);
  const legal = released ? [] : waiting ? [] : [{operation: 'allocation.release',
    entity: {kind: 'allocation', entity_id: allocation.allocation_id}}];
  if (item.state === 'running') legal.push({operation: 'job.cancel', entity: {kind: 'job', entity_id: item.job_ids[0]}});
  if (action) legal.push({operation: 'workflow.create_job_and_bind', entity: {kind: 'work_intent', entity_id: id}});
  return {...clone(currentSnapshot), item: clone(item), allocations: [allocation],
    jobs: item.job_ids.map(job_id => ({job_id, state: completed ? 'Succeeded' : waiting ? 'Preempted' : 'Running',
      retry_count: 0, attempt: {generation: 1, attempt_id: `${id}-attempt-1`}})),
    legal_actions: legal, events: [{event_id: `${id}-event`, event_type: 'allocation.granted',
      sequence: 1, logical_time: 3, decision_id: 'fixture-decision-1'}]};
}

export async function installDualSpaceFixtures(context, origin) {
  const fixture = await installBaseFixtures(context, origin);
  fixture.works.clear();
  for (const item of sampleWorks) fixture.works.set(item.intent_id, clone(item));
  fixture.failures = new Map();
  fixture.emptyOwners = new Set();
  fixture.sessionOverrides = new Map();
  fixture.snapshot = clone(snapshot);
  fixture.allocationSnapshot = null;
  fixture.pending = [];
  fixture.holdNext = pathname => {
    let start, release;
    const entry = {pathname, started: new Promise(resolve => { start = resolve; }),
      wait: new Promise(resolve => { release = resolve; }), taken: false};
    entry.start = start; entry.release = release;
    fixture.pending.push(entry);
    return {started: entry.started, release};
  };
  fixture.releaseAll = () => { for (const entry of fixture.pending) entry.release(); };
  await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url()), path = url.pathname;
    if (url.origin !== origin || (path !== '/healthz' && !path.startsWith('/v1/') && !path.startsWith('/x/v1/'))) {
      return route.fallback(); // Base handler denies external requests and handles assets only.
    }
    const owner = request.headers()['x-owner'] || '';
    const token = request.headers().authorization || '';
    const responseSnapshot = clone(fixture.snapshot);
    const isAdmin = token === 'Bearer fixture-admin';
    const authenticated = isAdmin || ['alice', 'bob', 'empty'].includes(owner);
    const response = (data, status = 200, contentType = 'application/json') => route.fulfill({
      status, contentType, headers: {'X-Request-ID': 'synthetic-dual-space-fixture'},
      body: contentType === 'application/json' ? JSON.stringify(data) : String(data),
    });
    const record = () => fixture.requests.push({path: path + url.search, method: request.method(), owner,
      source: 'synthetic-dual-space-fixture'}); // Never record Authorization or identity file contents.
    if (request.method() !== 'GET') {
      record();
      return response({error: 'fixture_write_denied', detail: 'Navigation acceptance prohibits every business write.'}, 405);
    }
    const failure = fixture.failures.get(path);
    if (failure) {record(); return response(failure.body || {error: 'synthetic_failure', detail: 'Synthetic unavailable response'}, failure.status || 503, failure.contentType);}
    const gate = fixture.pending.find(entry => entry.pathname === path && !entry.taken);
    if (gate) {gate.taken = true; gate.start({path, owner}); await gate.wait;}
    if (fixture.delayMs) await new Promise(resolve => setTimeout(resolve, fixture.delayMs));
    if (path === '/healthz' || path === '/v1/version') return route.fallback();
    if (path === '/x/v1/session') {
      record();
      if (!authenticated) return response({error: 'unauthorized', detail: 'Identity authentication is required.'}, 401);
      if (fixture.sessionOverrides.has(owner)) return response(clone(fixture.sessionOverrides.get(owner)));
      return response({actor_id: isAdmin ? 'fixture-admin-actor' : `fixture-${owner}-actor`,
        owner_id: isAdmin ? null : owner, capabilities: ['use_compute', 'contribute_resources'],
        is_administrator: isAdmin});
    }
    if (!authenticated) {record(); return response({error: 'unauthorized', detail: 'Identity authentication is required.'}, 401);}
    if (path === '/x/v1/work-intents') {
      record(); const items = fixture.emptyOwners.has(owner) ? [] : [...fixture.works.values()].filter(item => item.owner_id === owner);
      return response({...responseSnapshot, items: clone(items), next_cursor: null});
    }
    if (path === '/v1/allocations') {
      record();
      const items = fixture.emptyOwners.has(owner) ? [] : [...fixture.works.values()].filter(item => item.owner_id === owner).map(sampleAllocation);
      const legal_actions = items.filter(item => item.allocation_id === `${ids.resumable}-lease`)
        .map(item => ({operation: 'allocation.resume', entity: {kind: 'allocation', entity_id: item.allocation_id}}));
      return response({...clone(fixture.allocationSnapshot || responseSnapshot), items, legal_actions, next_cursor: null});
    }
    if (path.startsWith('/x/v1/work-intents/')) {
      record(); const item = fixture.works.get(decodeURIComponent(path.split('/').at(-1)));
      if (!item || (item.owner_id !== owner && !isAdmin)) return response({error: 'not_found', detail: 'The requested object was not found.'}, 404);
      return response(detail(item, responseSnapshot));
    }
    if (path === '/x/v1/contributions') {
      record(); return response({...responseSnapshot, items: owner === 'alice' && !fixture.emptyOwners.has(owner) ? [{
        contribution_id: 'fixture-alice-contribution', node_id: 'fixture-alice-node', contributor_owner_id: 'alice',
        state: 'Ready', capacity: {gpu_count: 1, cpu_millicores: 8000, memory_mib: 32768},
        available: {gpu_count: 1, cpu_millicores: 8000, memory_mib: 32768},
        devices: [{device_id: 'fixture-alice-gpu', physical_gpu_id: 'fixture-alice-gpu', model: 'A10', memory_mib: 24576, state: 'available'}],
      }] : []});
    }
    if (path === '/x/v1/admin/overview' && !isAdmin) {
      record(); return response({error: 'forbidden', detail: 'Administrator authority is required.'}, 403);
    }
    return route.fallback(); // Remaining read-only responses remain fully synthetic in the base fixtures.
  });
  return fixture;
}
