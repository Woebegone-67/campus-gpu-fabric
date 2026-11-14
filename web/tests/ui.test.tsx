/** Component interaction regressions with an isolated DOM and synthetic API fixtures.
 * These tests never access a running Fabric instance or count as human acceptance.
 */
import assert from 'node:assert/strict';
import {after,afterEach,beforeEach,test} from 'node:test';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const name of ['window','document','navigator','HTMLElement','HTMLInputElement','Event','MouseEvent','FormData']) {
  Object.defineProperty(globalThis,name,{value:dom.window[name],configurable:true,writable:true});
}
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {act,useState}=React;
const {createRoot}=await import('react-dom/client');
const {ApiClient}=await import('../src/api.ts');
const {IdentityPicker}=await import('../src/components/IdentityPicker.tsx');
const {ContinuityPage}=await import('../src/pages/Continuity.tsx');
const {DecisionsPage}=await import('../src/pages/Decisions.tsx');
const {EventsPage}=await import('../src/pages/Events.tsx');
const {default:App}=await import('../src/App.tsx');
let container,root,requests;
const originalFetch=globalThis.fetch;
const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
const base={version:{epoch:0,revision:1},now:0};
const work=(id,state='running')=>({...base,item:{intent_id:id,state,execution_mode:'logical_simulation',settlement_action:'keep_lease',allocation_ids:['lease-1'],stages:[],attempts:[]},allocations:[{allocation_id:'lease-1',state:'Allocated'}],legal_actions:[],events:[{event_id:'event-1',event_type:'allocation.granted',decision_id:'decision-1',logical_time:0,sequence:1}]});
const decision=(id)=>({...base,item:{decision_id:id,rationale:`说明 ${id}`,policy:{name:'B0',version:'1'},coverage:{scope:'partial'},candidates:[]},actual_events:[{event_type:'decision.committed'}],commit_receipts:[]});
function api(){return new ApiClient({owner:'bob',token:'synthetic-test-token'});}
async function render(element){await act(async()=>{root.render(element);});}
async function interact(action){await act(async()=>{action();});}
function button(label){const found=[...container.querySelectorAll('button')].find(e=>e.textContent===label);assert.ok(found,`button ${label} exists`);return found;}
function field(label){const found=[...container.querySelectorAll('label')].find(e=>e.querySelector('span')?.textContent===label);assert.ok(found,`field ${label} exists`);return found.querySelector('input,select');}
async function enter(input,value){await interact(()=>{Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(input,value);input.dispatchEvent(new dom.window.Event('input',{bubbles:true}));});}
async function submit(form){await interact(()=>form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})));}
async function uploadProfiles(){const input=container.querySelector('input[type=file]');const data=JSON.stringify({admin:{Authorization:'Bearer synthetic-admin'},bob:{Authorization:'Bearer synthetic-bob','X-Owner':'bob'}});Object.defineProperty(input,'files',{value:[{size:data.length,text:async()=>data}],configurable:true});await interact(()=>input.dispatchEvent(new dom.window.Event('change',{bubbles:true})));}
beforeEach(()=>{
  document.body.innerHTML='<div id="test-root"></div>';
  container=document.getElementById('test-root');root=createRoot(container);requests=[];
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,items:[]});};
});
afterEach(async()=>{await act(async()=>root.unmount());globalThis.fetch=originalFetch;});
after(()=>dom.window.close());

test('imported admin remains selected after Apply, changed manual credentials lose its hint',async()=>{
  let applied;
  function Harness(){const [identity,setIdentity]=useState({owner:'',token:''});applied=identity;return <><IdentityPicker identity={identity} busy={false} onApply={setIdentity}/>{identity.profile==='admin'&&identity.token&&<span data-testid="clock">模拟时钟入口</span>}</>;}
  await render(<Harness/>);await uploadProfiles();
  assert.match(container.textContent,/已读取 本地身份文件，共 2 个身份/);
  assert.equal(applied.token,'','import does not silently change the active identity');
  await interact(()=>{const select=container.querySelector('select');select.value='admin';select.dispatchEvent(new dom.window.Event('change',{bubbles:true}));});
  assert.ok(container.querySelector('[data-testid=clock]'));
  await submit(container.querySelector('form'));
  assert.equal(applied.profile,'admin');assert.ok(container.querySelector('[data-testid=clock]'));
  assert.match(container.textContent,/已应用身份 admin/);
  await enter(field('Bearer token（启用认证时）'),'different-synthetic-token');
  await submit(container.querySelector('form'));
  assert.equal(applied.profile,undefined);assert.equal(container.querySelector('[data-testid=clock]'),null);
  assert.equal(requests.length,0,'identity file and tokens are not uploaded');
});

test('same work ID submit re-reads once, updates state, and Refresh is not a form submit',async()=>{
  let reads=0;
  globalThis.fetch=async(path)=>{requests.push({path});return json(work('work-1',++reads===1?'running':'completed'));};
  await render(<ContinuityPage api={api()} initialId="work-1"/>);
  assert.equal(reads,1);assert.match(container.textContent,/运行中/);
  await submit(container.querySelector('form'));
  assert.equal(reads,2);assert.match(container.textContent,/已完成/);assert.match(container.textContent,/已读取服务端状态/);
  const refresh=button('刷新');assert.equal(refresh.type,'button');
  await interact(()=>refresh.click());assert.equal(reads,3);
});

