/** Account/space integration regressions. Every response is synthetic and local;
 * this suite never connects to a Fabric instance or certifies human acceptance. */
import assert from 'node:assert/strict';
import {after, afterEach, beforeEach, test} from 'node:test';
import {JSDOM} from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/?visual=classic#overview', pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'navigator', 'location', 'Element', 'HTMLElement',
  'HTMLInputElement', 'HTMLTextAreaElement', 'Event', 'MouseEvent', 'HashChangeEvent', 'FormData']) {
  Object.defineProperty(globalThis, name, {value: dom.window[name], configurable: true, writable: true});
}
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window);
dom.window.HTMLElement.prototype.scrollIntoView = function() {};
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const React = await import('react');
const {act} = React;
const {createRoot} = await import('react-dom/client');
const {default: App} = await import('../src/App.tsx');
const {ApiClient, ApiError, apiErrorCode} = await import('../src/api.ts');
const originalFetch = globalThis.fetch;

const snapshot = {version: {epoch: 0, revision: 7}, now: 10};
const account = (owner, capabilities = ['use_compute']) => ({actor_id: `actor-${owner}`,
  owner_id: owner, capabilities, is_administrator: false});
const work = owner => ({intent_id: `${owner}-private-work`, owner_id: owner, state: 'running',
  execution_mode: 'logical_simulation', settlement_action: 'keep_lease',
  allocation_ids: [`${owner}-lease`], specification: {job: {image: `${owner}-private-image`}}});
const node = owner => ({node_id: `${owner}-node`, contributor_owner_id: owner, state: 'Ready',
  capacity: {cpu_millicores: 1000, memory_mib: 4096}, devices: [{device_id: `${owner}-gpu`,
    physical_gpu_id: `${owner}-gpu`, model: 'A10', state: 'available'}]});
const json = (data, status = 200) => new Response(JSON.stringify(data), {
  status, headers: {'Content-Type': 'application/json'},
});
const deferred = () => {let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve};};
let root, container, requests, sessions, intercept;

function defaults(path, owner) {
  if (path === '/healthz') return json({control_plane_ready: true});
  if (path === '/v1/version') return json({checkpoint: 'CP4', execution_mode: 'logical_simulation', capabilities: {checkpoint_recovery: true}});
  if (path === '/x/v1/session') return json(sessions.get(owner) || account(owner));
  if (path.startsWith('/x/v1/work-intents?')) return json({...snapshot, items: [work(owner)]});
  if (path.startsWith('/v1/allocations?')) return json({...snapshot, items: [{allocation_id: `${owner}-lease`, state: 'Running'}], legal_actions: []});
  if (path.startsWith('/v1/jobs?')) return json({...snapshot, items: []});
  if (path.startsWith('/x/v1/contributions?')) return json({...snapshot, items: [node(owner)]});
  if (path === '/v1/capacity') return json({...snapshot, nodes: [], totals: {}});
  if (path.startsWith('/v1/events?')) return json({...snapshot, items: []});
  throw new Error(`Unexpected synthetic request: ${path}`);
}
async function interact(action) {
  await act(async () => {action(); await new Promise(resolve => setTimeout(resolve, 5));});
}
async function mount(hash = '#overview') {
  dom.window.history.replaceState(null, '', `/?visual=classic${hash}`);
  await act(async () => {root.render(<App/>);});
}
function button(label, scope = container) {
  const found = [...scope.querySelectorAll('button')].find(element => element.textContent === label);
  assert.ok(found, `button ${label} exists`); return found;
}
async function enter(element, value) {
  const prototype = element instanceof dom.window.HTMLTextAreaElement ? dom.window.HTMLTextAreaElement.prototype : dom.window.HTMLInputElement.prototype;
  await interact(() => {
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, value);
    element.dispatchEvent(new dom.window.Event('input', {bubbles: true}));
  });
}
async function profiles() {
  const input = container.querySelector('input[type=file]');
  const contents = JSON.stringify({
    // The local name "admin" deliberately disagrees with server authority.
    admin: {'X-Owner': 'alice', Authorization: 'Bearer synthetic-alice'},
    bob: {'X-Owner': 'bob', Authorization: 'Bearer synthetic-bob'},
    contributor: {'X-Owner': 'contributor', Authorization: 'Bearer synthetic-contributor'},
  });
  Object.defineProperty(input, 'files', {value: [{name: 'synthetic-identities.json', size: contents.length,
    text: async () => contents}], configurable: true});
  await interact(() => input.dispatchEvent(new dom.window.Event('change', {bubbles: true})));
}
async function choose(profile = 'admin') {
  const select = container.querySelector('.identity-panel select');
  assert.ok(select, 'local identities imported');
  await interact(() => {select.value = profile; select.dispatchEvent(new dom.window.Event('change', {bubbles: true}));});
}
async function connect(profile = 'admin') {await profiles(); await choose(profile);}
function writes() {return requests.filter(request => request.method !== 'GET');}
function workbench() {return container.querySelector('[data-space-scroll="workbench"]');}
async function space(name) {await interact(() => button(name, container.querySelector('.space-switch')).click());}

