/** Account/space integration regressions. Every response is synthetic and local;
 * this suite never connects to a Fabric instance or certifies human acceptance. */
import assert from 'node:assert/strict';
import {after, afterEach, beforeEach, test} from 'node:test';
import {JSDOM} from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/?visual=classic#overview', pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'navigator', 'location', 'Element', 'HTMLElement',
  'Node', 'HTMLInputElement', 'HTMLTextAreaElement', 'Event', 'MouseEvent', 'HashChangeEvent', 'FormData']) {
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
let root, container, requests, sessions, intercept, cookieOwner;

function defaults(path, owner) {
  if (path === '/healthz') return json({control_plane_ready: true});
  if (path === '/v1/version') return json({checkpoint: 'CP4', execution_mode: 'logical_simulation', capabilities: {checkpoint_recovery: true}});
  if (path === '/x/v1/session') return owner ? json(sessions.get(owner) || account(owner)) : json({error:'unauthorized'},401);
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
async function waitForState(predicate, description) {
  const deadline = performance.now() + 1000;
  while (!predicate()) {
    assert.ok(performance.now() < deadline, `Timed out waiting for ${description}`);
    await act(async () => {await new Promise(resolve => setImmediate(resolve));});
  }
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
async function connect(profile = 'admin') {
  const username=profile==='admin'?'alice':profile;
  await enter(container.querySelector('input[name=username]'),username);
  await enter(container.querySelector('input[name=password]'),`synthetic-${username}`);
  await interact(()=>button('登录',container.querySelector('.identity-manual')).click());
}
async function choose(profile) {
  await interact(()=>container.querySelector('.account-trigger').click());
  await interact(()=>button('切换账户').click());
  await connect(profile);
}
function writes() {return requests.filter(request => request.method !== 'GET'&&!request.path.startsWith('/x/v1/auth/'));}
function workbench() {return container.querySelector('[data-space-scroll="workbench"]');}
async function space(name) {await interact(() => button(name, container.querySelector('.space-switch')).click());}

beforeEach(() => {
  document.body.innerHTML = '<div id="account-workspace-tests"></div>';
  container = document.getElementById('account-workspace-tests'); root = createRoot(container);
  requests = []; sessions = new Map([['alice', account('alice')], ['bob', account('bob')],
    ['contributor', account('contributor', ['contribute_resources'])]]); intercept = null; cookieOwner = '';
  globalThis.fetch = async (path, options = {}) => {
    const headers = new Headers(options.headers);
    const request = {path: String(path), owner: headers.get('X-Owner') || cookieOwner,
      method: options.method || 'GET', signal: options.signal};
    requests.push(request);
    if(request.path==='/x/v1/auth/login') {
      const body=JSON.parse(options.body);request.owner=body.username;
      const override=await intercept?.(request);if(override){if(override.ok)cookieOwner=body.username;return override;}
      if(body.password!==`synthetic-${body.username}`||!sessions.has(body.username))return json({error:'unauthorized'},401);
      cookieOwner=body.username;return json(sessions.get(cookieOwner));
    }
    if(request.path==='/x/v1/auth/logout'){cookieOwner='';return json({ok:true});}
    if (request.method !== 'GET') throw new Error('This navigation suite prohibits business writes.');
    return await intercept?.(request) || defaults(request.path, request.owner);
  };
});
afterEach(async () => {
  await act(async () => root.unmount()); globalThis.fetch = originalFetch;
  assert.equal(writes().length, 0, 'no business write reached the synthetic network boundary');
});
after(() => dom.window.close());

test('an existing session grants no workspace before server verification and no administrator capability afterward', async () => {
  const pending = deferred();
  intercept = request => request.path === '/x/v1/session' ? pending.promise : undefined;
  cookieOwner='alice'; await mount();
  assert.match(container.textContent, /正在验证账户/);
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
    if (request.owner === 'bob' && request.path === '/x/v1/auth/login') return newSession.promise;
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
  assert.match(container.textContent, /账户暂时无法验证.*暂停提交/s);
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
  assert.match(container.textContent, /登录织算/);
});

for (const entry of ['space button', 'sidebar link']) {
  test(`a contribution-only account enters its devices through the ${entry}`, async () => {
    await mount(); await connect('contributor');
    if (entry === 'space button') await space('工作台');
    else await interact(() => container.querySelector('nav[aria-label="工作台导航"] a[href="#resources"]').click());
    // Native anchor activation and hashchange are queued separately by JSDOM.
    // Observe the rendered route and its data instead of assuming 5 ms settled it.
    await waitForState(() => location.hash === '#resources' && workbench()?.hidden === false
      && /contributor-node/.test(workbench()?.textContent || ''), 'the contributor devices workspace');
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


test('password login and account menu keep preferences out of the header, and logout clears the session',async()=>{
  await mount();
  assert.equal(container.querySelector('.identity-manual').closest('[hidden]'),null);
  assert.equal(container.querySelector('input[type=file]'),null);
  assert.equal(container.querySelector('input[name=password]').autocomplete,'current-password');
  await connect();
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
  await interact(()=>button('退出登录',menu).click());
  assert.equal(container.querySelector('.space-body'),null);
  assert.equal(cookieOwner,'');
  assert.equal(container.querySelector('input[name=password]').value,'');
  assert.equal(dom.window.localStorage.length,0);assert.equal(dom.window.sessionStorage.length,0);
});

test('remount restores the cookie session without another login and uses the public username',async()=>{
  sessions.set('alice',{...account('legacy-owner',['use_compute','contribute_resources']),username:'chen'});
  await mount();await connect();
  const loginCount=requests.filter(r=>r.path==='/x/v1/auth/login').length;
  await act(async()=>root.unmount());root=createRoot(container);await mount();
  assert.ok(container.querySelector('.space-body'));
  assert.match(container.querySelector('.account-trigger').textContent,/chen/);
  assert.doesNotMatch(container.querySelector('.account-trigger').textContent,/legacy-owner/);
  assert.equal(requests.filter(r=>r.path==='/x/v1/auth/login').length,loginCount);
});

test('wrong passwords show a short error and preserve username without exposing a workspace',async()=>{
  await mount();
  await enter(container.querySelector('input[name=username]'),'alice');
  await enter(container.querySelector('input[name=password]'),'wrong');
  await interact(()=>button('登录',container.querySelector('.identity-manual')).click());
  assert.match(container.textContent,/账户或密码不正确/);
  assert.equal(container.querySelector('.space-body'),null);
  assert.equal(container.querySelector('input[name=username]').value,'alice');
  assert.equal(container.querySelector('input[name=password]').value,'');
});

test('a focus refresh switches to the current cookie identity and drops previous drafts',async()=>{
  await mount('#run?new=1');await connect();
  await enter(workbench().querySelector('input[name=cpu]'),'2048');
  cookieOwner='bob';
  await interact(()=>window.dispatchEvent(new dom.window.Event('focus')));
  assert.match(workbench().textContent,/bob-private-work/);
  assert.doesNotMatch(workbench().textContent,/alice-private-work/);
  assert.equal(workbench().querySelector('input[name=cpu]').value,'1000');
});