test('work lookup shows loading and failure without retaining a previous result',async()=>{
  let release;
  globalThis.fetch=async()=>new Promise(resolve=>{release=resolve;});
  await render(<ContinuityPage api={api()} initialId="missing-work"/>);
  assert.ok(button('正在查询…').disabled);assert.match(container.textContent,/正在读取服务端状态/);
  await act(async()=>release(json({error:'not_found',detail:'Work is not visible'},404)));
  assert.match(container.textContent,/HTTP 404/);assert.doesNotMatch(container.textContent,/租约与资源占用/);
});

test('same decision ID submit re-reads and direct lookup includes return-to-work link',async()=>{
  globalThis.fetch=async(path)=>{requests.push({path});return json(decision('decision-1'));};
  await render(<DecisionsPage api={api()} initialId="decision-1" workId="work-1"/>);
  assert.equal(requests.length,1);
  await submit(container.querySelector('form'));assert.equal(requests.length,2);
  assert.equal(container.querySelector('a').getAttribute('href'),'#continuity?intent=work-1');
});

test('work details link to their decisions and events and disclose held lease after completion',async()=>{
  globalThis.fetch=async()=>json(work('work-1','completed'));
  await render(<ContinuityPage api={api()} initialId="work-1"/>);
  const links=[...container.querySelectorAll('a')];
  assert.equal(links.find(a=>a.textContent==='查看关联决策').getAttribute('href'),'#decisions?decision=decision-1&intent=work-1');
  assert.equal(links.find(a=>a.textContent==='查看关联事件').getAttribute('href'),'#events?intent=work-1');
  assert.match(container.querySelector('[aria-label="租约与资源占用"]').textContent,/已获资源.*任务完成或取消后仍会占用资源/s);
});

test('work event view uses authorized work events, exposes decision links and top export',async()=>{
  globalThis.fetch=async(path)=>{requests.push({path});return json(work('work-1'));};
  await render(<EventsPage api={api()} intentId="work-1"/>);
  assert.deepEqual(requests.map(r=>r.path),['/x/v1/work-intents/work-1']);
  const exportButton=button('导出这项工作的事件');
  assert.ok(exportButton.closest('.panel-heading'));
  assert.equal(container.querySelector('a[href^="#decisions"]').getAttribute('href'),'#decisions?decision=decision-1&intent=work-1');
  assert.match(container.textContent,/筛选只改变页面显示，不改变导出范围/);
});

test('same experiment ID submit fetches fresh results',async()=>{
  globalThis.fetch=async(path)=>{requests.push({path});return json(path.startsWith('/v1/events')?{...base,items:[]}:{...base,item:{run_id:'run-1',status:'NOT_RUN',checks:[]}});};
  await render(<EventsPage api={api()}/>);
  await enter(field('Run ID'),'run-1');await submit(container.querySelector('form'));
  await submit(container.querySelector('form'));
  assert.equal(requests.filter(r=>r.path==='/x/v1/experiments/run-1').length,2);
});

test('hash navigation opens a decision and changing its parameter loads the other decision',async()=>{
  dom.window.history.replaceState(null,'','/?visual=classic#decisions?decision=decision-one');
  globalThis.location=dom.window.location;
  const frames={request:globalThis.requestAnimationFrame,cancel:globalThis.cancelAnimationFrame};
  globalThis.requestAnimationFrame=callback=>setTimeout(()=>callback(0),0);
  globalThis.cancelAnimationFrame=id=>clearTimeout(id);
  globalThis.fetch=async(path)=>{
    requests.push({path});
    if(path==='/healthz')return json({control_plane_ready:true});
    if(path==='/v1/version')return json({checkpoint:'CP4',execution_mode:'logical_simulation',capabilities:{}});
    if(path==='/x/v1/session')return json({actor_id:'synthetic-bob',owner_id:'bob',capabilities:['use_compute'],is_administrator:false});
    return json(decision(path.split('/').at(-1)));
  };
  try{
    await render(<App/>);
    await interact(()=>button('导入或切换账户').click());
    await uploadProfiles();
    await interact(()=>{const select=container.querySelector('.identity-panel select');select.value='bob';select.dispatchEvent(new dom.window.Event('change',{bubbles:true}));});
    assert.equal(field('决策 ID').value,'decision-one');
    assert.equal(requests.filter(r=>r.path==='/x/v1/decisions/decision-one').length,1);
    await interact(()=>{dom.window.history.replaceState(null,'','#decisions?decision=decision-two');dom.window.dispatchEvent(new dom.window.HashChangeEvent('hashchange'));});
    assert.equal(field('决策 ID').value,'decision-two');
    assert.equal(requests.filter(r=>r.path==='/x/v1/decisions/decision-two').length,1);
    assert.doesNotMatch(container.textContent,/说明 decision-one/);
  }finally{
    await render(null);
    if(frames.request)globalThis.requestAnimationFrame=frames.request;else delete globalThis.requestAnimationFrame;
    if(frames.cancel)globalThis.cancelAnimationFrame=frames.cancel;else delete globalThis.cancelAnimationFrame;
  }
});