beforeEach(() => {
  document.body.innerHTML = '<div id="account-workspace-tests"></div>';
  container = document.getElementById('account-workspace-tests'); root = createRoot(container);
  requests = []; sessions = new Map([['alice', account('alice')], ['bob', account('bob')],
    ['contributor', account('contributor', ['contribute_resources'])]]); intercept = null;
  globalThis.fetch = async (path, options = {}) => {
    const headers = new Headers(options.headers);
    const request = {path: String(path), owner: headers.get('X-Owner') || '',
      method: options.method || 'GET', signal: options.signal};
    requests.push(request);
    if (request.method !== 'GET') throw new Error('This navigation suite prohibits business writes.');
    return await intercept?.(request) || defaults(request.path, request.owner);
  };
});
afterEach(async () => {
  await act(async () => root.unmount()); globalThis.fetch = originalFetch;
  assert.equal(writes().length, 0, 'no business write reached the synthetic network boundary');
});
after(() => dom.window.close());

test('a local admin label grants no workspace before a trusted session and no administrator capability afterward', async () => {
  const pending = deferred();
  intercept = request => request.path === '/x/v1/session' ? pending.promise : undefined;
  await mount(); await connect();
  assert.match(container.textContent, /正在连接/);
  assert.equal(container.querySelector('.space-body'), null);
  assert.deepEqual(requests.filter(request => request.path.startsWith('/x/v1/')).map(request => request.path), ['/x/v1/session']);
  await act(async () => pending.resolve(json(account('alice'))));
  const navigation = container.querySelector('nav[aria-label="工作台导航"]');
  assert.match(navigation.textContent, /我的工作/);
  assert.doesNotMatch(navigation.textContent, /系统管理|实验与报告|我的设备/);
  assert.match(container.textContent, /alice-private-work/);
  assert.equal(dom.window.localStorage.length, 0); assert.equal(dom.window.sessionStorage.length, 0);
});

test('a pure administrator opening #run sees the capability notice and receives no compute workspace', async () => {
  sessions.set('alice', {actor_id: 'pure-administrator', owner_id: null,
    capabilities: [], is_administrator: true});
  await mount('#run?new=1'); await connect();
  assert.match(workbench().textContent, /此账户未获得该能力/);
  assert.equal(workbench().querySelector('input[name=cpu]'), null);
  assert.doesNotMatch(workbench().textContent, /提交一项工作/);
  assert.equal(requests.some(request => /work-intents|\/v1\/jobs|\/v1\/allocations/.test(request.path)), false);
  const navigation = container.querySelector('nav[aria-label="工作台导航"]');
  assert.match(navigation.textContent, /系统管理/);
  assert.doesNotMatch(navigation.textContent, /我的工作/);
});

