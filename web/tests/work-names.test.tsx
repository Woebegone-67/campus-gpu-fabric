/** Synthetic UI interactions only; persistence is verified by the backend suite. */
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
const {AccountContext}=await import('../src/session.ts');
const {RunPage,CreateWork}=await import('../src/pages/Run.tsx');
const {ContinuityPage}=await import('../src/pages/Continuity.tsx');
const {OverviewPage}=await import('../src/pages/Overview.tsx');
const {EventsPage}=await import('../src/pages/Events.tsx');
const {DecisionsPage}=await import('../src/pages/Decisions.tsx');
const originalFetch=globalThis.fetch;
const base={version:{epoch:0,revision:5},now:10};
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const work=(id,name,state='running')=>({intent_id:id,display_name:name,owner_id:'alice',state,execution_mode:'logical_simulation',specification:{job:{image:'example:1'}},allocation_ids:[],job_ids:[]});
const detail=item=>({...base,item,allocations:[],jobs:[],events:[],legal_actions:[]});
const client=(owner='alice')=>new ApiClient({owner,token:`synthetic-name-${owner}`});
const service=child=><ServiceContext.Provider value={{mode:'logical_simulation',checkpoint:'CP4',capabilities:{checkpoint_recovery:true}}}>{child}</ServiceContext.Provider>;
let container,root,requests;
const render=element=>act(async()=>root.render(element));
const interact=callback=>act(async()=>callback());
async function input(node,value){await interact(()=>{Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(node,value);node.dispatchEvent(new dom.window.Event('input',{bubbles:true}));});}
const submit=()=>interact(()=>container.querySelector('form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})));
beforeEach(()=>{
  document.body.innerHTML='<div id="name-tests"></div>';container=document.getElementById('name-tests');root=createRoot(container);requests=[];
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,items:[]});};
});
afterEach(async()=>{await act(async()=>root.unmount());globalThis.fetch=originalFetch;});
after(()=>dom.window.close());

test('creation requires its first field, trims the name and counts Unicode characters',async()=>{
  await render(service(<CreateWork api={client()} onCreated={()=>{}} onOpen={()=>{}}/>));
  const name=container.querySelector('input');
  assert.equal(name.name,'display_name');assert.equal(name.required,true);
  await input(name,'   ');await submit();
  assert.equal(requests.length,0);assert.match(container.textContent,/请输入工作名称/);assert.equal(document.activeElement,name);
  await input(name,'界'.repeat(81));await submit();
  assert.equal(requests.length,0);assert.match(container.textContent,/不能超过 80 个字符/);
  const valid='🔬'.repeat(80);
  await input(name,`  ${valid}  `);await submit();
  assert.equal(requests.length,1);assert.equal(JSON.parse(requests[0].options.body).display_name,valid);
  assert.equal(name.closest('fieldset').disabled,true);
});

test('a registration retry retains the same name, ID and complete input',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return requests.length===1?json({error:'temporarily_blocked',detail:'Retry the same registration'},409):json({...base,receipt:{outcome:'committed'}},201);};
  await render(service(<CreateWork api={client()} onCreated={()=>{}} onOpen={()=>{}}/>));
  const name=container.querySelector('input[name=display_name]');
  await input(name,'  毕业实验  ');await submit();
  assert.equal(name.closest('fieldset').disabled,true);
  name.value='changed outside the disabled form';
  await submit();
  assert.equal(requests.length,2);assert.equal(requests[0].options.body,requests[1].options.body);
  const submitted=JSON.parse(requests[1].options.body);
  assert.equal(submitted.display_name,'毕业实验');assert.match(submitted.intent_id,/^work-/);
  assert.match(container.querySelector('.helper strong').textContent,/毕业实验/);
});

