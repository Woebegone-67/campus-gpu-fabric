/** Real list-envelope shapes; all requests are synthetic GETs. */
import assert from 'node:assert/strict';
import {after,afterEach,beforeEach,test} from 'node:test';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const name of ['window','document','navigator','HTMLElement','Event','MouseEvent'])Object.defineProperty(globalThis,name,{value:dom.window[name],configurable:true,writable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {act}=React;
const {createRoot}=await import('react-dom/client');
const {ApiClient}=await import('../src/api.ts');
const {AccountContext}=await import('../src/session.ts');
const {OverviewPage}=await import('../src/pages/Overview.tsx');
const originalFetch=globalThis.fetch;
const base={version:{epoch:0,revision:5},now:10};
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const account=(owner='alice',capabilities=['use_compute'])=>({actor_id:owner,owner_id:owner,capabilities,is_administrator:false});
const client=(owner='alice')=>new ApiClient({owner,token:`synthetic-overview-${owner}`});
const work=(id,state='running',allocation_ids=[],extra={})=>({intent_id:id,owner_id:'alice',state,allocation_ids,first_arrival_at:0,execution_mode:'logical_simulation',settlement_action:'keep_lease',specification:{job:{image:'example:1'}},...extra});
const lease=(id,state='Running',extra={})=>({allocation_id:id,owner_id:'alice',state,created_at:'2026-11-15T00:00:00Z',created_at_logical:0,expires_at:'2026-11-15T00:05:00Z',expires_at_logical:300,...extra});
const resume=id=>({operation:'allocation.resume',entity:{kind:'allocation',entity_id:id}});
let container,root,requests,workResponse,leaseResponse,contributionResponse,api;
async function render(owner='alice',capabilities=['use_compute']){await act(async()=>root.render(<AccountContext.Provider value={account(owner,capabilities)}><OverviewPage api={api}/></AccountContext.Provider>));}
async function interact(fn){await act(async()=>fn());}
const panel=()=>container.querySelector('.overview-attention');
const alerts=()=>[...container.querySelectorAll('.attention-row')];
const alertIds=()=>alerts().map(row=>new URL(row.querySelector('a').href).hash.match(/intent=([^&]+)/)[1]);
function button(label,scope=panel()){const node=[...scope.querySelectorAll('button')].find(node=>node.textContent===label);assert.ok(node,`button ${label}`);return node;}
async function refresh(){await interact(()=>button('刷新',container).click());}
beforeEach(()=>{
  document.body.innerHTML='<div id="overview-tests"></div>';container=document.getElementById('overview-tests');root=createRoot(container);requests=[];api=client();
  workResponse={...base,items:[],legal_actions:[]};leaseResponse={...base,items:[],legal_actions:[]};contributionResponse={...base,items:[]};
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(path.startsWith('/x/v1/work-intents')?workResponse:path.startsWith('/v1/allocations')?leaseResponse:contributionResponse);};
});
afterEach(async()=>{await act(async()=>root.unmount());globalThis.fetch=originalFetch;});
after(()=>dom.window.close());

test('real WorkIntent allocation_ids join separate authorized leases for all three resource reminders',async()=>{
  workResponse.items=[work('work-retained','completed',['a-retained']),work('work-notice','running',['a-notice']),work('work-stop','cancelled',['a-stop']),work('work-released','completed',['a-released'])];
  leaseResponse.items=[lease('a-retained'),lease('a-notice','PreemptRequested',{preemption:{requested_at:2,deadline:12,checkpoint_due:12}}),lease('a-stop','PreemptRequested',{pending_terminal:'Released'}),lease('a-released','Released')];
  await render();
  assert.deepEqual(alertIds(),['work-stop','work-notice','work-retained']);
  assert.match(panel().textContent,/资源停止仍待确认/);assert.match(panel().textContent,/收到资源收回通知/);assert.match(panel().textContent,/工作已结束，资源尚未归还/);
  assert.match(container.querySelector('.overview-scope').textContent,/最近 25 项工作/);
  assert.equal(container.querySelectorAll('.overview-work-row').length,0,'attention items are not duplicated in ongoing work');
  assert.equal(container.querySelectorAll('.overview-result').length,1,'held-resource results appear only under attention');
  assert.deepEqual(requests.map(request=>request.path),['/x/v1/work-intents?limit=25','/v1/allocations?limit=50']);
  assert.ok(requests.every(({options})=>options.method==='GET'&&options.headers['X-Owner']==='alice'&&options.headers.Authorization==='Bearer synthetic-overview-alice'));
});

test('only exact related authorized resume hints prompt interrupted work; Pending never becomes a warning',async()=>{
  workResponse.items=[work('work-wait','waiting',['a-wait']),work('work-resume','interrupted',['a-resume']),work('work-no-resume','interrupted',['a-other']),work('work-manual','manual_action'),work('work-foreign','manual_action',[],{owner_id:'bob'})];
  leaseResponse.items=[lease('a-wait','Pending'),lease('a-resume','Preempted'),lease('a-other','Preempted'),lease('a-unrelated','PreemptRequested')];
  leaseResponse.legal_actions=[resume('a-resume'),{operation:'allocation.resume',entity:{kind:'job',entity_id:'a-other'}}];
  await render();
  assert.deepEqual(alertIds(),['work-manual','work-resume']);
  assert.match(panel().textContent,/工作已中断，可进入详情恢复/);assert.doesNotMatch(panel().textContent,/work-wait|work-foreign|work-no-resume/);
});

test('reminders order by severity then real logical deadline, occurrence and stable ID',async()=>{
  workResponse.items=['late','same-b','same-a','old','urgent'].map(id=>work(`work-${id}`,'running',[`a-${id}`]));
  leaseResponse.items=[
    lease('a-late','PreemptRequested',{preemption:{deadline:40,requested_at:0}}),
    lease('a-same-b','PreemptRequested',{preemption:{deadline:20,requested_at:5}}),
    lease('a-same-a','PreemptRequested',{preemption:{deadline:20,requested_at:5}}),
    lease('a-old','PreemptRequested',{preemption:{deadline:20,requested_at:2}}),
    lease('a-urgent','PreemptRequested',{pending_terminal:'Released',preemption:{deadline:99,requested_at:10}}),
  ];
  await render();assert.deepEqual(alertIds(),['work-urgent','work-old','work-same-a']);assert.match(panel().textContent,/查看全部 5 项/);
  await interact(()=>alerts()[0].querySelector('.attention-dismiss').click());
  assert.deepEqual(alertIds(),['work-old','work-same-a','work-same-b']);
  assert.match(alerts()[0].textContent,/截止 20 逻辑秒/);
});

test('dismissed reminders stay explicit, can be restored, and a changed deadline or reason reappears',async()=>{
  workResponse.items=[work('work-one','running',['a-one'])];
  leaseResponse.items=[lease('a-one','PreemptRequested',{preemption:{deadline:20,requested_at:2}})];
  await render();await interact(()=>button('本次收起').click());
  assert.equal(alerts().length,0);assert.match(panel().querySelector('h2').textContent,/已收起 1 项提示/);assert.doesNotMatch(panel().textContent,/暂无待处理/);
  await interact(()=>button('展开已收起提示').click());assert.equal(alerts().length,1);
  await interact(()=>button('本次收起').click());
  leaseResponse.items[0].preemption.deadline=21;await refresh();assert.equal(alerts().length,1);assert.match(alerts()[0].textContent,/截止 21 逻辑秒/);
  await interact(()=>button('本次收起').click());leaseResponse.items[0].pending_terminal='Expired';await refresh();assert.match(alerts()[0].textContent,/资源停止仍待确认/);
});

test('unread or truncated lease pages cannot be presented as no reminders and embedded fixture-only fields are ignored',async()=>{
  workResponse.items=[work('work-one','completed',['a-one'],{allocations:[lease('fake','Running')]})];
  let resolveLeases;
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return path.startsWith('/v1/allocations')?new Promise(resolve=>{resolveLeases=resolve;}):json(workResponse);};
  await render();assert.equal(alerts().length,0);assert.match(panel().querySelector('h2').textContent,/状态待更新/);assert.doesNotMatch(panel().textContent,/暂无待处理/);
  await act(async()=>resolveLeases(json({...base,items:[lease('unrelated','Released')],has_more:true})));
  assert.match(panel().textContent,/部分资源状态未载入/);assert.equal(alerts().length,0);assert.doesNotMatch(panel().textContent,/暂无待处理/);
});