test('changing identity clears prior data immediately, aborts hidden reads, and ignores a late old-owner response', async () => {
  const old = deferred(), newSession = deferred(); let oldRequest;
  intercept = request => {
    if (request.owner === 'alice' && request.path.startsWith('/x/v1/work-intents?')) {oldRequest = request; return old.promise;}
    if (request.owner === 'bob' && request.path === '/x/v1/session') return newSession.promise;
  };
  await mount('#run?new=1'); await connect();
  const draft = workbench().querySelector('input[name=cpu]'); await enter(draft, '2048');
  await choose('bob');
  assert.equal(oldRequest.signal.aborted, true);
  assert.equal(container.querySelector('.space-body'), null, 'old workspace is not faded out under a new identity');
  await act(async () => old.resolve(json({...snapshot, items: [work('alice')]})));
  assert.doesNotMatch(container.textContent, /alice-private-work|alice-private-image/);
  await act(async () => newSession.resolve(json(account('bob'))));
  assert.match(workbench().textContent, /bob-private-work/);
  assert.doesNotMatch(workbench().textContent, /alice-private-work|alice-private-image/);
  assert.equal(workbench().querySelector('input[name=cpu]').value, '1000', 'drafts do not cross identities');
});

test('temporary session failure retains drafts but disables submission, including handlers invoked outside native button activation', async () => {
  let unhealthy = true, failSession = false;
  intercept = request => request.path === '/healthz' && unhealthy ? json({error: 'unavailable'}, 503)
    : request.path === '/x/v1/session' && failSession ? json({error: 'temporarily_unavailable'}, 503) : undefined;
  await mount('#run?new=1'); await connect();
  await interact(()=>{workbench().querySelector('.work-supporting-records').open=true;});
  const originalWorkspace = workbench(), draft = originalWorkspace.querySelector('input[name=cpu]');
  await enter(draft, '2048');
  const filter = originalWorkspace.querySelector('[aria-label="筛选当前页工作"]'); await enter(filter, 'private-image');
  failSession = true; unhealthy = false;
  await interact(() => button('重新连接').click());
  assert.equal(workbench(), originalWorkspace); assert.equal(workbench().querySelector('input[name=cpu]'), draft);
  assert.equal(draft.value, '2048'); assert.equal(filter.value, 'private-image');
  assert.equal(draft.matches(':disabled'), true);
  assert.match(container.textContent, /账户连接暂未确认.*暂停提交/s);
  const renew = button('续租 300 秒', workbench());
  await interact(() => renew.dispatchEvent(new dom.window.MouseEvent('click', {bubbles: true, cancelable: true})));
  assert.equal(writes().length, 0, 'ApiClient rejects the write even when native disabled activation is bypassed');
  assert.match(workbench().textContent, /账户能力正在重新确认/);
  failSession = false;
  await interact(() => button('重新检查账户').click());
  assert.equal(workbench(), originalWorkspace); assert.equal(draft.value, '2048');
  assert.equal(draft.matches(':disabled'), false);
});

test('session authorization rejection clears the prior workspace instead of preserving privileged content', async () => {
  let unhealthy = true, revoked = false;
  intercept = request => request.path === '/healthz' && unhealthy ? json({error: 'unavailable'}, 503)
    : request.path === '/x/v1/session' && revoked ? json({error: 'forbidden'}, 403) : undefined;
  await mount('#run?new=1'); await connect();
  assert.match(workbench().textContent, /alice-private-work/);
  revoked = true; unhealthy = false;
  await interact(() => button('重新连接').click());
  assert.equal(container.querySelector('.space-body'), null);
  assert.doesNotMatch(container.textContent, /alice-private-image|alice-private-work/);
  assert.match(container.textContent, /HTTP 403/);
});

