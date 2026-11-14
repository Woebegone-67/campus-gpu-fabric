/** Synthetic DOM regressions, never formal experiment or human evidence. */
import assert from 'node:assert/strict';
import {after,afterEach,beforeEach,test} from 'node:test';
import {JSDOM} from 'jsdom';
const dom=new JSDOM('<html><body></body></html>',{url:'http://localhost/'});
for(const name of ['window','document','navigator','HTMLElement','Event','MouseEvent']) Object.defineProperty(globalThis,name,{value:dom.window[name],configurable:true});
globalThis.IS_REACT_ACT_ENVIRONMENT=true;
const React=await import('react');const {act}=React;
const {createRoot}=await import('react-dom/client');
const {ApiClient}=await import('../src/api.ts');
const {PolicyValidation}=await import('../src/components/PolicyValidation.tsx');
let root,container;const originalFetch=globalThis.fetch;
const api=()=>new ApiClient({owner:'',token:'synthetic-admin',profile:'admin'});
const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}});
const report={status:'ANALYZED',expected_runs:125,analysis:{target_met:false,evidence_complete:true,coverage_complete:false,limitations:['heterogeneous durations unavailable'],comparisons:{B2:{aggregate:{macro_area_ratio:1.05,families:[{scenario_family:'neutral',pair_count:5,mean_area_ratio:1.05,worst_area_ratio:1.2}]},target:{completion_count_not_lower:true}}}},runs:[{run_id:'neutral--101--P',scenario_family:'neutral',seed:101,policy:'P',status:'complete',artifact_ids:[]},{run_id:'fault--101--P',scenario_family:'fault',seed:101,policy:'P',status:'failed',artifact_ids:[]}],artifact_ids:['analysis']};
beforeEach(()=>{document.body.innerHTML='<div id="root"></div>';container=document.getElementById('root');root=createRoot(container);});
afterEach(async()=>{await act(async()=>root.unmount());globalThis.fetch=originalFetch;});after(()=>dom.window.close());
test('negative target and incomplete semantic coverage remain visible despite complete artifacts',async()=>{
  globalThis.fetch=async()=>json(report);
  await act(async()=>root.render(<PolicyValidation api={api()}/>));
  assert.match(container.textContent,/1.0500/);assert.match(container.textContent,/效果目标 · ≤ 0.85未通过/);
  assert.match(container.textContent,/运行记录完整/);assert.match(container.textContent,/存在缺口/);
  assert.match(container.textContent,/2 条记录/);
  const checkbox=container.querySelector('input[type=checkbox]');
  await act(async()=>checkbox.click());
  assert.match(container.textContent,/1 条记录/);
  assert.match(container.textContent,/失败/);
});
test('historical reports disclose original audit scope and never claim current acceptance',async()=>{
  globalThis.fetch=async()=>json({...report,partition:'validation',historical:true,
    build:{git_commit:'original-run'},serving_build:{git_commit:'current-ui'},
    current_build_acceptance_eligible:false,
    integrity_scope:'manifest_index_analysis; artifacts_verified_on_download'});
  await act(async()=>root.render(<PolicyValidation api={api()}/>));
  assert.match(container.textContent,/历史报告/);
  assert.match(container.textContent,/不用于当前验收/);
  assert.match(container.textContent,/原运行记录/);
  assert.match(container.textContent,/原运行记录完整/);
  assert.match(container.textContent,/manifest_index_analysis; artifacts_verified_on_download/);
  assert.match(container.textContent,/original-run/);
  assert.match(container.textContent,/current-ui/);
  assert.doesNotMatch(container.textContent,/全部工件已核验/);
  assert.match(container.textContent,/原实验目标 · ≤ 0.85未通过/);
});
test('a stale report refresh clears the prior score and downloads',async()=>{
  let reads=0;globalThis.fetch=async()=>++reads===1?json(report):json({error:'invalid_evidence',detail:'Build mismatch'},409);
  await act(async()=>root.render(<PolicyValidation api={api()}/>));
  const refresh=[...container.querySelectorAll('button')].find(b=>b.textContent==='刷新');
  await act(async()=>refresh.click());
  assert.match(container.textContent,/HTTP 409/);assert.doesNotMatch(container.textContent,/1.0500/);
  assert.doesNotMatch(container.textContent,/下载配对分析报告/);
});
test('unexecuted suite displays no numerical target or fabricated success',async()=>{
  globalThis.fetch=async()=>json({status:'NOT_RUN',analysis:null,runs:[]});
  await act(async()=>root.render(<PolicyValidation api={api()}/>));
  assert.match(container.textContent,/暂无分析报告/);assert.doesNotMatch(container.textContent,/运行记录完整/);
});