test('list names may repeat, remain safe text, filter locally and open by the original ID',async()=>{
  const name='<img src=x> 测试';
  const rows=[work('work-first',name),work('work-second',name),work('work-legacy',undefined)];
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,items:path.startsWith('/x/v1/work-intents')?rows:[]});};
  const opened=[];
  await render(service(<RunPage api={client()} onOpen={id=>opened.push(id)}/>));
  assert.equal(container.querySelectorAll('img').length,0);
  assert.match(container.textContent,/未命名工作 · work-legacy/);
  await input(container.querySelector('[aria-label="筛选当前页工作"]'),'测试');
  assert.equal(container.querySelectorAll('.work-list-identity').length,2);
  const names=container.querySelectorAll('.work-list-identity .link-button');
  assert.equal(names[0].textContent,name);assert.equal(names[1].textContent,name);
  await interact(()=>names[1].click());assert.deepEqual(opened,['work-second']);
  assert.ok(container.querySelector('[aria-label="复制工作 ID work-second"]'));
  assert.ok(requests.every(({options})=>options.method==='GET'));
});

test('the same persisted name appears in details, associated events and decision context',async()=>{
  const id='work-context',name='遥感样本验证';
  const value=detail(work(id,name));
  value.events=[{event_id:'event-1',event_type:'intent.registered',logical_time:1,sequence:1,state_changes:[{entity:{kind:'intent',entity_id:id},before:null,after:'pending'}],decision_id:'decision-1'}];
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(path.startsWith('/x/v1/decisions/')?{...base,item:{decision_id:'decision-1',coverage:{},candidates:[]}}:value);};
  const api=client();
  await render(service(<ContinuityPage api={api} initialId={id}/>));
  assert.ok([...container.querySelectorAll('h2')].some(node=>node.textContent===name));
  assert.equal(container.querySelector('a[href^="#events"]').getAttribute('href'),`#events?intent=${id}`);
  await render(<EventsPage api={api} intentId={id} showExperiments={false}/>);
  assert.equal(container.querySelector('.event-context strong').textContent,name);
  assert.match(container.querySelector('.timeline').textContent,/遥感样本验证/);
  await input(container.querySelector('.search'),name);
  assert.equal(container.querySelectorAll('.timeline>li').length,1);
  assert.equal(container.querySelector('a[href^="#decisions"]').getAttribute('href'),`#decisions?decision=decision-1&intent=${id}`);
  await render(<DecisionsPage api={api} initialId="decision-1" workId={id}/>);
  assert.equal(container.querySelector('.event-context strong').textContent,name);
  assert.equal(container.querySelector('a').getAttribute('href'),`#continuity?intent=${id}`);
  assert.ok(requests.every(({options})=>options.method==='GET'&&options.headers['X-Owner']==='alice'));
});

test('overview names cover attention, current work and completed results with a legacy fallback',async()=>{
  const rows=[work('work-notice','需要处理的实验','manual_action'),work('work-current','正在计算的实验'),work('work-result','昨日结果','completed'),work('work-legacy',undefined)];
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,items:path.startsWith('/x/v1/work-intents')?rows:[]});};
  await render(<AccountContext.Provider value={{actor_id:'alice',owner_id:'alice',capabilities:['use_compute'],is_administrator:false}}><OverviewPage api={client()}/></AccountContext.Provider>);
  assert.match(container.querySelector('.attention-row').textContent,/需要处理的实验/);
  assert.match(container.querySelector('.overview-work-list').textContent,/正在计算的实验/);
  assert.match(container.querySelector('.overview-work-list').textContent,/未命名工作 · work-legacy/);
  assert.match(container.querySelector('.overview-results').textContent,/昨日结果/);
  assert.equal(container.querySelector('.overview-result').getAttribute('href'),'#continuity?intent=work-result');
});

test('decision context cannot retain an old account name after unauthorized lookup',async()=>{
  globalThis.fetch=async(path,options)=>{
    requests.push({path,options});
    if(path.startsWith('/x/v1/work-intents/'))return options.headers['X-Owner']==='alice'?json(detail(work('work-private','Alice 的私有名称'))):json({error:'not_found',detail:'The requested object was not found.'},404);
    return json({...base,item:{decision_id:'decision-1',coverage:{},candidates:[]}});
  };
  await render(<DecisionsPage api={client()} initialId="decision-1" workId="work-private"/>);
  assert.match(container.textContent,/Alice 的私有名称/);
  await render(<DecisionsPage api={client('bob')} initialId="decision-1" workId="work-private"/>);
  assert.doesNotMatch(container.textContent,/Alice 的私有名称/);
  assert.match(container.textContent,/HTTP 404/);
});