for (const entry of ['space button', 'sidebar link']) {
  test(`a contribution-only account enters its devices through the ${entry}`, async () => {
    await mount(); await connect('contributor');
    if (entry === 'space button') await space('工作台');
    else await interact(() => container.querySelector('nav[aria-label="工作台导航"] a[href="#resources"]').click());
    assert.equal(location.hash, '#resources');
    assert.equal(workbench().hidden, false); assert.match(workbench().textContent, /contributor-node/);
    assert.doesNotMatch(workbench().textContent, /此账户未获得该能力|提交一项工作/);
    assert.equal(requests.some(request => /work-intents|\/v1\/jobs|\/v1\/allocations/.test(request.path)), false);
  });
}

test('device drafts and disclosure state survive an overview roundtrip without remounting or issuing writes', async () => {
  await mount('#resources'); await connect('contributor');
  const notice = workbench().querySelector('input[name=notice]'); await enter(notice, '47');
  const configuration = workbench().querySelector('textarea[name=node]');
  await enter(configuration, '{"node_id":"unsubmitted-device-draft"}');
  const disclosure = configuration.closest('details'); await interact(() => {disclosure.open = true;});
  await space('总览'); assert.equal(workbench().hidden, true);
  await space('工作台');
  assert.equal(workbench().querySelector('input[name=notice]'), notice); assert.equal(notice.value, '47');
  assert.equal(workbench().querySelector('textarea[name=node]'), configuration);
  assert.equal(configuration.value, '{"node_id":"unsubmitted-device-draft"}'); assert.equal(disclosure.open, true);
});

test('hidden workbench reads abort while the form and filters remain available on return', async () => {
  const pending = deferred(); let allocationRequest;
  intercept = request => {
    if (request.path.startsWith('/v1/allocations?') && !allocationRequest) {allocationRequest = request; return pending.promise;}
  };
  await mount('#run?new=1'); await connect();
  await interact(()=>{workbench().querySelector('.work-supporting-records').open=true;});
  const draft = workbench().querySelector('input[name=cpu]'); await enter(draft, '3072');
  const filter = workbench().querySelector('[aria-label="筛选当前页工作"]'); await enter(filter, 'private-image');
  await space('总览'); assert.equal(allocationRequest.signal.aborted, true);
  await act(async () => pending.resolve(json({...snapshot, items: [{allocation_id: 'late-hidden-allocation', state: 'Running'}]})));
  assert.doesNotMatch(container.textContent, /late-hidden-allocation/);
  await space('工作台');
  assert.equal(workbench().querySelector('input[name=cpu]'), draft); assert.equal(draft.value, '3072');
  assert.equal(filter.value, 'private-image');
});

test('a new epoch resets workspace context and rejects both late and newly returned older-epoch data', async () => {
  const oldAllocation = deferred(); let currentEpoch = 0, pendingRequest, staleWork = false;
  intercept = request => {
    if (request.path.startsWith('/x/v1/work-intents?')) {
      const epoch = staleWork ? 0 : currentEpoch;
      return json({version: {epoch, revision: 7}, now: 10, items: [{...work('alice'),
        intent_id: `epoch-${epoch}-work`, specification: {job: {image: `epoch-${epoch}-image`}}}]});
    }
    if (request.path.startsWith('/v1/allocations?')) {
      if (currentEpoch === 1 && !pendingRequest) {pendingRequest = request; return oldAllocation.promise;}
      return json({version: {epoch: currentEpoch, revision: 7}, now: 10, items: []});
    }
    if (request.path.startsWith('/v1/jobs?')) return json({version: {epoch: currentEpoch, revision: 7}, now: 10, items: []});
  };
  await mount('#run?new=1'); await connect();
  await interact(()=>{workbench().querySelector('.work-supporting-records').open=true;});
  const previousWorkspace = workbench(), draft = previousWorkspace.querySelector('input[name=cpu]');
  await enter(draft, '6144');
  assert.match(previousWorkspace.textContent, /epoch-0-work/);
  currentEpoch = 1;
  await interact(() => button('刷新', previousWorkspace.querySelector('.panel')).click());
  assert.notEqual(workbench(), previousWorkspace, 'a reset does not retain the previous instance context');
  assert.equal(workbench().querySelector('input[name=cpu]').value, '1000');
  assert.match(workbench().textContent, /epoch-1-work/);
  await interact(()=>{workbench().querySelector('.work-supporting-records').open=true;});
  await space('总览');
  assert.equal(pendingRequest.signal.aborted, true);
  await space('工作台');
  await act(async () => oldAllocation.resolve(json({...snapshot, items: [{allocation_id: 'epoch-zero-late-lease', state: 'Running'}]})));
  assert.doesNotMatch(container.textContent, /epoch-zero-late-lease|epoch-0-image/);
  staleWork = true;
  await interact(() => button('刷新', workbench().querySelector('.panel')).click());
  assert.match(workbench().textContent, /实例代次已更新/);
  assert.doesNotMatch(workbench().textContent, /epoch-0-image|epoch-1-image/);
});

