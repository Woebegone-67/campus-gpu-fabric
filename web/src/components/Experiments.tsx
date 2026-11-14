import {useState,type FormEvent} from 'react';
import {ApiClient,ApiError,detail,encodeId,items,list,object,text,type ApiResult} from '../api';
import {useLookupQuery,useQuery} from '../hooks';
import {downloadJson,downloadJsonText,ErrorNotice,Field,LookupStatus,Metadata,Panel,QueryFeedback,RawData,Receipt,Refresh,Status,Table} from './common';
import {PolicyValidation} from './PolicyValidation';

const artifactNames:Record<string,string>={input:'固定输入',summary:'汇总',events:'事件',decisions:'决策',receipts:'提交回执','final-snapshot':'最终快照','run-metadata':'运行元数据'};
export function Experiments({api}:{api:ApiClient}) {
  const {input,setInput,id,query,submit,open}=useLookupQuery(api,'/x/v1/experiments');
  const [catalogOpen,setCatalogOpen]=useState(api.identity.profile==='admin');
  const catalog=useQuery(api,catalogOpen?'/x/v1/experiments/catalog':null);
  const history=useQuery(api,catalogOpen?'/x/v1/experiments?limit=25':null);
  const [scenarioId,setScenarioId]=useState('');
  const scenarios=items(catalog.data),scenario=scenarios.find(s=>s.scenario_id===scenarioId)??scenarios[0];
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null),[result,setResult]=useState<ApiResult|null>(null);
  const run=detail(query.data);
  async function start(event:FormEvent<HTMLFormElement>){
    event.preventDefault();if(!scenario)return;
    const form=new FormData(event.currentTarget);setBusy(true);setError(null);setResult(null);
    try{const response=await api.request('/x/v1/experiments',{method:'POST',body:{scenario_id:scenario.scenario_id,policy:String(form.get('policy')),seed:Number(form.get('seed'))}});setResult(response);const runId=text(detail(response.data).run_id,'');if(runId)open(runId);history.refresh();}
    catch(error){setError(error instanceof ApiError?error:new ApiError('实验提交结果不明',0));}
    finally{setBusy(false);}
  }
  return <>
    <PolicyValidation api={api}/>
    <Panel title="隔离实验记录" description="实验拥有独立输入、策略、种子与输出，不修改运营实例。读写均由服务端核验管理员权限。">
      <form className="inline-form" onSubmit={submit}><Field label="Run ID"><input value={input} onChange={e=>setInput(e.target.value)} required placeholder="run-…"/></Field><button className="button primary" disabled={query.loading}>{query.loading?'正在查询…':'查询实验'}</button>{id&&<Refresh loading={query.loading} onClick={query.refresh}/>}</form><LookupStatus query={query}/>
      <p className="helper">CP2 研发样例只验证流程与证据，不构成 CP3 正式效果比较，也不代表真实 GPU 性能。</p>
      {!catalogOpen&&<button className="button" onClick={()=>setCatalogOpen(true)}>管理员 · 加载实验目录</button>}
    </Panel>
    {catalogOpen&&<Panel title="运行已登记场景" description="目录、可用策略和输入边界来自服务端。相同场景与种子可复现输入；每次运行保留独立证据。" actions={<Refresh loading={catalog.loading} onClick={()=>{catalog.refresh();history.refresh();}}/>}>
      <QueryFeedback query={catalog} empty={!scenarios.length}><form onSubmit={start}><fieldset className="form-grid" disabled={busy||Boolean(error?.uncertain)}><Field label="研发场景"><select value={text(scenario?.scenario_id,'')} onChange={e=>setScenarioId(e.target.value)}>{scenarios.map(s=><option key={text(s.scenario_id)} value={text(s.scenario_id)}>{text(s.label)}</option>)}</select></Field><Field label="实验策略"><select name="policy">{Array.isArray(scenario?.policies)&&scenario.policies.map(policy=><option key={String(policy)} value={String(policy)}>{String(policy)}</option>)}</select></Field><Field label="实验种子"><input name="seed" type="number" defaultValue="0" min={Number(scenario?.seed_min??0)} max={Number(scenario?.seed_max??9999)} required/></Field></fieldset><p className="helper">{text(scenario?.description,'')} · {text(scenario?.work_count)} 项工作 · 窗口 {text(scenario?.horizon)} 逻辑秒 · 候选上限 {text(scenario?.max_candidates)}。实验使用逻辑模拟。</p><button className="button primary" disabled={busy||!scenario||Boolean(error?.uncertain)}>{busy?'正在登记实验…':'运行隔离实验'}</button></form></QueryFeedback>
      {error&&<ErrorNotice error={error}/>} {error?.uncertain&&<p className="helper">提交结果不明，已阻止再次提交。先刷新实验列表核对是否已有运行记录。</p>}{result&&<Receipt result={result}/>}
      <QueryFeedback query={history}><div className="record-list">{items(history.data).map(item=><div className="record" key={text(item.run_id)}><div className="record-title"><button className="link-button" onClick={()=>open(text(item.run_id))}>{text(item.run_id)}</button><Status value={item.status}/></div></div>)}</div></QueryFeedback>
    </Panel>}
    <QueryFeedback query={query}><Panel title={`实验 ${id}`} actions={<div className="panel-actions"><Status value={run.status}/><Refresh loading={query.loading} onClick={query.refresh}/><button type="button" className="button" onClick={()=>downloadJson(query.data,`experiment-${encodeId(id)}.json`)}>导出实验记录</button></div>}>
      <Metadata data={query.data}/><p className="helper">运行中请刷新读取进度；只有实际生成的工件会出现在下载清单。失败与异常也保留在记录内。</p>
      <dl className="facts"><div><dt>运行类型</dt><dd>{text(run.run_kind)}</dd></div><div><dt>执行模式</dt><dd>{text(object(run.environment).execution_mode, text(run.execution_mode))}</dd></div><div><dt>策略</dt><dd>{text(object(run.policy).name)} · {text(object(run.policy).version)}</dd></div><div><dt>种子 / 窗口</dt><dd>{text(run.seed)} · {text(object(run.window).start)}—{text(object(run.window).end)} s</dd></div></dl>
      {list(run.checks).length>0&&<Table rows={list(run.checks)} rowKey={(r,i)=>`${text(r.target_id)}-${i}`} columns={[{label:'目标',render:r=>text(r.target_id)},{label:'结果',render:r=><Status value={r.status}/>},{label:'实际观察',render:r=>text(r.actual,'尚未运行')}]}/>}
      <div className="actions">{[...list(run.inputs),...list(run.outputs)].filter(output=>Object.hasOwn(artifactNames,text(output.name))).map(output=><ExperimentDownload key={text(output.name)} api={api} runId={id} artifactId={text(output.name)}/>)}</div>
      <RawData data={query.data} label="输入、输出、版本与异常记录"/>
    </Panel></QueryFeedback>
  </>;
}
function ExperimentDownload({api,runId,artifactId}:{api:ApiClient;runId:string;artifactId:string}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null);
  async function download(){setBusy(true);setError(null);try{const response=await api.request(`/x/v1/experiments/${encodeId(runId)}/artifacts/${encodeId(artifactId)}`);downloadJsonText(response.raw,`${encodeId(runId)}-${artifactId}.json`);}catch(error){setError(error instanceof ApiError?error:new ApiError('实验工件读取失败',0));}finally{setBusy(false);}}
  return <div className="action-unit"><button type="button" className="button" disabled={busy} onClick={download}>{busy?'正在读取…':`下载${artifactNames[artifactId]}`}</button>{error&&<ErrorNotice error={error}/>}</div>;
}