test('owner-scoped decision explains limited visibility without claiming global rejection',async()=>{
  globalThis.fetch=async()=>json({...base,scope:'owner',item:{decision_id:'decision-own',selected_candidate_id:null,policy:{name:'B0'},coverage:{scope:'partial',examined_count:1},candidates:[{candidate_id:'own-candidate',action:'allocate',feasibility:'legal',scores:[],violations:[]}]},actual_events:[],commit_receipts:[]});
  await render(<DecisionsPage api={api()} initialId="decision-own"/>);
  assert.match(container.textContent,/仅展示你的候选/);
  assert.match(container.textContent,/当前授权视图未显示你的需求被选中，这不代表你的需求不可行/);
  assert.match(container.textContent,/比较分值未公开/);
  assert.doesNotMatch(container.textContent,/声明覆盖完整候选域/);
});

test('temporary 503 lookup failure is not labeled as an unimplemented capability',async()=>{
  globalThis.fetch=async()=>json({error:'temporarily_unavailable',detail:'Please retry later'},503);
  await render(<DecisionsPage api={api()} initialId="decision-1"/>);
  assert.match(container.textContent,/服务暂时不可用/);
  assert.match(container.textContent,/HTTP 503/);
  assert.doesNotMatch(container.textContent,/能力等待后端接入/);
});

const {ServiceContext}=await import('../src/service.tsx');
const {CreateWork}=await import('../src/pages/Run.tsx');
const {RecallForm}=await import('../src/pages/Resources.tsx');
const {AcceptancePage}=await import('../src/pages/Acceptance.tsx');
const {ArtifactDownload}=await import('../src/components/CP2.tsx');
const {Experiments}=await import('../src/components/Experiments.tsx');
const {ActionButton}=await import('../src/components/common.tsx');
function service(mode,children){return <ServiceContext.Provider value={{mode,checkpoint:'CP2',capabilities:{checkpoint_recovery:true,real_gpu_execution:false}}}>{children}</ServiceContext.Provider>;}
function cp2Work(mode='logical_simulation'){
  return {...work('work-cp2'),item:{...work('work-cp2').item,execution_mode:mode,specification:{allocation:{max_restarts:2},job:{max_retries:0}},attempts:[{ref:{attempt_id:'attempt-2',generation:2,job_id:'job-1'},state:'running',checkpoint:null}]},allocations:[{allocation_id:'lease-1',state:'PreemptRequested',restart_count:1,preemption:{requested_at:2,deadline:12,checkpoint_due:7},checkpoint:{report_status:'ready',reported_at:3,verification_status:'unverified',continuation_status:'not_attempted'}}],jobs:[{job_id:'job-1',state:'Running',retry_count:0,attempt:{generation:2,attempt_id:'attempt-2'}}],legal_actions:[{operation:'allocation.resume',entity:{entity_id:'lease-1'}},{operation:'allocation.checkpoint',entity:{entity_id:'lease-1'}}]};
}

test('CPU registration follows declared service mode and sends separate recovery/retry budgets',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({receipt:{outcome:'committed'}},202);};
  await render(service('cpu_reference',<CreateWork api={api()} onCreated={()=>{}} onOpen={()=>{}}/>));
  assert.match(container.textContent,/固定 CPU 参考程序，不执行填写的镜像与命令/);
  await submit(container.querySelector('form'));
  assert.equal(requests.length,1);
  const payload=JSON.parse(requests[0].options.body);
  assert.equal(payload.execution_mode,'cpu_reference');
  assert.equal(payload.specification.allocation.max_restarts,2);
  assert.equal(payload.specification.job.max_retries,0);
  assert.equal(payload.specification.allocation.checkpoint_timeout_seconds,15);
  assert.equal(payload.owner_id,'bob');
  assert.ok(button('重试同一工作登记').disabled,'accepted registration cannot be duplicated by button');
});

test('unknown execution mode blocks registration and does not guess logical mode',async()=>{
  await render(<CreateWork api={api()} onCreated={()=>{}} onOpen={()=>{}}/>);
  assert.ok(button('提交工作').disabled);
  await submit(container.querySelector('form'));
  assert.equal(requests.length,0);assert.match(container.textContent,/尚未确认服务执行模式/);
});

