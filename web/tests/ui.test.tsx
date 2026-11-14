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
  dom.window.history.replaceState(null,'','#decisions?decision=decision-one');
  globalThis.location=dom.window.location;
  globalThis.fetch=async(path)=>{requests.push({path});return json(path==='/healthz'?{control_plane_ready:true}:decision(path.split('/').at(-1)));};
  await render(<App/>);
  assert.equal(field('决策 ID').value,'decision-one');
  assert.equal(requests.filter(r=>r.path==='/x/v1/decisions/decision-one').length,1);
  await interact(()=>{dom.window.history.replaceState(null,'','#decisions?decision=decision-two');dom.window.dispatchEvent(new dom.window.HashChangeEvent('hashchange'));});
  assert.equal(field('决策 ID').value,'decision-two');
  assert.equal(requests.filter(r=>r.path==='/x/v1/decisions/decision-two').length,1);
  assert.doesNotMatch(container.textContent,/说明 decision-one/);
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
