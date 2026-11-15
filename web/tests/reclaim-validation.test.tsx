import assert from 'node:assert/strict';
import {after,afterEach,beforeEach,test} from 'node:test';
import {JSDOM} from 'jsdom';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const name of ['window','document','navigator','HTMLElement','Event','MouseEvent'])Object.defineProperty(globalThis,name,{value:dom.window[name],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react'),{act}=React;const {createRoot}=await import('react-dom/client');
const {ApiClient}=await import('../src/api.ts');
const {ReclaimValidation}=await import('../src/components/ReclaimValidation.tsx');
let root,container;const originalFetch=globalThis.fetch;
const api=()=>new ApiClient({owner:'',token:'synthetic-admin'});
const json=value=>new Response(JSON.stringify(value),{headers:{'Content-Type':'application/json'}});
const report={status:'ANALYZED',model_label:'参数模型回放',planner_matches_current:false,summary:[{return_policy:'preserve_work',policy:'adaptive',label:'自适应收回',cases:4,mean_cost:3.25,mean_owner_wait_seconds:1.5,deadline_misses:1,checkpoint_failures:0},{return_policy:'return_first',policy:'adaptive',label:'自适应收回',cases:4,mean_cost:2.5,mean_owner_wait_seconds:0.4,deadline_misses:0,checkpoint_failures:1}]};
async function render(element){await act(async()=>root.render(element));}
async function click(label){const button=[...container.querySelectorAll('button')].find(node=>node.textContent===label);assert.ok(button,label);await act(async()=>button.click());}
beforeEach(()=>{document.body.innerHTML='<div id="reclaim-tests"></div>';container=document.getElementById('reclaim-tests');root=createRoot(container);});
afterEach(async()=>{await act(async()=>root.unmount());globalThis.fetch=originalFetch;});after(()=>dom.window.close());
test('model results stay grouped by owner preference and expose mismatched planner version',async()=>{
 globalThis.fetch=async()=>json(report);await render(<ReclaimValidation api={api()}/>);
 assert.match(container.textContent,/参数模型回放/);assert.match(container.textContent,/历史策略版本/);
 assert.equal(container.querySelectorAll('table').length,2);assert.match(container.textContent,/尽量保留计算/);assert.match(container.textContent,/尽快归还/);
 const first=container.querySelector('tbody').textContent;assert.match(first,/3\.25/);assert.match(first,/1\.50/);
 assert.doesNotMatch(container.textContent,/真实 CPU 验证通过|验收通过/);
});
test('not-run evidence offers no numerical result or report download',async()=>{
 globalThis.fetch=async()=>json({status:'NOT_RUN',summary:[]});await render(<ReclaimValidation api={api()}/>);
 assert.match(container.textContent,/暂无对照结果/);assert.equal(container.querySelector('table'),null);assert.doesNotMatch(container.textContent,/下载对照报告/);
});
test('report download preserves raw bytes and discards a response after identity replacement',async()=>{
 const raw='{ "wide_id":18446744073709551615, "deadline_misses":1 }\n',downloads=[];
 const create=URL.createObjectURL,revoke=URL.revokeObjectURL,anchorClick=dom.window.HTMLAnchorElement.prototype.click;
 URL.createObjectURL=blob=>{downloads.push(blob);return 'blob:reclaim-test';};URL.revokeObjectURL=()=>{};dom.window.HTMLAnchorElement.prototype.click=function(){};
 try{
  globalThis.fetch=async path=>path.includes('/artifacts/')?new Response(raw):json(report);
  await render(<ReclaimValidation api={api()}/>);await click('下载对照报告');assert.equal(await downloads[0].text(),raw);
  let finish,signal;globalThis.fetch=async(path,options)=>path.includes('/artifacts/')?new Promise(resolve=>{finish=resolve;signal=options.signal;}):json({summary:[]});
  await click('下载实验协议');await render(<ReclaimValidation api={new ApiClient({owner:'alice',token:'synthetic-alice'})}/>);
  assert.equal(signal.aborted,true);await act(async()=>finish(new Response(raw)));assert.equal(downloads.length,1);
 }finally{URL.createObjectURL=create;URL.revokeObjectURL=revoke;dom.window.HTMLAnchorElement.prototype.click=anchorClick;}
});