test('different epochs never join; same-epoch different revisions show provisional independently read reminders',async()=>{
  workResponse.items=[work('work-one','completed',['a-one'])];leaseResponse.items=[lease('a-one')];leaseResponse.version={epoch:1,revision:5};
  await render();assert.equal(alerts().length,0);assert.match(panel().textContent,/实例已更新/);assert.doesNotMatch(panel().textContent,/暂无待处理/);
  leaseResponse.version={epoch:0,revision:6};await refresh();assert.equal(alerts().length,1);assert.match(panel().textContent,/状态正在同步/);
  workResponse.items[0].state='running';await refresh();assert.equal(alerts().length,0);assert.match(panel().querySelector('h2').textContent,/状态待更新/);
  assert.equal(requests.length,6,'refresh reads each endpoint once, without consistency retry loops');
});

test('failed lease refresh retains prior reminders as stale while authorization failure clears old lease data',async()=>{
  workResponse.items=[work('work-one','completed',['a-one'])];leaseResponse.items=[lease('a-one')];await render();
  let status=503;
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return path.startsWith('/v1/allocations')?json({detail:'synthetic temporary error'},status):json(workResponse);};
  await refresh();assert.equal(alerts().length,1);assert.match(panel().textContent,/未能更新/);assert.match(panel().textContent,/部分状态未更新/);
  status=403;await refresh();assert.equal(alerts().length,0);assert.match(panel().textContent,/当前账户不能执行此操作/);assert.doesNotMatch(panel().textContent,/暂无待处理/);
});

