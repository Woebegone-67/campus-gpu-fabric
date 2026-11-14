import {useEffect,useRef,useState} from 'react';
import {ApiClient,ApiError,encodeId,list,object,text} from '../api';
import {useQuery} from '../hooks';
import {downloadJsonText,ErrorNotice,Field,Panel,QueryFeedback,RawData,Refresh,Status,Table} from './common';

const names:Record<string,string>={manifest:'冻结协议',analysis:'配对分析报告',summary:'工作结果',input:'冻结输入',events:'活动',decisions:'决策记录',receipts:'提交回执','final-snapshot':'最终快照','run-metadata':'运行信息',commands:'命令记录','resource-samples':'资源采样','external-injections':'外部事件','final-decision-snapshot':'最终决策快照'};
const ratio=(v:unknown)=>typeof v==='number'&&Number.isFinite(v)?v.toFixed(4):'—';
export function PolicyValidation({api}:{api:ApiClient}) {
  const query=useQuery(api,'/x/v1/policy-validation');
  const data=object(query.data),analysis=object(data.analysis),comparison=object(object(analysis.comparisons).B2);
  const aggregate=object(comparison.aggregate),runs=list(data.runs);
  const [family,setFamily]=useState(''),[policy,setPolicy]=useState(''),[onlyFailures,setOnlyFailures]=useState(false);
  const shown=runs.filter(r=>(!family||r.scenario_family===family)&&(!policy||r.policy===policy)&&(!onlyFailures||r.status!=='complete'));
  return <Panel title="策略评估报告" actions={<Refresh loading={query.loading} onClick={query.refresh}/>}>
    <QueryFeedback query={query}>
      <div className="actions">{data.historical===true&&<span className="status">历史报告 · 不用于当前验收</span>}{data.partition==='development'&&<span className="status">开发回归</span>}{data.partition==='validation'&&<span className="status">验证集</span>}</div>
      {data.analysis==null?<p className="empty">暂无分析报告</p>:<>
        <dl className="facts">
          <div><dt>需求面积比 P / B2</dt><dd>{ratio(aggregate.macro_area_ratio)}</dd></div>
          <div><dt>{data.historical===true?'原实验目标':'效果目标'} · ≤ 0.85</dt><dd>{data.partition==='development'?'不作正式判定':<Status value={analysis.target_met===true?'MET':analysis.target_met===false?'NOT_MET':'NOT_RUN'}/>}</dd></div>
          <div><dt>{data.historical===true?'原运行记录':'运行记录'}</dt><dd>{analysis.evidence_complete===true?'完整':'未齐全'}</dd></div>
          <div><dt>场景覆盖</dt><dd>{analysis.coverage_complete===true?'已覆盖':'存在缺口'}</dd></div>
        </dl>
        {list(aggregate.families).length>0&&<Table rows={list(aggregate.families)} rowKey={r=>text(r.scenario_family)} columns={[
          {label:'场景',render:r=>text(r.scenario_family)},
          {label:'配对数',render:r=>text(r.pair_count)},
          {label:'平均 P / B2',render:r=>ratio(r.mean_area_ratio)},
          {label:'最差 P / B2',render:r=>ratio(r.worst_area_ratio)},
        ]}/>}
      </>}
      {Array.isArray(data.artifact_ids)&&<div className="actions">{data.artifact_ids.map(id=><ValidationDownload key={String(id)} api={api} id={String(id)}/>)}</div>}
      {runs.length>0&&<>
        <p>已完成 {runs.filter(r=>r.status==='complete').length} / {text(data.expected_runs,runs.length.toString())} 次运行</p>
        <div className="form-grid">
          <Field label="场景"><select value={family} onChange={e=>setFamily(e.target.value)}><option value="">全部场景</option>{[...new Set(runs.map(r=>text(r.scenario_family)))].map(v=><option key={v}>{v}</option>)}</select></Field>
          <Field label="策略"><select value={policy} onChange={e=>setPolicy(e.target.value)}><option value="">全部策略</option>{[...new Set(runs.map(r=>text(r.policy)))].map(v=><option key={v}>{v}</option>)}</select></Field>
          <label><input type="checkbox" checked={onlyFailures} onChange={e=>setOnlyFailures(e.target.checked)}/> 只看失败或未运行</label>
        </div>
        <p>{shown.length} 条记录</p>
        <Table rows={shown} rowKey={r=>text(r.run_id)} columns={[
          {label:'场景 / 种子 / 策略',render:r=><>{text(r.scenario_family)} · {text(r.seed)} · {text(r.policy)}</>},
          {label:'状态',render:r=><Status value={r.status}/>},
          {label:'下载',render:r=>Array.isArray(r.artifact_ids)&&r.artifact_ids.length>0?<details><summary>下载文件</summary>{r.artifact_ids.map(id=><ValidationDownload key={String(id)} api={api} id={String(id)}/>)}</details>:'—'},
        ]}/>
      </>}
      <RawData data={data} label="技术详情"/>
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
      if(isCurrent())setError(e instanceof ApiError?e:new ApiError('文件读取失败',0));
    }finally{
      if(isCurrent()){pending.current=null;setBusy(false);}
    }
  }
  return <div className="action-unit"><button type="button" className="button" disabled={busy} onClick={download}>{busy?'正在核验…':`下载${names[id.split(':').at(-1)!]??id}`}</button>{error&&<ErrorNotice error={error}/>}</div>;
}