test('contributor recalls owned physical GPUs without contribution ID or test route',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({outcome:'committed'},201);};
  await render(<RecallForm api={api()} nodes={[{node_id:'own-node',contributor_owner_id:'bob',devices:[{device_id:'gpu-own',physical_gpu_id:'gpu-own',model:'A10'},{device_id:'gpu-own-slice',physical_gpu_id:'gpu-own',model:'A10'}]},{node_id:'other-node',contributor_owner_id:'alice',devices:[{device_id:'gpu-private'}]}]} onDone={()=>{}}/>);
  const boxes=container.querySelectorAll('input[type=checkbox]');assert.equal(boxes.length,1);assert.doesNotMatch(container.textContent,/gpu-private/);
  await interact(()=>boxes[0].click());await submit(container.querySelector('form'));
  assert.equal(requests[0].path,'/x/v1/contributions/recall');
  assert.deepEqual(JSON.parse(requests[0].options.body),{gpu_ids:['gpu-own'],notice_seconds:30,duration_seconds:60});
  assert.equal(requests[0].options.headers['X-Test-Key'],undefined);
});

test('checkpoint facts stay separate; logical report writes only status and refreshes',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(options.method==='POST'?{allocation_id:'lease-1'}:cp2Work());};
  await render(service('logical_simulation',<ContinuityPage api={api()} initialId="work-cp2"/>));
  const facts=container.querySelector('.checkpoint-grid');assert.match(facts.textContent,/收到 ready 报告.*未核验.*未接续/s);
  assert.equal([...container.querySelectorAll('button')].some(b=>b.textContent==='下载已核验保存点'),false);
  await interact(()=>button('报告保存 ready').click());
  const post=requests.find(r=>r.options.method==='POST');
  assert.equal(post.path,'/v1/allocations/lease-1/checkpoint');assert.deepEqual(JSON.parse(post.options.body),{status:'ready'});
  assert.equal(requests.filter(r=>r.path==='/x/v1/work-intents/work-cp2').length,2);
});

test('CPU continuity cannot manually mark checkpoint verified and keeps stopping claims visible',async()=>{
  const data=cp2Work('cpu_reference');data.allocations[0].pending_terminal='Released';data.item.attempts[0].state='stopping';
  globalThis.fetch=async()=>json(data);
  await render(service('cpu_reference',<ContinuityPage api={api()} initialId="work-cp2"/>));
  assert.match(container.textContent,/停止确认前，资源仍由此租约持有/);
  assert.match(container.textContent,/新代次已登记不代表保存点已应用/);
  assert.doesNotMatch(container.textContent,/这项工作当前没有持有资源/);
  assert.equal([...container.querySelectorAll('button')].some(b=>b.textContent==='报告保存 ready'),false);
  assert.match(container.textContent,/收回恢复预算1 \/ 2/);assert.match(container.textContent,/任务失败重试预算0 \/ 0/);
});

test('resume action uses current work ID and version without creating another job or stage',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(options.method==='POST'?{receipt:{outcome:'committed'}}:cp2Work('cpu_reference'));};
  await render(service('cpu_reference',<ContinuityPage api={api()} initialId="work-cp2"/>));
  await interact(()=>button('恢复这项工作').click());
  const writes=requests.filter(r=>r.options.method==='POST');assert.equal(writes.length,1);
  assert.equal(writes[0].path,'/x/v1/work-intents/work-cp2/actions');
  assert.deepEqual(JSON.parse(writes[0].options.body),{action:'resume',expected_version:base.version});
});

test('artifact download uses authenticated registered URI endpoint, rejecting arbitrary file links',async()=>{
  const urls=[],blobs=[];const originalCreate=URL.createObjectURL,originalRevoke=URL.revokeObjectURL,originalClick=dom.window.HTMLAnchorElement.prototype.click;
  URL.createObjectURL=(blob)=>{blobs.push(blob);urls.push('blob-created');return 'blob:isolated-test';};URL.revokeObjectURL=()=>{};dom.window.HTMLAnchorElement.prototype.click=function(){urls.push(this.download);};
  try{
    const raw='{"state":{"step":1000000,"value":16159431991146531633}}\n';
    globalThis.fetch=async(path,options)=>{requests.push({path,options});return new Response(raw,{headers:{'Content-Type':'application/json'}});};
    await render(<><ArtifactDownload api={api()} uri="fabric-cpu://epoch-0/work/result.json"/><ArtifactDownload api={api()} uri="file:///private/arbitrary"/></>);
    assert.equal(container.querySelectorAll('button').length,1);
    await interact(()=>button('下载真实工件').click());
    assert.equal(requests[0].path,'/x/v1/artifacts?uri=fabric-cpu%3A%2F%2Fepoch-0%2Fwork%2Fresult.json');
    assert.equal(requests[0].options.headers['X-Owner'],'bob');assert.equal(urls.length,2);assert.equal(await blobs[0].text(),raw);
  }finally{URL.createObjectURL=originalCreate;URL.revokeObjectURL=originalRevoke;dom.window.HTMLAnchorElement.prototype.click=originalClick;}
});

