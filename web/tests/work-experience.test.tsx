/** Isolated reading/creation interaction tests. No running service is contacted. */
import assert from 'node:assert/strict';
import {after,afterEach,beforeEach,test} from 'node:test';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const name of ['window','document','navigator','HTMLElement','HTMLInputElement','Event','MouseEvent','FormData'])Object.defineProperty(globalThis,name,{value:dom.window[name],configurable:true,writable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
globalThis.requestAnimationFrame=callback=>setTimeout(()=>callback(0),0);
dom.window.HTMLElement.prototype.scrollIntoView=function(){};
const React=await import('react');
const {act}=React;
const {createRoot}=await import('react-dom/client');
const {ApiClient}=await import('../src/api.ts');
const {ServiceContext}=await import('../src/service.tsx');
const {RunPage,CreateWork}=await import('../src/pages/Run.tsx');
const {ContinuityPage}=await import('../src/pages/Continuity.tsx');
const {CheckpointFacts}=await import('../src/components/CP2.tsx');
const originalFetch=globalThis.fetch;
const base={version:{epoch:0,revision:5},now:10};
const json=(value,status=200)=>new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
const work=(id,state)=>({intent_id:id,state,execution_mode:'logical_simulation',specification:{job:{image:'python:3.12-slim'}},allocation_ids:[],job_ids:[]});
const api=()=>new ApiClient({owner:'alice',token:'synthetic-work-experience-token'});
const service=child=><ServiceContext.Provider value={{mode:'logical_simulation',checkpoint:'CP4',capabilities:{checkpoint_recovery:true}}}>{child}</ServiceContext.Provider>;
let container,root,requests;
async function render(element){await act(async()=>root.render(element));}
async function interact(fn){await act(async()=>fn());}
function button(label){const found=[...container.querySelectorAll('button')].find(node=>node.textContent===label);assert.ok(found,`button ${label}`);return found;}
function disclosure(label){const found=[...container.querySelectorAll('summary')].find(node=>node.textContent===label)?.parentElement;assert.ok(found,`disclosure ${label}`);return found;}
async function input(node,value){await interact(()=>{Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype,'value').set.call(node,value);node.dispatchEvent(new dom.window.Event('input',{bubbles:true}));});}
async function select(node,value){await interact(()=>{node.value=value;node.dispatchEvent(new dom.window.Event('change',{bubbles:true}));});}
beforeEach(()=>{
  document.body.innerHTML='<div id="work-tests"></div>';container=document.getElementById('work-tests');root=createRoot(container);requests=[];
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,items:[]});};
});
afterEach(async()=>{await act(async()=>root.unmount());globalThis.fetch=originalFetch;});
after(()=>dom.window.close());

test('my work precedes creation, defaults to all states and retains current-page filters on refresh',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,items:path.startsWith('/x/v1/work-intents')?[work('work-running','running'),work('work-finished','completed'),work('work-waiting','waiting')]:[]});};
  await render(service(<RunPage api={api()} onOpen={()=>{}}/>));
  assert.equal(container.querySelector('h2').textContent,'我的工作');
  assert.equal(container.querySelector('.work-create').hidden,true);
  assert.equal(container.querySelector('[aria-label="工作状态筛选"]').value,'all');
  assert.ok(container.querySelector('[aria-label="查看工作 work-finished"]'));
  await select(container.querySelector('[aria-label="工作状态筛选"]'),'unfinished');
  assert.equal(container.querySelector('[aria-label="查看工作 work-finished"]'),null);
  assert.equal(container.querySelectorAll('.work-list-identity').length,2);
  await select(container.querySelector('[aria-label="工作状态筛选"]'),'all');
  await input(container.querySelector('[aria-label="筛选当前页工作"]'),'finished');
  assert.ok(container.querySelector('[aria-label="查看工作 work-finished"]'));
  assert.equal(container.querySelector('[aria-label="查看工作 work-running"]'),null);
  await interact(()=>button('刷新').click());
  assert.equal(container.querySelector('[aria-label="工作状态筛选"]').value,'all');
  assert.equal(container.querySelector('[aria-label="筛选当前页工作"]').value,'finished');
  assert.ok(requests.every(request=>request.options.method==='GET'),'filtering and refreshing issue no writes');
});

