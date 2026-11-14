/** Clipboard/UI tests with synthetic reads only; no service or real work is used. */
import assert from 'node:assert/strict';
import {after,afterEach,beforeEach,test} from 'node:test';
import {JSDOM} from 'jsdom';

const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const name of ['window','document','navigator','HTMLElement','HTMLInputElement','Event','MouseEvent','FormData'])Object.defineProperty(globalThis,name,{value:dom.window[name],configurable:true,writable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');
const {act}=React;
const {createRoot}=await import('react-dom/client');
const {CopyWorkId}=await import('../src/components/CopyWorkId.tsx');
const {ApiClient}=await import('../src/api.ts');
const {RunPage}=await import('../src/pages/Run.tsx');
const {ContinuityPage}=await import('../src/pages/Continuity.tsx');
const {OverviewPage}=await import('../src/pages/Overview.tsx');
const {AccountContext}=await import('../src/session.ts');
const fullId='work-a383db08-d613-4e00-aba0-9d61d2ba9801';
const originalFetch=globalThis.fetch;
const base={version:{epoch:0,revision:5},now:10};
const json=value=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
const work={intent_id:fullId,owner_id:'alice',state:'running',execution_mode:'logical_simulation',allocation_ids:[],job_ids:[],specification:{job:{image:'synthetic:1'}}};
let container,root,copied,requests;
const clipboard=writeText=>Object.defineProperty(navigator,'clipboard',{value:{writeText},configurable:true});
const render=element=>act(async()=>root.render(element));
const click=element=>act(async()=>element.click());
const copyButton=()=>container.querySelector(`button[aria-label="复制工作 ID ${fullId}"]`);
beforeEach(()=>{
  document.body.innerHTML='<div id="copy-tests"></div>';container=document.getElementById('copy-tests');root=createRoot(container);copied=[];requests=[];
  clipboard(async text=>{copied.push(text);});
  globalThis.fetch=async(path,options)=>{requests.push({path,options});return json(path.startsWith('/x/v1/work-intents/')?{...base,item:work,allocations:[],jobs:[],events:[],legal_actions:[]}:{...base,items:path.startsWith('/x/v1/work-intents')?[work]:[]});};
});
afterEach(async()=>{await act(async()=>root?.unmount());globalThis.fetch=originalFetch;delete navigator.clipboard;});
after(()=>dom.window.close());

test('copy uses the full ID and does not submit forms or bubble into row navigation',async()=>{
  let submitted=0,navigated=0;
  await render(<form onSubmit={event=>{event.preventDefault();submitted++;}} onClick={()=>navigated++}><CopyWorkId id={fullId}/></form>);
  await click(copyButton());
  assert.deepEqual(copied,[fullId]);assert.equal(submitted,0);assert.equal(navigated,0);
  assert.equal(container.querySelector('[role="status"]').textContent,'已复制完整工作 ID');
  assert.equal(copyButton().getAttribute('aria-busy'),'false');
});

test('unavailable or rejected clipboard offers the complete selectable ID without a fake success',async()=>{
  clipboard(async()=>{throw new Error('Denied');});
  await render(<CopyWorkId id={fullId} compact/>);await click(copyButton());
  const field=container.querySelector('input');
  assert.ok(field);assert.equal(field.readOnly,true);assert.equal(field.value,fullId);
  assert.match(container.querySelector('[role="status"]').textContent,/无法自动复制/);
  await click(field);assert.equal(field.selectionStart,0);assert.equal(field.selectionEnd,fullId.length);
  delete navigator.clipboard;
  await click(copyButton());assert.match(container.querySelector('[role="status"]').textContent,/无法自动复制/);
});

test('a pending clipboard request cannot put feedback onto a different ID or account',async()=>{
  const pending=[];clipboard(text=>new Promise(resolve=>pending.push({text,resolve})));
  const alice={},bob={};
  await render(<CopyWorkId id={fullId} scope={alice}/>);await click(copyButton());
  assert.equal(copyButton().disabled,true);
  await render(<CopyWorkId id="work-new" scope={alice}/>);
  await act(async()=>pending[0].resolve());
  assert.equal(container.querySelector('[role="status"]').textContent,'');
  await render(<CopyWorkId id={fullId} scope={alice}/>);await click(copyButton());
  await render(<CopyWorkId id={fullId} scope={bob}/>);
  assert.equal(copyButton().disabled,false);
  await act(async()=>pending[1].resolve());
  assert.equal(container.querySelector('[role="status"]').textContent,'');
  await click(copyButton());await act(async()=>pending[2].resolve());
  assert.equal(container.querySelector('[role="status"]').textContent,'已复制完整工作 ID');
  await render(<CopyWorkId id="work-new" scope={bob}/>);
  await render(<CopyWorkId id={fullId} scope={bob}/>);
  assert.equal(container.querySelector('[role="status"]').textContent,'','returning to an old ID must not resurrect a stale success');
});

test('clipboard completion after unmount is ignored',async()=>{
  let finish;clipboard(()=>new Promise(resolve=>{finish=resolve;}));
  await render(<CopyWorkId id={fullId}/>);await click(copyButton());
  await act(async()=>root.unmount());root=null;
  await act(async()=>finish());assert.equal(container.textContent,'');
});

test('work lists copy without opening a work and the detail header exposes copy outside disclosures',async()=>{
  const api=new ApiClient({owner:'alice',token:'synthetic-copy-test'});let opened=0;
  await render(<RunPage api={api} onOpen={()=>opened++}/>);
  await click(copyButton());assert.equal(opened,0);assert.deepEqual(copied,[fullId]);
  await render(<ContinuityPage api={api} initialId={fullId}/>);
  const button=copyButton();assert.ok(button);assert.equal(button.closest('details'),null);assert.ok(button.closest('.panel-heading'));
  await click(button);assert.deepEqual(copied,[fullId,fullId]);
  assert.ok(requests.every(({options})=>options.method==='GET'));
});

test('overview copy controls are siblings of navigation links and keep their destinations',async()=>{
  const api=new ApiClient({owner:'alice',token:'synthetic-copy-overview'});
  await render(<AccountContext.Provider value={{actor_id:'alice',owner_id:'alice',capabilities:['use_compute'],is_administrator:false}}><OverviewPage api={api}/></AccountContext.Provider>);
  const button=copyButton();assert.ok(button);assert.equal(button.closest('a'),null);
  const link=container.querySelector('.overview-work-row');assert.match(link.getAttribute('href'),new RegExp(fullId));
  await click(button);assert.deepEqual(copied,[fullId]);assert.equal(window.location.hash,'');
  assert.ok(requests.every(({options})=>options.method==='GET'));
});