test('the account write guard blocks direct mutations and composite actions before fetch, while reads remain usable', async () => {
  let confirmed = false, busy = 0;
  const api = new ApiClient({owner: 'alice', token: 'synthetic-alice'}, amount => {busy += amount;}, () => confirmed);
  for (const operation of [() => api.request('/x/v1/contributions/recall', {method: 'POST', body: {}}),
    () => api.request('/v1/allocations/alice-lease', {method: 'DELETE'}),
    () => api.registerWork({intent_id: 'unsubmitted'}),
    () => api.workAction('alice-private-work', 'release', snapshot.version)]) {
    await assert.rejects(operation, error => error instanceof ApiError && apiErrorCode(error) === 'account_unconfirmed');
    assert.equal(busy, 0);
  }
  assert.equal(requests.length, 0);
  await api.request('/healthz'); assert.equal(requests.length, 1);
});


test('one primary connection flow and account menu keep preferences out of the header',async()=>{
  await mount();
  assert.equal(container.querySelector('.identity-manual').closest('[hidden]'),null);
  assert.equal(container.querySelector('.identity-import').open,false);
  await enter(container.querySelector('input[name=account]'),'alice');
  await enter(container.querySelector('input[name=credential]'),'synthetic-alice');
  await interact(()=>button('连接账户',container.querySelector('.identity-manual')).click());
  assert.ok(container.querySelector('.space-body'));
  const menu=container.querySelector('#account-panel');assert.equal(menu.hidden,true);
  assert.equal(container.querySelector('.topbar').querySelector('.sw-motion-control'),null);
  assert.doesNotMatch(container.querySelector('.topbar').textContent,/显示设置|显示偏好/);
  assert.doesNotMatch(container.querySelector('.sidebar').textContent,/临时共享|持续接续/);
  await interact(()=>container.querySelector('.account-trigger').click());
  assert.equal(menu.hidden,false);assert.equal(menu.querySelector('.account-preferences').open,false);
  assert.match(menu.textContent,/逻辑模拟/);
  await interact(()=>document.dispatchEvent(new dom.window.KeyboardEvent('keydown',{key:'Escape',bubbles:true})));
  assert.equal(menu.hidden,true);assert.equal(document.activeElement,container.querySelector('.account-trigger'));
  await interact(()=>container.querySelector('.account-trigger').click());
  await interact(()=>button('切换账户',menu).click());
  assert.equal(container.querySelector('.identity-manual').closest('[hidden]'),null);
  await interact(()=>container.querySelector('.account-trigger').click());
  await interact(()=>container.querySelector('.account-trigger').click());
  await interact(()=>button('断开连接',menu).click());
  assert.equal(container.querySelector('.space-body'),null);
  assert.equal(container.querySelector('input[name=credential]').value,'');
  assert.equal(dom.window.localStorage.length,0);assert.equal(dom.window.sessionStorage.length,0);
});