test('partial development runs cannot be presented as a formal target pass',async()=>{
  const runs=Array.from({length:50},(_,i)=>({...report.runs[0],run_id:`development-${i}`,
    status:i<5?'complete':'not_run'}));
  globalThis.fetch=async()=>json({...report,partition:'development',expected_runs:50,
    build:{git_commit:'development-build'},analysis:{...report.analysis,target_met:true,evidence_complete:false},
    runs});
  await act(async()=>root.render(<PolicyValidation api={api()}/>));
  assert.match(container.textContent,/开发回归/);
  assert.match(container.textContent,/不作正式判定/);
  assert.match(container.textContent,/已完成 5 \/ 50 次运行/);
  assert.match(container.textContent,/未齐全/);
  assert.match(container.textContent,/not_run/);
  assert.doesNotMatch(container.textContent,/当前模型的效果目标通过/);
  await act(async()=>container.querySelector('input[type=checkbox]').click());
  assert.match(container.textContent,/45 条记录/);
});

test('switching identity aborts a pending admin download and ignores its late response',async()=>{
  let resolveDownload,downloadSignal;
  const downloads=[];
  const create=URL.createObjectURL,revoke=URL.revokeObjectURL,click=dom.window.HTMLAnchorElement.prototype.click;
  URL.createObjectURL=blob=>{downloads.push(blob);return 'blob:synthetic-cp3';};
  URL.revokeObjectURL=()=>{};
  dom.window.HTMLAnchorElement.prototype.click=function(){};
  try{
    globalThis.fetch=async(path,options)=>{
      if(path.includes('/artifacts/')){
        downloadSignal=options.signal;
        // Deliberately ignore abort, as a response/body may already be arriving.
        return new Promise(resolve=>{resolveDownload=resolve;});
      }
      return options.headers.Authorization==='Bearer synthetic-bob'?json({error:'not_found',detail:'Report not found'},404):json(report);
    };
    await act(async()=>root.render(<div key="admin"><PolicyValidation api={api()}/></div>));
    await act(async()=>[...container.querySelectorAll('button')].find(b=>b.textContent==='下载配对分析报告').click());
    assert.equal(downloadSignal.aborted,false);
    const bob=new ApiClient({owner:'bob',token:'synthetic-bob',profile:'bob'});
    // App keys its workspace by owner and token, unmounting the old identity.
    await act(async()=>root.render(<div key="bob"><PolicyValidation api={bob}/></div>));
    assert.equal(downloadSignal.aborted,true);
    assert.doesNotMatch(container.textContent,/1.0500|下载配对分析报告/);
    await act(async()=>resolveDownload(new Response('{"admin_only_record":18446744073709551615}\n',{status:200})));
    assert.equal(downloads.length,0,'the old identity must not create any download blob');
    assert.doesNotMatch(container.textContent,/admin_only_record|文件读取失败/);
  }finally{
    URL.createObjectURL=create;URL.revokeObjectURL=revoke;
    dom.window.HTMLAnchorElement.prototype.click=click;
  }
});

test('a current admin download preserves raw JSON bytes and its registered filename',async()=>{
  const raw='{ "large_integer":18446744073709551615, "target_met":false }\n';
  const downloads=[];
  const create=URL.createObjectURL,revoke=URL.revokeObjectURL,click=dom.window.HTMLAnchorElement.prototype.click;
  URL.createObjectURL=blob=>{downloads.push({blob});return 'blob:synthetic-cp3';};
  URL.revokeObjectURL=()=>{};
  dom.window.HTMLAnchorElement.prototype.click=function(){downloads.at(-1).filename=this.download;};
  try{
    globalThis.fetch=async path=>path.includes('/artifacts/')?new Response(raw,{status:200}):json(report);
    await act(async()=>root.render(<PolicyValidation api={api()}/>));
    await act(async()=>[...container.querySelectorAll('button')].find(b=>b.textContent==='下载配对分析报告').click());
    assert.equal(downloads.length,1);
    assert.equal(downloads[0].filename,'cp3-analysis.json');
    assert.equal(await downloads[0].blob.text(),raw);
    assert.equal([...container.querySelectorAll('button')].find(b=>b.textContent==='下载配对分析报告').disabled,false);
  }finally{
    URL.createObjectURL=create;URL.revokeObjectURL=revoke;
    dom.window.HTMLAnchorElement.prototype.click=click;
  }
});
