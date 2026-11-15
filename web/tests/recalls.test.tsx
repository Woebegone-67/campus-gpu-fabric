import assert from 'node:assert/strict';
import {after,afterEach,beforeEach,test} from 'node:test';
import {JSDOM} from 'jsdom';
const dom=new JSDOM('<!doctype html><html><body></body></html>',{url:'http://localhost/'});
for(const name of ['window','document','navigator','HTMLElement','Event','MouseEvent'])Object.defineProperty(globalThis,name,{value:dom.window[name],configurable:true,writable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');const {act}=React;const {createRoot}=await import('react-dom/client');
const {RecallContext,RecallProvider,RecallBanner,WorkRecall,OwnedRecalls,ReclaimAudit,remainingRecallSeconds}=await import('../src/components/Recalls.tsx');
const {ApiClient}=await import('../src/api.ts');
let root,container;
const originalFetch=globalThis.fetch;
const saving={allocation_id:'lease',intent_id:'work-one',display_name:'矩阵计算',state:'saving',checkpoint_due:18,deadline:25,checkpoint_verified:false};
function state(extra={}){return {query:{data:{},error:null,loading:false,receivedAt:1000,refresh:()=>{}},borrowed:[saving],owned:[],now:10,mode:'cpu_reference',elapsed:2,fresh:true,...extra};}
async function render(element){await act(async()=>root.render(element));}
beforeEach(()=>{document.body.innerHTML='<div id="recall-tests"></div>';container=document.getElementById('recall-tests');root=createRoot(container);});
afterEach(async()=>{await act(async()=>root.unmount());globalThis.fetch=originalFetch;});after(()=>dom.window.close());

test('CPU saving countdown follows checkpoint_due; simulation does not advance with wall time',()=>{
 assert.equal(remainingRecallSeconds(18,10,'cpu_reference',2),6);
 assert.equal(remainingRecallSeconds(18,10,'logical_simulation',2),8);
 assert.equal(remainingRecallSeconds(18,10,'cpu_reference',20),0);
 assert.equal(remainingRecallSeconds(null,10,'cpu_reference',2),null);
});
test('global reminder links the affected work and deadline zero never claims returned',async()=>{
 await render(<RecallContext.Provider value={state()}><RecallBanner/></RecallContext.Provider>);
 assert.match(container.textContent,/矩阵计算.*设备正在收回.*保存剩余 6 秒/s);
 assert.equal(container.querySelector('a').getAttribute('href'),'#continuity?intent=work-one');
 await render(<RecallContext.Provider value={state({elapsed:9})}><WorkRecall intentId="work-one"/></RecallContext.Provider>);
 assert.match(container.textContent,/正在确认停止/);assert.doesNotMatch(container.textContent,/设备已归还/);
});
test('stale reads stop countdown and never upgrade a remembered phase',async()=>{
 await render(<RecallContext.Provider value={state({fresh:false,elapsed:8})}><WorkRecall intentId="work-one"/></RecallContext.Provider>);
 assert.match(container.textContent,/状态待更新/);assert.doesNotMatch(container.textContent,/保存剩余|设备已归还|到时自动停止/);
});
test('current detail avoids duplicate global reminder and other work still alerts',async()=>{
 await render(<RecallContext.Provider value={state()}><RecallBanner currentIntent="work-one"/></RecallContext.Provider>);
 assert.equal(container.textContent,'');
 await render(<RecallContext.Provider value={state({borrowed:[{...saving,intent_id:'work-two',allocation_id:'lease-two'}]})}><RecallBanner currentIntent="work-one"/></RecallContext.Provider>);
 assert.match(container.textContent,/设备正在收回/);
});
test('owner phases use server facts and only matching device notice',async()=>{
 const notice={notice_id:'notice-1',device_ids:['gpu-one'],deadline:25,unavailable_until:45,state:'stopping',held_count:1};
 await render(<RecallContext.Provider value={state({owned:[notice]})}><OwnedRecalls deviceIds={['gpu-one']}/></RecallContext.Provider>);
 assert.match(container.textContent,/正在确认停止.*等待 1 项任务停止/s);assert.doesNotMatch(container.textContent,/已收回/);
 await render(<RecallContext.Provider value={state({owned:[{...notice,state:'ready',held_count:0}]})}><OwnedRecalls deviceIds={['gpu-one']}/></RecallContext.Provider>);
 assert.match(container.textContent,/已收回 · 自用中/);
 await render(<RecallContext.Provider value={state({owned:[notice]})}><OwnedRecalls deviceIds={['someone-elses-gpu']}/></RecallContext.Provider>);
 assert.equal(container.textContent,'');
});
test('shared provider reads authenticated recall projection once for banner and detail',async()=>{
 const calls=[];globalThis.fetch=async(path)=>{calls.push(path);return new Response(JSON.stringify({version:{epoch:0,revision:1},now:10,execution_mode:'logical_simulation',borrowed:[saving],owned:[]}));};
 const api=new ApiClient({owner:'alice',token:'test-only'});
 await render(<RecallProvider api={api}><RecallBanner/><WorkRecall intentId="work-one"/></RecallProvider>);
 assert.deepEqual(calls,['/x/v1/recalls']);assert.match(container.textContent,/保存剩余 8 逻辑秒/);
});
test('adaptive plans distinguish continuing calculation from actual checkpoint saving',async()=>{
 const decision={action:'finish_first',phase:'planned',reason:'finish_fits',dispatch_at:16,stop_at:17};
 await render(<RecallContext.Provider value={state({borrowed:[{...saving,decision}]})}><WorkRecall intentId="work-one"/></RecallContext.Provider>);
 assert.match(container.textContent,/最多继续计算 4 秒/);assert.match(container.textContent,/优先完成 · 预计能在收回前完成/);
 assert.doesNotMatch(container.textContent,/保存剩余|进度已保存|设备已归还/);
 await render(<RecallContext.Provider value={state({borrowed:[{...saving,decision:{...decision,action:'save_at',reason:'delay_valuable'}}]})}><WorkRecall intentId="work-one"/></RecallContext.Provider>);
 assert.match(container.textContent,/开始保存前 4 秒/);assert.doesNotMatch(container.textContent,/保存剩余/);
 await render(<RecallContext.Provider value={state({borrowed:[{...saving,decision:{...decision,phase:'saving'}}]})}><WorkRecall intentId="work-one"/></RecallContext.Provider>);
 assert.match(container.textContent,/保存剩余 5 秒/);assert.doesNotMatch(container.textContent,/最多继续计算/);
});
test('elapsed planned dispatch is a status check, never a fabricated stop or saved result',async()=>{
 const row={...saving,decision:{action:'save_at',phase:'planned',reason:'delay_valuable',dispatch_at:11,stop_at:17}};
 await render(<RecallContext.Provider value={state({borrowed:[row]})}><RecallBanner/></RecallContext.Provider>);
 assert.match(container.textContent,/正在更新收回进度/);assert.doesNotMatch(container.textContent,/正在确认停止|已保存|已归还/);
 await render(<RecallContext.Provider value={state({borrowed:[row],fresh:false})}><WorkRecall intentId="work-one"/></RecallContext.Provider>);
 assert.match(container.textContent,/状态待更新/);assert.doesNotMatch(container.textContent,/稍后保存|现在保存|多算一段/);
});
test('owner sees waiting plan until the server confirms checkpoint saving or device returned',async()=>{
 const notice={notice_id:'notice-1',device_ids:['gpu-one'],deadline:25,state:'saving',held_count:1,decision:{action:'finish_first',phase:'planned',reason:'finish_fits'}};
 await render(<RecallContext.Provider value={state({owned:[notice]})}><OwnedRecalls deviceIds={['gpu-one']}/></RecallContext.Provider>);
 assert.match(container.textContent,/等待计算完成.*收回剩余 13 秒/s);assert.doesNotMatch(container.textContent,/正在保存|已收回/);
 await render(<RecallContext.Provider value={state({owned:[{...notice,decision:{...notice.decision,phase:'saving'}}]})}><OwnedRecalls deviceIds={['gpu-one']}/></RecallContext.Provider>);
 assert.match(container.textContent,/正在保存/);assert.doesNotMatch(container.textContent,/已收回/);
});
test('saving and stopping show current progress without a superseded planned decision',async()=>{
 const decision={action:'save_at',phase:'saving',reason:'delay_valuable',dispatch_at:11,stop_at:17};
 for(const status of ['saving','stopping']){
  const borrowed={...saving,state:status,decision:{...decision,phase:status}};
  const owned={notice_id:'notice-1',device_ids:['gpu-one'],deadline:25,held_count:1,state:status,decision:{...decision,phase:status}};
  await render(<RecallContext.Provider value={state({borrowed:[borrowed],owned:[owned]})}><WorkRecall intentId="work-one"/><OwnedRecalls deviceIds={['gpu-one']}/></RecallContext.Provider>);
  assert.equal(container.querySelector('.recall-decision'),null);
  assert.doesNotMatch(container.textContent,/稍后保存|多算一段后再保存/);
  assert.match(container.textContent,status==='saving'?/保存剩余 5 秒.*正在保存/s:/正在确认停止/);
 }
});
test('completed reclaim audit loads only when opened inside visible technical details',async()=>{
 const calls=[],plan={action:'finish_first',phase:'complete',inputs:{rate_lower:10},candidates:[{action:'save_now',score:2}],stop_confirmed_at:15};
 globalThis.fetch=async path=>{calls.push(path);return new Response(JSON.stringify({items:[plan]}));};
 const api=new ApiClient({owner:'alice',token:'test-only'});
 await render(<ReclaimAudit api={api} intentId="work-one" visible/>);assert.equal(calls.length,0);
 const disclosure=container.querySelector('details');await act(async()=>{disclosure.open=true;disclosure.dispatchEvent(new dom.window.Event('toggle'));});
 assert.deepEqual(calls,['/x/v1/work-intents/work-one/reclaim-decisions']);assert.deepEqual(JSON.parse(container.querySelector('pre').textContent),{items:[plan]});
 await render(<ReclaimAudit api={api} intentId="work-one" visible={false}/>);assert.equal(container.querySelector('pre'),null);
});

test('reclaim explanation renders recorded choice, reason, estimates and actual completion separately',async()=>{
 const plan={allocation_id:'lease',attempt_id:'attempt',action:'finish_first',reason:'finish_fits',phase:'complete',return_policy:'return_first',planned_at:109.05059825,
  inputs:{now:109.05059825,owner_deadline:112.05059825,progress_steps:1000000,total_steps:1000000,checkpoint_cost_upper:.75840425,stop_cost_upper:2},completed_at:109.149306417,stop_confirmed_at:109.149306417};
 globalThis.fetch=async()=>new Response(JSON.stringify({items:[plan]}));
 await render(<ReclaimAudit api={new ApiClient({owner:'alice',token:'test-only'})} intentId="work-one" visible/>);
 const disclosure=container.querySelector('details');await act(async()=>{disclosure.open=true;disclosure.dispatchEvent(new dom.window.Event('toggle'));});
 const card=container.querySelector('[aria-label="收回选择与理由"]');
 assert.match(card.textContent,/本次选择：优先完成/);assert.match(card.textContent,/选择理由：根据当时的进度与速度估计/);
 assert.match(card.textContent,/决策时可用通知窗口3 秒/);assert.match(card.textContent,/决策时观测进度100%/);
 assert.match(card.textContent,/保存耗时保守估计0.76 秒/);assert.match(card.textContent,/贡献者偏好尽快归还/);
 assert.match(card.textContent,/任务完成已确认 · 逻辑时间 109.15 秒/);
 assert.deepEqual(JSON.parse(container.querySelector('pre').textContent),{items:[plan]});
 assert.equal(container.querySelector('details details').open,false);
});

test('short window explanation never invents failed saving or completed work; unknown fields remain unknown',async()=>{
 const plans=[
  {allocation_id:'lease',action:'stop',reason:'insufficient_window',phase:'complete',inputs:{now:0,owner_deadline:3,checkpoint_cost_upper:2.13,stop_cost_upper:2},stop_confirmed_at:.23},
  {allocation_id:'legacy',action:'new_action',reason:'new_reason',inputs:{}},
  {allocation_id:'waiting',action:'save_now',reason:'cold_observation',phase:'planned',inputs:{now:0,owner_deadline:0,progress_steps:0,total_steps:100}},
 ];
 globalThis.fetch=async()=>new Response(JSON.stringify({items:plans}));
 await render(<ReclaimAudit api={new ApiClient({owner:'alice',token:'test-only'})} intentId="work-one" visible/>);
 const disclosure=container.querySelector('details');await act(async()=>{disclosure.open=true;disclosure.dispatchEvent(new dom.window.Event('toggle'));});
 const cards=container.querySelectorAll('[aria-label="收回选择与理由"]');
 assert.match(cards[0].textContent,/本次选择：直接停止/);assert.match(cards[0].textContent,/不表示已经尝试保存并失败/);
 assert.match(cards[0].textContent,/执行停止／结束已确认/);assert.doesNotMatch(cards[0].textContent,/任务完成已确认/);
 assert.match(cards[1].textContent,/未知动作（new_action）/);assert.match(cards[1].textContent,/原因代码：new_reason/);
 assert.match(cards[1].textContent,/保存耗时保守估计未记录/);assert.match(cards[1].textContent,/尚未记录任务完成或停止确认/);
 assert.match(cards[2].textContent,/缺少足够新鲜、可信/);assert.match(cards[2].textContent,/通知窗口0 秒/);assert.match(cards[2].textContent,/观测进度0%/);
 assert.doesNotMatch(cards[2].textContent,/任务完成已确认|执行停止／结束已确认/);
});