for(const change of ['identity','uri','navigation'])test(`pending CPU artifact download cannot survive ${change}`,async()=>{
  let resolveDownload,signal;
  const downloads=[];
  const originalCreate=URL.createObjectURL,originalRevoke=URL.revokeObjectURL,originalClick=dom.window.HTMLAnchorElement.prototype.click;
  URL.createObjectURL=blob=>{downloads.push(blob);return 'blob:private-artifact';};
  URL.revokeObjectURL=()=>{};dom.window.HTMLAnchorElement.prototype.click=function(){};
  try{
    globalThis.fetch=async(_path,options)=>{signal=options.signal;return new Promise(resolve=>{resolveDownload=resolve;});};
    const ownerApi=api();
    await render(<ArtifactDownload api={ownerApi} uri="fabric-cpu://private/result.json"/>);
    await interact(()=>button('下载真实工件').click());
    if(change==='identity')await render(<ArtifactDownload api={new ApiClient({owner:'alice',token:'other-token'})} uri="fabric-cpu://private/result.json"/>);
    else if(change==='uri')await render(<ArtifactDownload api={ownerApi} uri="fabric-cpu://other/result.json"/>);
    else await render(<p>Another workspace</p>);
    // Ignore cancellation in this transport to exercise the late-response guard.
    await act(async()=>resolveDownload(json({private_result:18446744073709551615n.toString()})));
    assert.equal(downloads.length,0,'old permissions or an obsolete resource must never create a download');
    assert.equal(signal.aborted,true);
    assert.doesNotMatch(container.textContent,/private_result|工件读取失败|HTTP/);
  }finally{URL.createObjectURL=originalCreate;URL.revokeObjectURL=originalRevoke;dom.window.HTMLAnchorElement.prototype.click=originalClick;}
});

test('late CPU artifact failure cannot replace a newer URI result',async()=>{
  let resolveOld;
  const ownerApi=api();
  globalThis.fetch=async path=>path.includes('old')?new Promise(resolve=>{resolveOld=resolve;}):json({current_result:'visible'});
  await render(<ArtifactDownload api={ownerApi} uri="fabric-cpu://old/result.json" previewLabel="查看结果"/>);
  await interact(()=>button('查看结果').click());
  await render(<ArtifactDownload api={ownerApi} uri="fabric-cpu://new/result.json" previewLabel="查看结果"/>);
  await interact(()=>button('查看结果').click());
  await act(async()=>resolveOld(json({error:'not_found',detail:'old private failure'},404)));
  assert.match(container.textContent,/current_result/);
  assert.doesNotMatch(container.textContent,/old private failure|HTTP 404/);
  assert.equal(button('查看结果').disabled,false);
});

test('unknown lifecycle write result cannot be replayed from the same button',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});throw new TypeError('lost response');};
  await render(<ActionButton api={api()} path="/x/v1/work-intents/work/actions" body={{action:'release'}}>释放</ActionButton>);
  await interact(()=>button('释放').click());assert.ok(button('释放').disabled);
  await interact(()=>button('释放').click());assert.equal(requests.length,1);
  assert.match(container.textContent,/先刷新当前状态/);
});

test('CPU system acceptance hides manual logical clock even with admin profile',async()=>{
  globalThis.fetch=async(path)=>json(path==='/v1/version'?{execution_mode:'cpu_reference',checkpoint:'CP2',capabilities:{real_cpu_execution:true}}:{items:[]});
  await render(<AcceptancePage api={api()} showAdminClock/>);
  assert.doesNotMatch(container.textContent,/推进模拟时间/);assert.match(container.textContent,/cpu_reference/);
});

test('admin experiment uses real catalog input and queries accepted run, preserving pending status',async()=>{
  const admin=new ApiClient({owner:'',token:'synthetic-admin',profile:'admin'});
  globalThis.fetch=async(path,options)=>{
    requests.push({path,options});
    if(path==='/x/v1/experiments/catalog')return json({items:[{scenario_id:'small-development',label:'研发小场景',description:'isolated',policies:['B0','P'],seed_min:0,seed_max:99,work_count:4,horizon:30,max_candidates:12}]});
    if(options.method==='POST')return json({item:{run_id:'run-accepted',status:'running'}},202);
    if(path==='/x/v1/experiments/run-accepted')return json({item:{run_id:'run-accepted',status:'running',outputs:[],checks:[]}});
    return json({items:[]});
  };
  await render(<Experiments api={admin}/>);
  await submit(button('运行隔离实验').closest('form'));
  const post=requests.find(r=>r.options.method==='POST');assert.equal(post.path,'/x/v1/experiments');
  assert.deepEqual(JSON.parse(post.options.body),{scenario_id:'small-development',policy:'B0',seed:0});
  assert.ok(requests.some(r=>r.path==='/x/v1/experiments/run-accepted'));
  assert.match(container.textContent,/运行中/);assert.match(container.textContent,/不构成 CP3 正式效果/);
  assert.equal([...container.querySelectorAll('button')].some(b=>b.textContent==='下载汇总'),false,'pending run does not invent artifacts');
});

test('failed checkpoint report sends failed without adding verification or artifact fields',async()=>{
  const data=cp2Work();data.allocations[0].checkpoint=null;
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(options.method==='POST'?{allocation_id:'lease-1'}:data);};
  await render(service('logical_simulation',<ContinuityPage api={api()} initialId="work-cp2"/>));
  await interact(()=>button('报告保存 failed').click());
  assert.deepEqual(JSON.parse(requests.find(r=>r.options.method==='POST').options.body),{status:'failed'});
});