test('refresh keeps the selected page and an empty filtered page can show all loaded work',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({...base,items:path.startsWith('/x/v1/work-intents')?[work(path.includes('after=')?'work-second':'work-first','completed')]:[],...(path.startsWith('/x/v1/work-intents')&&!path.includes('after=')?{next_cursor:'cursor two'}:{})});};
  await render(service(<RunPage api={api()} onOpen={()=>{}}/>));
  await select(container.querySelector('[aria-label="工作状态筛选"]'),'unfinished');
  assert.match(container.textContent,/当前页没有符合筛选条件的工作/);
  await interact(()=>button('清除当前页筛选').click());
  assert.ok(container.querySelector('[aria-label="查看工作 work-first"]'));
  await interact(()=>button('下一页').click());
  await interact(()=>button('刷新').click());
  assert.match(container.textContent,/第 2 页/);
  const reads=requests.filter(request=>request.path.startsWith('/x/v1/work-intents'));
  assert.equal(reads.at(-1).path,'/x/v1/work-intents?limit=25&after=cursor%20two');
  assert.ok(container.querySelector('[aria-label="查看工作 work-second"]'));
});

test('closed creation and advanced sections preserve draft inputs and use fixed execution metadata',async()=>{
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(options.method==='POST'?{...base,receipt:{outcome:'committed'}}:{...base,items:[]},options.method==='POST'?201:200);};
  await render(service(<RunPage api={api()} onOpen={()=>{}}/>));
  await interact(()=>button('新建工作').click());
  const create=container.querySelector('.work-create');
  await input(container.querySelector('input[name=display_name]'),'实验一');
  const cpu=container.querySelector('input[name=cpu]');await input(cpu,'2500');
  await interact(()=>disclosure('高级设置').querySelector('summary').click());
  assert.equal(container.querySelector('input[name=image]').type,'hidden');
  assert.equal(container.querySelector('input[name=command]').type,'hidden');
  await interact(()=>disclosure('高级设置').querySelector('summary').click());
  await interact(()=>button('关闭新建工作').click());
  assert.equal(create.hidden,true);
  assert.equal(cpu.isConnected,true);assert.equal(cpu.value,'2500');
  await interact(()=>button('新建工作').click());
  assert.equal(create.hidden,false);
  await interact(()=>create.querySelector('form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true})));
  const writes=requests.filter(request=>request.options.method==='POST');assert.equal(writes.length,1);
  const body=JSON.parse(writes[0].options.body);
  assert.equal(body.specification.allocation.request.cpu_millicores,2500);
  assert.equal(body.specification.job.image,'python:3.12-slim');
  assert.deepEqual(body.specification.job.command,['python','-V']);
  assert.equal(body.specification.allocation.preemption_mode,'checkpoint');
  assert.equal(body.settlement_action,'release_on_terminal');
  assert.equal(body.specification.allocation.max_restarts,2);assert.equal(body.specification.job.max_retries,0);
  assert.doesNotMatch(container.textContent,/任务镜像|启动命令|不执行填写/);
});

test('an invalid advanced field opens its section and preserves the resource draft',async()=>{
  await render(service(<CreateWork api={api()} onCreated={()=>{}} onOpen={()=>{}}/>));
  await input(container.querySelector('input[name=display_name]'),'参数验证');
  await input(container.querySelector('input[name=cpu]'),'3500');
  const retries=container.querySelector('input[name=max_retries]');
  await input(retries,'-1');
  await interact(()=>retries.dispatchEvent(new dom.window.Event('invalid',{cancelable:true})));
  assert.equal(disclosure('高级设置').open,true);
  assert.equal(container.querySelector('input[name=cpu]').value,'3500');
  assert.equal(requests.length,0);
});

test('creation waits for service capabilities so its first interruption choice defaults to checkpoint recovery',async()=>{
  const client=api();const page=<RunPage api={client} onOpen={()=>{}}/>;
  await render(<ServiceContext.Provider value={{mode:null,checkpoint:null,capabilities:{}}}>{page}</ServiceContext.Provider>);
  await interact(()=>button('新建工作').click());
  assert.equal(container.querySelector('select[name=preemption]'),null,'unknown capabilities must not freeze a fallback default');
  await render(service(page));
  assert.equal(container.querySelector('select[name=preemption]').value,'checkpoint');
  assert.equal(container.querySelector('select[name=settlement]').value,'release_on_terminal');
});

test('creation blocks synchronous duplicate submissions and does not reopen or register after an accepted response',async()=>{
  let resolveWrite;const pending=new Promise(resolve=>{resolveWrite=resolve;});const opened=[];let created=0;
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return pending;};
  await render(service(<CreateWork api={api()} onCreated={()=>{created++;}} onOpen={id=>opened.push(id)}/>));
  await input(container.querySelector('input[name=display_name]'),'单次提交');
  const form=container.querySelector('form');
  const submit=()=>form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));
  await interact(()=>{submit();submit();});
  assert.equal(requests.length,1);
  const registered=JSON.parse(requests[0].options.body);
  assert.equal(button('正在登记…').disabled,true);
  await act(async()=>{resolveWrite(json({...base,receipt:{outcome:'committed'}},201));});
  assert.equal(created,1);assert.deepEqual(opened,[registered.intent_id]);
  await interact(submit);
  assert.equal(requests.length,1);assert.equal(created,1);assert.deepEqual(opened,[registered.intent_id]);
});

