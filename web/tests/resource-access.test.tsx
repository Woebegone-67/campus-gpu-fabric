/** Capability-driven presentation tests with synthetic, isolated responses. */
import assert from 'node:assert/strict';
import {after,afterEach,beforeEach,test} from 'node:test';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const name of ['window','document','navigator','HTMLElement','HTMLInputElement','Event','MouseEvent','FormData'])Object.defineProperty(globalThis,name,{value:dom.window[name],configurable:true,writable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {act}=React;
const {createRoot}=await import('react-dom/client');
const {ApiClient}=await import('../src/api.ts');
const {ServiceContext}=await import('../src/service.tsx');
const {ResourcesPage,RecallForm}=await import('../src/pages/Resources.tsx');
const {EventsPage}=await import('../src/pages/Events.tsx');
const originalFetch=globalThis.fetch;
const base={version:{epoch:0,revision:5},now:10};
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const api=()=>new ApiClient({owner:'alice',token:'synthetic-resource-access',profile:'admin'});
const ownNode={node_id:'own-node',state:'Ready',contributor_owner_id:'alice',capacity:{cpu_millicores:4000,memory_mib:8192},devices:[{device_id:'own-card',physical_gpu_id:'own-card',model:'A10',state:'available'},{device_id:'own-slice',physical_gpu_id:'own-card',model:'A10',profile:'slice',state:'available'}]};
const otherNode={node_id:'other-node',state:'Ready',contributor_owner_id:'bob',devices:[{device_id:'other-card',model:'A100'}]};
const service=child=><ServiceContext.Provider value={{mode:'logical_simulation',checkpoint:'CP4',capabilities:{checkpoint_recovery:true}}}>{child}</ServiceContext.Provider>;
let container,root,requests;
async function render(element){await act(async()=>root.render(element));}
async function interact(fn){await act(async()=>fn());}
function button(label){const found=[...container.querySelectorAll('button')].find(node=>node.textContent===label);assert.ok(found,`button ${label}`);return found;}
function disclosure(label){const found=[...container.querySelectorAll('summary')].find(node=>node.textContent===label)?.parentElement;assert.ok(found,`disclosure ${label}`);return found;}
beforeEach(()=>{
  document.body.innerHTML='<div id="resource-tests"></div>';container=document.getElementById('resource-tests');root=createRoot(container);requests=[];
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,items:[],nodes:[]});};
});
afterEach(async()=>{await act(async()=>root.unmount());globalThis.fetch=originalFetch;});
after(()=>dom.window.close());

test('compute-only view does not query contributions or mount write forms despite a local admin label',async()=>{
  await render(service(<ResourcesPage api={api()} canContribute={false}/>));
  assert.deepEqual(requests.map(request=>request.path),['/v1/capacity']);
  assert.equal(container.querySelector('form'),null);
  assert.match(container.textContent,/当前账户未启用贡献设备能力/);
  assert.equal(disclosure('查看可见资源池').open,false);
  assert.match(container.textContent,/并非你的自有设备清单/);
});

test('owned devices precede the shared pool and recall deduplicates physical cards without exposing another owner',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(options.method==='POST'?{receipt:{outcome:'committed'}}:path.startsWith('/x/v1/contributions')?{...base,items:[ownNode,otherNode]}:{...base,nodes:[]});};
  await render(service(<ResourcesPage api={api()}/>));
  assert.equal(container.querySelector('h2').textContent,'我的设备');
  assert.equal(disclosure('高级 · 登记模拟供给').open,false);
  assert.match(container.textContent,/1 个物理 GPU/);
  assert.doesNotMatch(container.textContent,/other-node|other-card/);
  const checkboxes=container.querySelectorAll('input[type=checkbox]');assert.equal(checkboxes.length,1);
  await interact(()=>checkboxes[0].click());
  await interact(()=>button('发送收回通知').click());
  const writes=requests.filter(request=>request.options.method==='POST');assert.equal(writes.length,1);
  assert.equal(writes[0].path,'/x/v1/contributions/recall');
  assert.deepEqual(JSON.parse(writes[0].options.body),{gpu_ids:['own-card'],notice_seconds:30,duration_seconds:60});
});

test('revoking contribution capability aborts pending reads and removes the old forms immediately',async()=>{
  let resolveContribution,signal;
  globalThis.fetch=async(path,options)=>{requests.push({path,options});if(path.startsWith('/x/v1/contributions')){signal=options.signal;return new Promise(resolve=>{resolveContribution=resolve;});}return json({...base,nodes:[]});};
  const client=api();await render(service(<ResourcesPage api={client} canContribute/>));
  await render(service(<ResourcesPage api={client} canContribute={false}/>));
  assert.equal(signal.aborted,true);assert.equal(container.querySelector('form'),null);
  await act(async()=>resolveContribution(json({...base,items:[ownNode]})));
  assert.doesNotMatch(container.textContent,/own-node/);assert.equal(container.querySelector('form'),null);
});

test('unknown recall response blocks both button retry and a repeated form submission',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});throw new TypeError('synthetic dropped response');};
  await render(<RecallForm api={api()} nodes={[ownNode]} onDone={()=>{}}/>);
  await interact(()=>container.querySelector('input[type=checkbox]').click());
  const form=container.querySelector('form');
  await interact(()=>form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(button('发送收回通知').disabled,true);
  await interact(()=>form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(requests.length,1);assert.match(container.textContent,/结果尚不确定/);
});

test('unknown contribution registration blocks resubmission while its configuration remains available',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});if(options.method==='POST')throw new TypeError('synthetic dropped response');return json({...base,items:[],nodes:[]});};
  await render(service(<ResourcesPage api={api()}/>));
  const advanced=disclosure('高级 · 登记模拟供给');const textarea=advanced.querySelector('textarea');
  await interact(()=>{textarea.value=JSON.stringify(ownNode);});
  const form=advanced.querySelector('form');
  await interact(()=>form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(button('登记供给').disabled,true);
  await interact(()=>form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})));
  assert.equal(requests.filter(request=>request.options.method==='POST').length,1);
  assert.equal(JSON.parse(textarea.value).node_id,'own-node');
});

test('ordinary work events never mount experiments when showExperiments is false',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,events:[{event_id:'event-1',event_type:'job.started',logical_time:0,sequence:1}],items:[]});};
  await render(<EventsPage api={api()} intentId="work-one" showExperiments={false}/>);
  assert.deepEqual(requests.map(request=>request.path),['/x/v1/work-intents/work-one']);
  assert.match(container.textContent,/这项工作的事件.*任务开始运行/s);
  assert.doesNotMatch(container.textContent,/隔离实验记录|运行已登记场景|管理员 · 加载实验目录/);
  assert.ok(button('导出这项工作的事件'));
});

test('event refresh preserves pagination and the default still provides the legacy experiment entry',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,items:path.startsWith('/v1/events')?[{event_id:'event-1',event_type:'job.started',logical_time:0,sequence:1}]:[],has_more:!path.includes('after='),next_after:'next-page'});};
  const client=new ApiClient({owner:'alice',token:'synthetic-events'});
  await render(<EventsPage api={client} showExperiments={false}/>);
  await interact(()=>button('下一页').click());
  await interact(()=>button('刷新').click());
  assert.equal(requests.at(-1).path,'/v1/events?limit=50&after=next-page');
  assert.match(container.textContent,/第 2 页/);
  await render(<EventsPage api={client}/>);
  assert.match(container.textContent,/隔离实验记录/);
});