test('forbidden artifact response stays 404 and never produces a download',async()=>{
  const originalCreate=URL.createObjectURL;let downloads=0;URL.createObjectURL=()=>{downloads++;return 'blob:should-not-exist';};
  try{
    globalThis.fetch=async()=>json({error:'not_found',detail:'Artifact is not visible'},404);
    await render(<ArtifactDownload api={api()} uri="fabric-cpu://other-owner/result.json"/>);
    await interact(()=>button('下载真实工件').click());
    assert.equal(downloads,0);assert.match(container.textContent,/HTTP 404/);
  }finally{URL.createObjectURL=originalCreate;}
});

test('experiment input and output downloads use only registered fixed artifact IDs',async()=>{
  const blobs=[];const originalCreate=URL.createObjectURL,originalRevoke=URL.revokeObjectURL,originalClick=dom.window.HTMLAnchorElement.prototype.click;
  URL.createObjectURL=(blob)=>{blobs.push(blob);return 'blob:experiment-test';};URL.revokeObjectURL=()=>{};dom.window.HTMLAnchorElement.prototype.click=function(){};
  try{
    const raw='{"seed":0,"checksum":16159431991146531633}\n';
    globalThis.fetch=async(path,options)=>{requests.push({path,options});if(path.endsWith('/artifacts/input'))return new Response(raw,{headers:{'Content-Type':'application/json'}});return json({item:{run_id:'run-evidence',status:'completed',inputs:[{name:'input',uri:'experiment://run-evidence/input'}],outputs:[{name:'summary',uri:'experiment://run-evidence/summary'},{name:'../../private',uri:'file:///private'}]}});};
    await render(<Experiments api={api()}/>);await enter(field('Run ID'),'run-evidence');await submit(container.querySelector('form'));
    assert.ok(button('下载固定输入'));assert.ok(button('下载汇总'));assert.equal([...container.querySelectorAll('button')].some(b=>b.textContent?.includes('../../private')),false);
    await interact(()=>button('下载固定输入').click());
    assert.equal(requests.at(-1).path,'/x/v1/experiments/run-evidence/artifacts/input');
    assert.equal(requests.at(-1).options.headers.Authorization,'Bearer synthetic-test-token');
    assert.equal(await blobs[0].text(),raw,'experiment artifact must retain exact HTTP JSON');
  }finally{URL.createObjectURL=originalCreate;URL.revokeObjectURL=originalRevoke;dom.window.HTMLAnchorElement.prototype.click=originalClick;}
});

for(const change of ['identity','run','navigation'])test(`pending experiment artifact download cannot survive ${change}`,async()=>{
  let resolveDownload,signal;
  const downloads=[];
  const originalCreate=URL.createObjectURL,originalRevoke=URL.revokeObjectURL,originalClick=dom.window.HTMLAnchorElement.prototype.click;
  URL.createObjectURL=blob=>{downloads.push(blob);return 'blob:admin-artifact';};
  URL.revokeObjectURL=()=>{};dom.window.HTMLAnchorElement.prototype.click=function(){};
  try{
    globalThis.fetch=async(path,options)=>{
      if(path.includes('/artifacts/')){signal=options.signal;return new Promise(resolve=>{resolveDownload=resolve;});}
      return json({item:{run_id:path.split('/').at(-1),status:'completed',outputs:[{name:'summary'}]}});
    };
    const ownerApi=api();
    await render(<Experiments api={ownerApi}/>);
    await enter(field('Run ID'),'old-run');await submit(container.querySelector('form'));
    await interact(()=>button('下载汇总').click());
    if(change==='identity')await render(<div key="alice"><Experiments api={new ApiClient({owner:'alice',token:'other-token'})}/></div>);
    else if(change==='run'){await enter(field('Run ID'),'new-run');await submit(container.querySelector('form'));}
    else await render(<p>Another workspace</p>);
    await act(async()=>resolveDownload(json({admin_only:'old private evidence'})));
    assert.equal(downloads.length,0,'a late admin artifact must not download after leaving its context');
    assert.equal(signal.aborted,true);
    assert.doesNotMatch(container.textContent,/admin_only|工件读取失败|HTTP/);
  }finally{URL.createObjectURL=originalCreate;URL.revokeObjectURL=originalRevoke;dom.window.HTMLAnchorElement.prototype.click=originalClick;}
});

test('CPU resources hide manual inventory registration while keeping owned GPU recall usable',async()=>{
  const {ResourcesPage}=await import('../src/pages/Resources.tsx');
  const nodes=[{node_id:'own-node',contributor_owner_id:'bob',state:'Ready',devices:[{device_id:'owned-gpu',model:'A10'}]}];
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(path==='/v1/capacity'?{nodes,totals:{capacity:{gpu_count:1},available:{gpu_count:1}}}:{items:nodes});};
  await render(service('cpu_reference',<ResourcesPage api={api()}/>));
  assert.match(container.textContent,/此实例的库存由启动配置载入/);
  assert.match(container.textContent,/请在逻辑模拟实例登记供给/);
  assert.equal(container.querySelector('textarea[name=node]'),null);
  const box=container.querySelector('input[type=checkbox]');assert.ok(box);
  await interact(()=>box.click());
  assert.equal(button('发送收回通知').disabled,false);
  assert.equal(requests.filter(r=>r.options.method!=='GET').length,0);
});