test('unknown registration results keep the original ID and input and block a programmatic resubmission',async()=>{
  const opened=[];
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json({detail:'The response was interrupted.'},503);};
  await render(service(<CreateWork api={api()} onCreated={()=>assert.fail('unknown results are not success')} onOpen={id=>opened.push(id)}/>));
  await input(container.querySelector('input[name=display_name]'),'保留此项');
  await input(container.querySelector('input[name=cpu]'),'2500');
  const form=container.querySelector('form');
  const submit=()=>form.dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));
  await interact(submit);
  assert.equal(requests.length,1);assert.equal(opened.length,0);
  const registered=JSON.parse(requests[0].options.body);
  assert.match(container.textContent,/暂未确认结果/);
  assert.equal(button('重试同一工作登记').disabled,true);
  assert.equal(container.querySelector('input[name=display_name]').value,'保留此项');
  assert.equal(container.querySelector('input[name=cpu]').value,'2500');
  await interact(submit);
  assert.equal(requests.length,1,'unknown results require an explicit query, never a blind replay');
  await interact(()=>button('查询这项工作').click());
  assert.deepEqual(opened,[registered.intent_id]);
});

test('finished work promotes only its legal release while keeping the same versioned command',async()=>{
  const data={...base,item:{...work('work-complete','completed'),settlement_action:'keep_lease',stages:[],attempts:[],result:{kind:'success'}},allocations:[{allocation_id:'lease-1',state:'Allocated'}],jobs:[],events:[],legal_actions:[{operation:'allocation.release',entity:{kind:'allocation',entity_id:'lease-1'}}]};
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(options.method==='POST'?{receipt:{outcome:'committed'}}:data);};
  const confirm=window.confirm;window.confirm=()=>true;
  try{
    await render(service(<ContinuityPage api={api()} initialId="work-complete"/>));
    assert.equal(container.querySelector('.work-primary-action button').textContent,'释放租约');
    assert.equal([...container.querySelectorAll('button')].some(b=>b.textContent==='恢复这项工作'),false);
    assert.equal([...container.querySelectorAll('summary')].some(e=>e.textContent==='更多操作'),false);
    assert.equal(disclosure('技术详情').open,false);
    assert.match(container.querySelector('[aria-label="结果与结算"]').textContent,/资源尚未归还/);
    await interact(()=>button('释放租约').click());
    const writes=requests.filter(request=>request.options.method==='POST');assert.equal(writes.length,1);
    assert.equal(writes[0].path,'/x/v1/work-intents/work-complete/actions');
    assert.deepEqual(JSON.parse(writes[0].options.body),{action:'release',expected_epoch:base.version.epoch});
  }finally{window.confirm=confirm;}
});

test('concise checkpoint summary does not equate a ready report or verified artifact with continuation',async()=>{
  const client=api();
  await render(<CheckpointFacts api={client} checkpoint={{report_status:'ready',verification_status:'unverified',continuation_status:'not_attempted'}}/>);
  assert.equal(container.querySelector('.checkpoint-summary').textContent,'正在检查保存点');
  assert.equal(disclosure('保存点记录').open,false);
  await render(<CheckpointFacts api={client} checkpoint={{report_status:'ready',verification_status:'verified',continuation_status:'not_attempted'}}/>);
  assert.equal(container.querySelector('.checkpoint-summary').textContent,'进度已保存');
  await render(<CheckpointFacts api={client} checkpoint={{report_status:'ready',verification_status:'verified',continuation_status:'applied'}}/>);
  assert.equal(container.querySelector('.checkpoint-summary').textContent,'已从保存点继续');
  await render(<CheckpointFacts api={client} checkpoint={{report_status:'ready',verification_status:'failed',continuation_status:'not_attempted'}}/>);
  assert.match(container.querySelector('.checkpoint-summary').textContent,/保存点无法使用/);
  await render(<CheckpointFacts api={client} checkpoint={{report_status:'ready',verification_status:'verified',continuation_status:'failed'}}/>);
  assert.match(container.querySelector('.checkpoint-summary').textContent,/恢复失败/);
  assert.equal(requests.length,0);
});
