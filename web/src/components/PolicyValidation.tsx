import {useEffect,useRef,useState} from 'react';
import {ApiClient,ApiError,encodeId,list,object,text} from '../api';
import {useQuery} from '../hooks';
import {downloadJsonText,ErrorNotice,Field,Panel,QueryFeedback,RawData,Refresh,Status,Table} from './common';

const names:Record<string,string>={manifest:'冻结协议',analysis:'配对分析报告',summary:'工作结果',input:'冻结输入',events:'提交事件',decisions:'决策记录',receipts:'提交回执','final-snapshot':'最终快照','run-metadata':'运行元数据',commands:'命令记录','resource-samples':'资源采样','external-injections':'外部事件执行记录','final-decision-snapshot':'最终决策快照'};
const ratio=(v:unknown)=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(4):'未定义';

export function PolicyValidation({api}:{api:ApiClient}) {
  const [enabled,setEnabled]=useState(api.identity.profile==='admin');
  const query=useQuery(api,enabled?'/x/v1/policy-validation':null);
  const data=object(query.data),analysis=object(data.analysis),comparison=object(object(analysis.comparisons).B2);
  const aggregate=object(comparison.aggregate),runs=list(data.runs);
  const [family,setFamily]=useState(''),[policy,setPolicy]=useState(''),[onlyFailures,setOnlyFailures]=useState(false);
  const shown=runs.filter(r=>(!family||r.scenario_family===family)&&(!policy||r.policy===policy)&&(!onlyFailures||r.status!=='complete'));
  return <Panel title="CP3 策略配对验证" description="读取冻结输入及 B0、B1、B2、P 和机会分量消融的独立实验产物；证据用途与执行范围在下方列出。" actions={enabled?<Refresh loading={query.loading} onClick={query.refresh}/>:undefined}>
    {!enabled&&<button className="button" onClick={()=>setEnabled(true)}>管理员 · 读取 CP3 报告</button>}
    <QueryFeedback query={query}>
      {data.partition==='development'&&<p className="helper">开发回归结果，仅用于功能检查；不构成正式策略效果验证。未运行项保留，不计为通过。</p>}
      {data.historical===true&&<p className="helper">历史实验报告：保留原实验构建，仅供查阅与下载，不能作为当前构建验收证据。本次读取核验封存协议、索引与分析报告；每个工件在下载时核验原始字节，未在本次请求中重新审核全部工件。</p>}
      {data.partition==='validation'&&<p className="helper">{data.historical===true?'原实验的正式验证集结果。':'正式验证集结果。'}是否完整执行、达到目标和满足场景覆盖分别核验。</p>}
      {data.build!=null&&<RawData data={{build:data.build,serving_build:data.serving_build,historical:data.historical,current_build_acceptance_eligible:data.current_build_acceptance_eligible,integrity_scope:data.integrity_scope,partition:data.partition,manifest_sha256:data.manifest_sha256}} label="证据版本与用途"/>}
      {data.status==='NOT_RUN'&&<p className="helper">尚无完整分析。先在独立实验目录执行冻结、运行与分析；研发样例不会自动成为 CP3 证据。</p>}
      {data.analysis!=null&&<>
        <dl className="facts">
          <div><dt>{data.partition==='development'?'已运行子集 · P / B2 面积比 R':'P / B2 · 族等权需求面积比 R'}</dt><dd>{ratio(aggregate.macro_area_ratio)}{data.partition==='development'?' · 仅作诊断':' · 目标 ≤ 0.85'}</dd></div>
          <div><dt>{data.historical===true?'原实验模型的效果目标':'当前模型的效果目标'}</dt><dd>{data.partition==='development'?'开发回归不作正式目标判定':<Status value={analysis.target_met===true?'MET':'NOT_MET'}/>}</dd></div>
          <div><dt>{data.historical===true?'原运行审核结果':'实验工件完整性'}</dt><dd>{analysis.evidence_complete===true?(data.historical===true?'原审核记录完整':'全部工件已核验'):'未齐全'}</dd></div>
          <div><dt>蓝图场景覆盖</dt><dd>{analysis.coverage_complete===true?'已覆盖':'存在未覆盖项'}</dd></div>
        </dl>
        <p className="helper">效果目标同时检查每组完成数和个体、Owner 损害。工件完整、效果达标、联合 CP3 验收是分别核验的结论。模拟结果不代表真实 GPU 性能。</p>
        <Table rows={list(aggregate.families)} rowKey={r=>text(r.scenario_family)} columns={[
          {label:'场景族',render:r=>text(r.scenario_family)},
          {label:'配对数',render:r=>text(r.pair_count)},
          {label:'平均 P / B2',render:r=>ratio(r.mean_area_ratio)},
          {label:'最差 P / B2',render:r=>ratio(r.worst_area_ratio)},
        ]}/>
        <RawData data={{limitations:analysis.limitations,coverage:analysis.coverage,run_failures:analysis.run_failures}} label="覆盖缺口与运行失败"/>
        <RawData data={comparison.target} label="完成数、个体损害与 Owner 损害判据"/>
        <RawData data={analysis.instrumentation} label="机会分量覆盖、动作变化与决策耗时"/>
      </>}
      {Array.isArray(data.artifact_ids)&&<div className="actions">{data.artifact_ids.map(id=><ValidationDownload key={String(id)} api={api} id={String(id)}/>)}</div>}
      {runs.length>0&&<>
        <p className="helper">登记 {runs.length} / {text(data.expected_runs)} 次运行；完成 {runs.filter(r=>r.status==='complete').length} 次。输入和全部结果保留，包括失败、未完成与负结果。</p>
        <div className="form-grid">
          <Field label="筛选场景族"><select value={family} onChange={e=>setFamily(e.target.value)}><option value="">全部场景</option>{[...new Set(runs.map(r=>text(r.scenario_family)))].map(v=><option key={v}>{v}</option>)}</select></Field>
          <Field label="筛选策略"><select value={policy} onChange={e=>setPolicy(e.target.value)}><option value="">全部策略</option>{[...new Set(runs.map(r=>text(r.policy)))].map(v=><option key={v}>{v}</option>)}</select></Field>
          <label><input type="checkbox" checked={onlyFailures} onChange={e=>setOnlyFailures(e.target.checked)}/> 只看失败或未运行</label>
        </div>
        <p className="helper">当前显示 {shown.length} 次运行。配对分析报告包含每个工作与 Owner 的差值。</p>
        <Table rows={shown} rowKey={r=>text(r.run_id)} columns={[
          {label:'场景 / 种子 / 策略',render:r=><>{text(r.scenario_family)} · {text(r.seed)} · {text(r.policy)}</>},
          {label:'执行状态',render:r=>text(r.status)},
          {label:'真实记录',render:r=><details><summary>查看下载项</summary>{Array.isArray(r.artifact_ids)&&r.artifact_ids.map(id=><ValidationDownload key={String(id)} api={api} id={String(id)}/>)}</details>},
        ]}/>
      </>}
    </QueryFeedback>
  </Panel>;
}