test('CPU form retries stale registration with same identity/body and blocks navigation until response',async()=>{
  let release,opened=0;
  globalThis.fetch=async(path,options)=>{
    requests.push({path,options});
    if(requests.length===1)return json({error:'stale_version',detail:'Refresh snapshot'},409);
    return new Promise(resolve=>{release=resolve;});
  };
  await render(service('cpu_reference',<CreateWork api={api()} onCreated={()=>{}} onOpen={()=>{opened++;}}/>));
  await submit(container.querySelector('form'));
  assert.equal(requests.length,2);assert.equal(requests[0].options.body,requests[1].options.body);
  const payload=JSON.parse(requests[1].options.body);assert.match(payload.intent_id,/^work-/);assert.equal(payload.execution_mode,'cpu_reference');
  assert.ok(button('查询这项工作').disabled);
  await interact(()=>button('查询这项工作').click());assert.equal(opened,0);
  await act(async()=>release(json({receipt:{outcome:'committed'}},202)));
  assert.equal(button('查询这项工作').disabled,false);
  await interact(()=>button('查询这项工作').click());assert.equal(opened,1);
});

test('CP3 settlement puts authoritative failure and held lease guidance before history',async()=>{
  const data=cp2Work();data.item.state='failed';data.item.result={kind:'failure',recorded_at:12,failure:{code:'timeout',detail:'Task exceeded its allowed runtime'}};
  data.allocations[0].state='Running';data.legal_actions=[{operation:'allocation.release'}];
  globalThis.fetch=async()=>json(data);
  await render(service('logical_simulation',<ContinuityPage api={api()} initialId="work-cp2"/>));
  const outcome=container.querySelector('[aria-label="结果与结算"]');
  assert.match(outcome.textContent,/工作失败 · timeout.*Task exceeded/s);
  assert.match(outcome.textContent,/工作已经结束，租约仍持有资源/);
  assert.ok(outcome.compareDocumentPosition([...container.querySelectorAll('h2')].find(e=>e.textContent==='执行与保存时间线'))&dom.window.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.equal(container.querySelector('.simulation-note'),null,'terminal work must not ask the user to advance time');
  assert.equal(button('释放租约').disabled,false);
});

test('CP3 settlement distinguishes completed work from pending stop and preserves receipt after refresh',async()=>{
  const data=cp2Work('cpu_reference');data.item.state='completed';data.item.result={kind:'success',recorded_at:12};data.allocations[0].pending_terminal='Released';
  data.legal_actions=[{operation:'allocation.release'}];
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(options.method==='POST'?{receipt:{outcome:'noop'}}:data);};
  const originalConfirm=window.confirm;window.confirm=()=>true;
  try{
    await render(service('cpu_reference',<ContinuityPage api={api()} initialId="work-cp2"/>));
    assert.match(container.querySelector('[aria-label="结果与结算"]').textContent,/停止请求正在确认中/);
    assert.doesNotMatch(container.textContent,/关联租约已不再持有资源/);
    await interact(()=>button('释放租约').click());
    assert.match(container.textContent,/无新增效果/,'receipt remains available after detail refresh');
    assert.match(container.textContent,/确认前不能视为资源已归还/);
  }finally{window.confirm=originalConfirm;}
});

test('CP3 settlement shows released cancellation and keeps previous attempts collapsed',async()=>{
  const data=cp2Work();data.item.state='cancelled';data.item.result={kind:'cancelled',recorded_at:12};data.allocations[0].state='Released';
  data.item.attempts.unshift({ref:{attempt_id:'attempt-1',generation:1,job_id:'job-1'},state:'stopped'});data.item.attempts[1].state='cancelled';
  globalThis.fetch=async()=>json(data);
  await render(service('logical_simulation',<ContinuityPage api={api()} initialId="work-cp2"/>));
  assert.match(container.querySelector('[aria-label="结果与结算"]').textContent,/已取消.*关联租约已不再持有资源/s);
  const history=[...container.querySelectorAll('details')].find(e=>e.querySelector('summary')?.textContent==='查看历史执行（1 次）');
  assert.ok(history);assert.equal(history.open,false);assert.match(history.textContent,/attempt-1.*已停止/s);
  assert.equal(container.querySelector('.simulation-note'),null);
});

