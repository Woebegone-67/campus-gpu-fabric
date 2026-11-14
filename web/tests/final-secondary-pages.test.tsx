/** Final interaction regressions with synthetic API responses, not live acceptance. */
import assert from 'node:assert/strict';
import {after,afterEach,beforeEach,test} from 'node:test';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<html><body></body></html>',{url:'http://localhost/'});
for(const name of ['window','document','navigator','HTMLElement','HTMLInputElement','Event','MouseEvent','FormData'])Object.defineProperty(globalThis,name,{value:dom.window[name],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react'),{act}=React;
const {createRoot}=await import('react-dom/client');
const {ApiClient}=await import('../src/api.ts');
const {ServiceContext}=await import('../src/service.tsx');
const {ResourcesPage,RecallForm}=await import('../src/pages/Resources.tsx');
const {AcceptancePage}=await import('../src/pages/Acceptance.tsx');
const {DecisionsPage}=await import('../src/pages/Decisions.tsx');
const {EventsPage}=await import('../src/pages/Events.tsx');
const {Experiments}=await import('../src/components/Experiments.tsx');
const originalFetch=globalThis.fetch;
const base={version:{epoch:0,revision:3},now:10};
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const api=()=>new ApiClient({owner:'alice',token:'synthetic-secondary'});
const node={node_id:'own-node',contributor_owner_id:'alice',state:'Ready',devices:[{device_id:'gpu-one',model:'A10',state:'occupied'}]};
let root,container,calls;
async function render(element){await act(async()=>root.render(element));}
async function click(label){const b=[...container.querySelectorAll('button')].find(e=>e.textContent===label);assert.ok(b,label);await act(async()=>b.click());}
async function openTool(title){const d=[...container.querySelectorAll('summary')].find(e=>e.textContent===title)?.parentElement;assert.ok(d,title);await act(async()=>{d.open=true;d.dispatchEvent(new dom.window.Event('toggle'));});return d;}
beforeEach(()=>{document.body.innerHTML='<div id="root"></div>';container=document.getElementById('root');root=createRoot(container);calls=[];dom.window.confirm=()=>true;globalThis.fetch=async(path,options)=>{calls.push({path,options});return json({...base,items:[]});};});
afterEach(async()=>{await act(async()=>root.unmount());globalThis.fetch=originalFetch;});
after(()=>dom.window.close());

test('CPU device page has no inactive registration branch and no recall form without devices',async()=>{
  globalThis.fetch=async(path,options)=>{calls.push({path,options});return json({...base,items:[],nodes:[]});};
  await render(<ServiceContext.Provider value={{mode:'cpu_reference',checkpoint:'CP4',capabilities:{}}}><ResourcesPage api={api()}/></ServiceContext.Provider>);
  assert.match(container.textContent,/暂无自己的设备/);
  assert.equal(container.querySelector('form'),null);
  assert.doesNotMatch(container.textContent,/CPU 参考实例供给|登记模拟设备|逻辑时刻|收回设备/);
});

test('recall requires specific confirmation and ignores selection removed by a fresh device read',async()=>{
  const client=api();await render(<RecallForm api={client} nodes={[node]} onDone={()=>{}}/>);
  await act(async()=>container.querySelector('input[type=checkbox]').click());
  let confirmation='';dom.window.confirm=message=>{confirmation=message;return false;};
  await click('发送收回通知');
  assert.match(confirmation,/1 张 GPU.*工作将被中断/);assert.equal(calls.length,0);
  await render(<RecallForm api={client} nodes={[]} onDone={()=>{}}/>);
  assert.equal(container.querySelector('button'),null);assert.equal(calls.length,0);
});

test('device pagination opens the next server cursor and refresh returns to the first page',async()=>{
  globalThis.fetch=async(path,options)=>{calls.push({path,options});return json(path.startsWith('/x/v1/contributions')?{...base,items:[node],next_cursor:path.includes('after=')?null:'0:3:50'}:{...base,nodes:[]});};
  await render(<ResourcesPage api={api()}/>);
  await click('下一页');assert.equal(calls.at(-1).path,'/x/v1/contributions?limit=50&after=0%3A3%3A50');
  await click('刷新');assert.ok(calls.slice(-2).some(call=>call.path==='/x/v1/contributions?limit=50'));
  assert.doesNotMatch(container.textContent,/第 2 页/);
});

test('system management loads expensive tools only when opened and keeps not-run checks honest',async()=>{
  globalThis.fetch=async(path,options)=>{calls.push({path,options});return json(path==='/v1/version'?{execution_mode:'cpu_reference'}:path==='/x/v1/admin/overview'?{...base,counts:{work_intents:4,jobs:3,allocations:3},work_by_state:{running:2,failed:1},capacity:{nodes:[],totals:{capacity:{gpu_count:2},available:{gpu_count:1}}}}:path==='/x/v1/acceptance'?{...base,items:[{target_id:'stability',status:'NOT_RUN',actual:null}]}:{...base,items:[]});};
  await render(<AcceptancePage api={api()} showAdminClock/>);
  assert.deepEqual(calls.map(c=>c.path),['/v1/version','/x/v1/admin/overview']);
  assert.match(container.textContent,/系统状态/);assert.doesNotMatch(container.textContent,/模拟时钟|CP3|当前阶段|不代表/);
  await openTool('验证记录');assert.equal(calls.at(-1).path,'/x/v1/acceptance');assert.match(container.textContent,/未运行/);assert.equal(container.querySelector('.status-met'),null);
  assert.ok(calls.every(c=>!c.path.startsWith('/x/v1/experiments')&&!c.path.startsWith('/x/v1/policy-validation')));
});

test('a selected plan without a committed event never appears executed; owner comparison scores stay out of the primary table',async()=>{
  globalThis.fetch=async()=>json({...base,scope:'owner',item:{selected_candidate_id:'mine',coverage:{scope:'partial',examined_count:1},policy:{name:'B2'},candidates:[{candidate_id:'mine',action:'allocate',feasibility:'legal',scores:[],violations:[]}]},actual_events:[],commit_receipts:[]});
  await render(<DecisionsPage api={api()} initialId="decision-one"/>);
  assert.match(container.textContent,/已选择方案 · 尚无执行记录/);assert.doesNotMatch(container.textContent,/已执行|决定已提交|不代表/);
  assert.equal(container.querySelectorAll('thead th').length,3);
  const details=container.querySelector('details.raw');assert.equal(details.open,false);assert.equal(details.querySelector('summary').textContent,'技术详情');
});

test('activity page searches readable labels and does not mount experiments even through the legacy flag',async()=>{
  globalThis.fetch=async(path,options)=>{calls.push({path,options});return json({...base,items:[{event_id:'e1',event_type:'checkpoint.reported',logical_time:2.12345},{event_id:'e2',event_type:'job.failed',logical_time:3}]});};
  await render(<EventsPage api={api()} showExperiments/>);
  assert.deepEqual(calls.map(c=>c.path),['/v1/events?limit=50']);
  const input=container.querySelector('input');await act(async()=>{Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(input,'保存报告');input.dispatchEvent(new dom.window.Event('input',{bubbles:true}));});
  assert.equal(container.querySelectorAll('.timeline li').length,1);assert.match(container.querySelector('.timeline').textContent,/收到保存报告/);assert.doesNotMatch(container.querySelector('.timeline').textContent,/进度已保存|checkpoint.reported/);
});

test('activity without name metadata uses a neutral work link and shows repeated states only once',async()=>{
  const id='work-8bb481e0-89d9-4ebd-8f62-1c6413db971c';
  const changes=[{entity:{kind:'allocation',entity_id:'lease-1'},after:'Running'},{entity:{kind:'intent',entity_id:id},after:'running'},{entity:{kind:'intent',entity_id:id},after:'running'},{entity:{kind:'job',entity_id:'job-2'},after:'Pending'}];
  globalThis.fetch=async(path,options)=>{calls.push({path,options});return json({...base,items:[{event_id:'e1',event_type:'job.started',logical_time:2,state_changes:changes}]});};
  await render(<EventsPage api={api()}/>);
  const timeline=container.querySelector('.timeline'),links=timeline.querySelectorAll('a');
  assert.equal(links.length,1);assert.equal(links[0].getAttribute('href'),`#continuity?intent=${id}`);
  assert.equal(links[0].textContent,'工作 · work-8bb481e0…db971c');
  assert.doesNotMatch(timeline.textContent,/未命名工作/);
  const states=[...timeline.querySelectorAll('.status')].map(node=>node.textContent);
  assert.deepEqual(states,['运行中','等待分配'],'different states remain visible while equivalent running states are deduplicated');
  assert.deepEqual(calls.map(call=>call.path),['/v1/events?limit=50'],'event presentation must not add per-work reads');
  const raw=container.querySelector('details.raw');assert.equal(raw.open,false);
  assert.deepEqual(JSON.parse(raw.querySelector('pre').textContent).items[0].state_changes,changes,'technical evidence retains every original state change');
});

test('unknown experiment submission cannot be resubmitted through the form',async()=>{
  globalThis.fetch=async(path,options)=>{calls.push({path,options});if(options.method==='POST')throw new TypeError('synthetic lost response');return json(path.endsWith('/catalog')?{items:[{scenario_id:'case',label:'场景',policies:['B0'],seed_min:0,seed_max:10,work_count:2,horizon:20}]}:{items:[]});};
  await render(<Experiments api={api()}/>);
  const form=container.querySelector('form');
  await act(async()=>form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})));
  await act(async()=>form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(calls.filter(c=>c.options.method==='POST').length,1);
  assert.match(container.textContent,/请刷新实验列表确认提交结果/);
});