test('identity changes abort late reads and never reuse previous leases or dismissal state',async()=>{
  let resolveOld,oldSignal;
  globalThis.fetch=async(path,options)=>{
    requests.push({path,options});const owner=options.headers['X-Owner'];
    if(path.startsWith('/v1/allocations')&&owner==='alice'){oldSignal=options.signal;return new Promise(resolve=>{resolveOld=resolve;});}
    return json({...base,items:path.startsWith('/x/v1/work-intents')?[work(`work-${owner}`,'completed',[`a-${owner}`],{owner_id:owner})]:[lease(`a-${owner}`,'Running',{owner_id:owner})]});
  };
  await render();api=client('bob');await render('bob');assert.equal(oldSignal.aborted,true);assert.deepEqual(alertIds(),['work-bob']);
  await act(async()=>resolveOld(json({...base,items:[lease('a-alice')]})));
  assert.deepEqual(alertIds(),['work-bob']);assert.doesNotMatch(container.textContent,/work-alice/);
  await interact(()=>button('本次收起').click());
  api=new ApiClient({owner:'bob',token:'synthetic-new-session'});await render('bob');assert.deepEqual(alertIds(),['work-bob']);
});

test('no compute capability means no work or lease reads, and own device cards show source availability and states',async()=>{
  contributionResponse.items=[{node_id:'own-node',contributor_owner_id:'alice',state:'Ready',available:{gpu_count:1},capacity:{gpu_count:2},devices:[{device_id:'own-gpu',model:'A10',state:'occupied'},{device_id:'own-slice',model:'A10',state:'available',profile:'slice'}]},{node_id:'foreign-node',contributor_owner_id:'bob'}];
  await render('alice',['contribute_resources']);assert.deepEqual(requests.map(request=>request.path),['/x/v1/contributions?limit=50']);
  assert.equal(panel(),null);assert.match(container.textContent,/可用 GPU 1 \/ 2/);assert.match(container.textContent,/own-gpu.*占用/s);assert.doesNotMatch(container.textContent,/foreign-node|own-slice/);
});

test('results use actual recorded_at descending within the loaded page, with missing times last',async()=>{
  workResponse.items=[work('work-no-time','completed'),work('work-old','completed',[],{result:{recorded_at:1}}),work('work-new','completed',[],{result:{recorded_at:30}}),work('work-middle','failed',[],{result:{recorded_at:20}})];
  await render();assert.deepEqual([...container.querySelectorAll('.overview-result')].map(row=>new URL(row.href).hash.match(/intent=([^&]+)/)[1]),['work-new','work-middle','work-old']);
  assert.match(container.querySelector('.overview-scope').textContent,/最近 25 项工作/);assert.equal(panel(),null);assert.match(container.textContent,/暂无待处理事项/);
});


test('empty personal overview has one create entry and no empty device or result cards',async()=>{
  await render('alice',['use_compute','contribute_resources']);
  assert.equal(container.querySelectorAll('a[href="#run?new=1"]').length,1);
  assert.equal(container.querySelector('.overview-devices'),null);
  assert.equal(container.querySelector('.overview-results'),null);
  assert.equal(container.querySelector('.overview-work'),null);
  assert.match(container.textContent,/还没有工作/);
});

test('administrator home reads authorized global counts without inventing private work',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,counts:{work_intents:14},running_jobs:3,work_by_state:{waiting:2,manual_action:1},capacity:{totals:{available:{gpu_count:4}}}});};
  await act(async()=>root.render(<AccountContext.Provider value={{actor_id:'admin',owner_id:null,capabilities:[],is_administrator:true}}><OverviewPage api={api}/></AccountContext.Provider>));
  assert.deepEqual(requests.map(request=>request.path),['/x/v1/admin/overview']);
  assert.deepEqual([...container.querySelectorAll('.overview-metrics dd')].map(item=>item.textContent),['3','2','4','14']);
  assert.match(container.textContent,/1 项工作需要处理/);
  assert.equal(container.querySelector('.overview-work'),null);
});