test('CP3 settlement leaves waiting recovery incomplete and exposes failed stage reason',async()=>{
  const data=cp2Work();data.item.state='waiting';data.allocations[0].state='Pending';data.legal_actions=[];
  data.item.stages=[{stage_id:'stage-settle',operation:'settle',state:'failed',receipt:{outcome:'rejected',error:{code:'lease_in_use',detail:'Another active task still holds this lease'}}}];
  globalThis.fetch=async()=>json(data);
  await render(service('logical_simulation',<ContinuityPage api={api()} initialId="work-cp2"/>));
  const outcome=container.querySelector('[aria-label="结果与结算"]');assert.match(outcome.textContent,/尚未产生终态结果/);assert.match(outcome.textContent,/工作流程有阶段失败/);assert.match(outcome.textContent,/lease_in_use.*Another active task/s);
  assert.match(container.querySelector('.checkpoint-grid').textContent,/收到 ready 报告.*未核验.*未接续/s);
});

test('allocated work without a job distinguishes task creation from waiting for resources',async()=>{
  const data=cp2Work();data.item.state='waiting';data.item.stages=[];data.jobs=[];data.allocations[0].state='Allocated';data.legal_actions=[];
  globalThis.fetch=async()=>json(data);
  await render(service('logical_simulation',<ContinuityPage api={api()} initialId="work-cp2"/>));
  let outcome=container.querySelector('[aria-label="结果与结算"]');assert.match(outcome.textContent,/租约已取得资源，任务尚未创建/);assert.doesNotMatch(outcome.textContent,/工作正在等待资源/);
  data.allocations[0].state='PreemptRequested';
  await interact(()=>button('刷新').click());
  outcome=container.querySelector('[aria-label="结果与结算"]');assert.match(outcome.textContent,/租约已收到收回通知，当前不能创建任务/);assert.doesNotMatch(outcome.textContent,/重试任务创建/);
  data.allocations[0].state='Pending';
  await interact(()=>button('刷新').click());
  outcome=container.querySelector('[aria-label="结果与结算"]');assert.match(outcome.textContent,/工作正在等待资源/);assert.doesNotMatch(outcome.textContent,/租约已取得资源/);
});

test('CP3 settlement result preview preserves exact JSON integers and is cleared on denied read',async()=>{
  const raw='{"state":{"step":1000000,"value":16159431991146531633}}\n';let reads=0;
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return ++reads===1?new Response(raw,{headers:{'Content-Type':'application/json'}}):json({error:'not_found',detail:'Artifact not visible'},404);};
  await render(<ArtifactDownload api={api()} uri="fabric-cpu://epoch-0/work/result.json" previewLabel="查看真实结果 1"/>);
  await interact(()=>button('查看真实结果 1').click());
  assert.equal(container.querySelector('pre').textContent,raw);
  assert.equal(requests[0].options.headers.Authorization,'Bearer synthetic-test-token');
  await interact(()=>button('查看真实结果 1').click());
  assert.equal(container.querySelector('pre'),null);assert.match(container.textContent,/HTTP 404/);
});

test('CP3 settlement artifact preview never leaks across identity or URI changes',async()=>{
  let resolve;
  const ownerApi=api(),otherApi=new ApiClient({owner:'alice',token:'synthetic-other'});
  globalThis.fetch=async()=>new Promise(r=>{resolve=r;});
  await render(<ArtifactDownload api={ownerApi} uri="fabric-cpu://private/result.json" previewLabel="查看结果"/>);
  await interact(()=>button('查看结果').click());
  await render(<ArtifactDownload api={otherApi} uri="fabric-cpu://private/result.json" previewLabel="查看结果"/>);
  await act(async()=>resolve(new Response('{"private":"owner-secret"}',{headers:{'Content-Type':'application/json'}})));
  assert.doesNotMatch(container.textContent,/owner-secret/);
});

test('CP3 settlement rejected business receipt is an error even with HTTP 200',async()=>{
  const {Receipt}=await import('../src/components/common.tsx');
  await render(<Receipt result={{status:200,data:{receipt:{outcome:'rejected',error:{code:'lease_in_use',detail:'Cannot release active lease'}}},raw:'{}',requestId:'cp3-rejected'}}/>);
  assert.match(container.querySelector('[role=alert]').textContent,/服务端拒绝.*lease_in_use.*Cannot release active lease/s);
  assert.equal(container.querySelector('.feedback.success'),null);
});

test('CP3 settlement completed CPU work exposes an inline result before any execution history',async()=>{
  const data=cp2Work('cpu_reference');data.item.state='completed';data.item.result={kind:'success',recorded_at:12,artifact_uris:['fabric-cpu://epoch-0/work/result.json']};data.allocations[0].state='Released';
  globalThis.fetch=async(path)=>path.startsWith('/x/v1/artifacts?')?new Response('{"state":{"value":16159431991146531633}}\n',{headers:{'Content-Type':'application/json'}}):json(data);
  await render(service('cpu_reference',<ContinuityPage api={api()} initialId="work-cp2"/>));
  const outcome=container.querySelector('[aria-label="结果与结算"]');
  assert.ok(outcome.contains(button('查看真实结果 1')));assert.ok(outcome.contains(button('下载真实结果 1')));
  await interact(()=>button('查看真实结果 1').click());
  assert.equal(outcome.querySelector('.artifact-access pre').textContent,'{"state":{"value":16159431991146531633}}\n');
  assert.match(outcome.textContent,/关联租约已不再持有资源/);
});