function ValidationDownload({api,id}:{api:ApiClient;id:string}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null);
  const pending=useRef<AbortController|null>(null);
  const current=useRef({api,id});
  current.current={api,id};
  useEffect(()=>{
    setBusy(false);setError(null);
    return ()=>{pending.current?.abort();pending.current=null;};
  },[api,id]);
  async function download(){
    pending.current?.abort();
    const controller=new AbortController();
    pending.current=controller;setBusy(true);setError(null);
    // A response already in flight can outlive identity changes or navigation.
    // Cancellation alone is insufficient if the transport has already resolved.
    const isCurrent=()=>pending.current===controller&&!controller.signal.aborted
      &&current.current.api===api&&current.current.id===id;
    try{
      const response=await api.request(`/x/v1/policy-validation/artifacts/${encodeId(id)}`,{signal:controller.signal});
      if(isCurrent())downloadJsonText(response.raw,`cp3-${id.replace(/:/g,'-')}.json`);
    }catch(e){
      if(isCurrent())setError(e instanceof ApiError?e:new ApiError('CP3 工件读取失败',0));
    }finally{
      if(isCurrent()){pending.current=null;setBusy(false);}
    }
  }
  return <div className="action-unit"><button type="button" className="button" disabled={busy} onClick={download}>{busy?'正在核验…':`下载${names[id.split(':').at(-1)!]??id}`}</button>{error&&<ErrorNotice error={error}/>}</div>;
}
