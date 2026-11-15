import {useRef,useState,type FormEvent,type ReactNode} from 'react';
import {ApiClient,ApiError,items,list,object,text,type ApiResult} from '../api';
import {useQuery} from '../hooks';
import {downloadJson,ErrorNotice,Field,Panel,QueryFeedback,RawData,Receipt,Refresh,Status,Table} from '../components/common';
import {Experiments} from '../components/Experiments';
import {PolicyValidation} from '../components/PolicyValidation';
import {ReclaimValidation} from '../components/ReclaimValidation';

export function AcceptancePage({api,showAdminClock=false}:{api:ApiClient;showAdminClock?:boolean}) {
  const version=useQuery(api,'/v1/version'),overview=useQuery(api,'/x/v1/admin/overview');
  const data=object(overview.data),counts=object(data.counts),workStates=object(data.work_by_state);
  const capacity=object(data.capacity),totals=object(capacity.totals);
  const refresh=()=>{version.refresh();overview.refresh();};
  return <div className="work-experience">
    <Panel title="系统状态" actions={<Refresh onClick={refresh} loading={overview.loading||version.loading}/>}>
      <QueryFeedback query={overview}>
        <div className="stat-row">{[['工作','work_intents'],['任务','jobs'],['租约','allocations']].map(([label,key])=><div key={key}><span>{label}</span><strong>{text(counts[key])}</strong></div>)}</div>
        {Object.keys(workStates).length>0&&<div className="actions">{Object.entries(workStates).map(([state,count])=><span key={state}><Status value={state}/> {text(count)}</span>)}</div>}
        <dl className="facts"><div><dt>可用 GPU / 总量</dt><dd>{text(object(totals.available).gpu_count)} / {text(object(totals.capacity).gpu_count)}</dd></div><div><dt>节点</dt><dd>{list(capacity.nodes).length}</dd></div></dl>
        {list(capacity.nodes).length>0&&<Table rows={list(capacity.nodes)} rowKey={r=>text(r.node_id)} columns={[{label:'节点',render:r=>text(r.node_id)},{label:'状态',render:r=><Status value={r.state}/>},{label:'可用 GPU',render:r=>text(object(r.available).gpu_count)}]}/>}
        <RawData data={overview.data} label="技术详情"/>
      </QueryFeedback>
    </Panel>
    <Panel title="管理工具">
      {showAdminClock&&object(version.data).execution_mode==='logical_simulation'&&<Tool title="模拟时钟"><SimulationClock api={api} onDone={refresh}/></Tool>}
      <Tool title="运行实验"><Experiments api={api}/></Tool>
      <Tool title="策略评估报告"><PolicyValidation api={api}/></Tool>
      <Tool title="收回策略对照"><ReclaimValidation api={api}/></Tool>
      <Tool title="验证记录"><AcceptanceRecords api={api}/></Tool>
      <Tool title="服务信息"><QueryFeedback query={version}><dl className="facts"><div><dt>API 版本</dt><dd>{text(object(version.data).api_version)}</dd></div><div><dt>实现版本</dt><dd>{text(object(version.data).implementation_version)}</dd></div></dl><RawData data={version.data} label="技术详情"/></QueryFeedback></Tool>
    </Panel>
  </div>;
}
function Tool({title,children}:{title:string;children:ReactNode}) {
  const [open,setOpen]=useState(false);
  return <details className="work-disclosure" onToggle={event=>setOpen(event.currentTarget.open)}><summary>{title}</summary>{open&&children}</details>;
}
function AcceptanceRecords({api}:{api:ApiClient}) {
  const checks=useQuery(api,'/x/v1/acceptance');
  return <Panel title="验证记录" actions={<Refresh onClick={checks.refresh} loading={checks.loading}/>}>
    <QueryFeedback query={checks}>
      {items(checks.data).length?<><Table rows={items(checks.data)} rowKey={(r,i)=>`${text(r.target_id)}-${i}`} columns={[{label:'项目',render:r=>text(r.target_id)},{label:'状态',render:r=><Status value={r.status}/>},{label:'结果',render:r=>text(r.actual,'未运行')}]}/><button className="button" onClick={()=>downloadJson(checks.data,'acceptance-records.json')}>导出记录</button><RawData data={checks.data} label="技术详情"/></>:<p className="empty">暂无验证记录</p>}
    </QueryFeedback>
  </Panel>;
}
function SimulationClock({api,onDone}:{api:ApiClient;onDone:()=>void}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null),[result,setResult]=useState<ApiResult|null>(null);
  const inFlight=useRef(false);
  async function advance(event:FormEvent<HTMLFormElement>){
    event.preventDefault();if(inFlight.current||error?.uncertain)return;
    const advance_seconds=Number(new FormData(event.currentTarget).get('seconds'));
    if(!window.confirm(`推进 ${advance_seconds} 秒模拟时间？期间到期的任务和租约将结束。`))return;
    inFlight.current=true;setBusy(true);setError(null);setResult(null);
    try{setResult(await api.request('/x/v1/admin/clock',{method:'POST',body:{advance_seconds}}));onDone();}
    catch(error){setError(error instanceof ApiError?error:new ApiError('时间推进结果待确认',0));}
    finally{inFlight.current=false;setBusy(false);}
  }
  return <form onSubmit={advance}><div className="inline-form"><Field label="推进秒数"><input name="seconds" type="number" min="0" max="86400" step="any" defaultValue="10" disabled={busy} required/></Field><button className="button primary" disabled={busy||Boolean(error?.uncertain)}>{busy?'正在推进…':'推进时间'}</button></div>{error&&<ErrorNotice error={error}/>} {error?.uncertain&&<p role="status">请刷新系统状态确认结果。</p>}{result&&<Receipt result={result}/>}</form>;
}
