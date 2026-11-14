import {useEffect,useRef,useState,type FormEvent} from 'react';
import {ApiClient,ApiError,detail,encodeId,items,list,object,text,type ApiResult} from '../api';
import {useLookupQuery,useQuery} from '../hooks';
import {downloadJson,downloadJsonText,ErrorNotice,Field,Panel,QueryFeedback,RawData,Receipt,Refresh,Status,Table} from './common';

const artifactNames:Record<string,string>={input:'输入',summary:'汇总',events:'活动',decisions:'决策',receipts:'回执','final-snapshot':'最终快照','run-metadata':'运行信息'};
export function Experiments({api}:{api:ApiClient}) {
  const {input,setInput,id,query,submit,open}=useLookupQuery(api,'/x/v1/experiments');
  const catalog=useQuery(api,'/x/v1/experiments/catalog'),history=useQuery(api,'/x/v1/experiments?limit=25');
  const [scenarioId,setScenarioId]=useState('');
  const scenarios=items(catalog.data),scenario=scenarios.find(s=>s.scenario_id===scenarioId)??scenarios[0];
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null),[result,setResult]=useState<ApiResult|null>(null);
  const inFlight=useRef(false),run=detail(query.data);
  async function start(event:FormEvent<HTMLFormElement>){
    event.preventDefault();if(!scenario||inFlight.current||error?.uncertain)return;
    const form=new FormData(event.currentTarget);inFlight.current=true;setBusy(true);setError(null);setResult(null);
    try{const response=await api.request('/x/v1/experiments',{method:'POST',body:{scenario_id:scenario.scenario_id,policy:String(form.get('policy')),seed:Number(form.get('seed'))}});setResult(response);const runId=text(detail(response.data).run_id,'');if(runId)open(runId);history.refresh();}
    catch(error){setError(error instanceof ApiError?error:new ApiError('实验提交结果待确认',0));}
    finally{inFlight.current=false;setBusy(false);}
  }
  return <>
    <Panel title="新实验" actions={<Refresh loading={catalog.loading||history.loading} onClick={()=>{catalog.refresh();history.refresh();}}/>}>
      <QueryFeedback query={catalog}>
        {scenarios.length?<form onSubmit={start}><fieldset className="form-grid" disabled={busy||Boolean(error?.uncertain)}><Field label="场景"><select value={text(scenario?.scenario_id,'')} onChange={e=>setScenarioId(e.target.value)}>{scenarios.map(s=><option key={text(s.scenario_id)} value={text(s.scenario_id)}>{text(s.label)}</option>)}</select></Field><Field label="策略"><select name="policy">{Array.isArray(scenario?.policies)&&scenario.policies.map(policy=><option key={String(policy)} value={String(policy)}>{String(policy)}</option>)}</select></Field><Field label="种子"><input name="seed" type="number" defaultValue="0" min={Number(scenario?.seed_min??0)} max={Number(scenario?.seed_max??9999)} required/></Field></fieldset><p>{text(scenario?.work_count)} 项工作 · {text(scenario?.horizon)} 秒 · 逻辑模拟</p><button className="button primary" disabled={busy||!scenario||Boolean(error?.uncertain)}>{busy?'正在提交…':'运行实验'}</button></form>:<p className="empty">暂无可运行场景</p>}
      </QueryFeedback>
      {error&&<ErrorNotice error={error}/>} {error?.uncertain&&<p role="status">请刷新实验列表确认提交结果。</p>}{result&&!id&&<Receipt result={result}/>}
    </Panel>
    <Panel title="实验记录">
      <form className="inline-form" onSubmit={submit}><Field label="Run ID"><input value={input} onChange={e=>setInput(e.target.value)} required placeholder="run-…"/></Field><button className="button" disabled={query.loading}>{query.loading?'正在查询…':'查询实验'}</button></form>
      <QueryFeedback query={history}>{items(history.data).length?<div className="record-list">{items(history.data).map(item=><div className="record" key={text(item.run_id)}><div className="record-title"><button className="link-button" onClick={()=>open(text(item.run_id))}>{text(item.run_id)}</button><Status value={item.status}/></div></div>)}</div>:<p className="empty">暂无实验记录</p>}</QueryFeedback>
    </Panel>
    <QueryFeedback query={query}><Panel title={`实验 ${id}`} actions={<div className="panel-actions"><Status value={run.status}/><Refresh loading={query.loading} onClick={query.refresh}/><button type="button" className="button" onClick={()=>downloadJson(query.data,`experiment-${encodeId(id)}.json`)}>导出记录</button></div>}>
      <dl className="facts"><div><dt>策略</dt><dd>{text(object(run.policy).name)}</dd></div><div><dt>种子</dt><dd>{text(run.seed)}</dd></div><div><dt>实验窗口</dt><dd>{text(object(run.window).start)}—{text(object(run.window).end)} s</dd></div></dl>
      {list(run.checks).length>0&&<Table rows={list(run.checks)} rowKey={(r,i)=>`${text(r.target_id)}-${i}`} columns={[{label:'项目',render:r=>text(r.target_id)},{label:'状态',render:r=><Status value={r.status}/>},{label:'结果',render:r=>text(r.actual,'未运行')}]}/>}
      <div className="actions">{[...list(run.inputs),...list(run.outputs)].filter(output=>Object.hasOwn(artifactNames,text(output.name))).map(output=><ExperimentDownload key={text(output.name)} api={api} runId={id} artifactId={text(output.name)}/>)}</div>
      <RawData data={query.data} label="技术详情"/>
    </Panel></QueryFeedback>
  </>;
}
function ExperimentDownload({api,runId,artifactId}:{api:ApiClient;runId:string;artifactId:string}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null);
  const pending=useRef<AbortController|null>(null);
  const current=useRef({api,runId,artifactId});
  current.current={api,runId,artifactId};
  useEffect(()=>{
    setBusy(false);setError(null);
    return ()=>{pending.current?.abort();pending.current=null;};
  },[api,runId,artifactId]);
  async function download(){
    pending.current?.abort();
    const controller=new AbortController();
    pending.current=controller;setBusy(true);setError(null);
    const isCurrent=()=>pending.current===controller&&!controller.signal.aborted
      &&current.current.api===api&&current.current.runId===runId&&current.current.artifactId===artifactId;
    try{
      const response=await api.request(`/x/v1/experiments/${encodeId(runId)}/artifacts/${encodeId(artifactId)}`,{signal:controller.signal});
      if(isCurrent())downloadJsonText(response.raw,`${encodeId(runId)}-${artifactId}.json`);
    }catch(error){if(isCurrent())setError(error instanceof ApiError?error:new ApiError('实验工件读取失败',0));}
    finally{if(isCurrent()){pending.current=null;setBusy(false);}}
  }
  return <div className="action-unit"><button type="button" className="button" disabled={busy} onClick={download}>{busy?'正在读取…':`下载${artifactNames[artifactId]}`}</button>{error&&<ErrorNotice error={error}/>}</div>;
}
