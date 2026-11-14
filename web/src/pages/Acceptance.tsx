import {useState,type FormEvent} from 'react';
import {ApiClient,ApiError,items,object,text,type ApiResult} from '../api';
import {useQuery} from '../hooks';
import {downloadJson,ErrorNotice,Field,Metadata,Panel,QueryFeedback,RawData,Receipt,Refresh,Status,Table} from '../components/common';

export function AcceptancePage({api,showAdminClock=false}:{api:ApiClient;showAdminClock?:boolean}) {
  const version=useQuery(api,'/v1/version'), checks=useQuery(api,'/x/v1/acceptance'), overview=useQuery(api,'/x/v1/admin/overview'),metrics=useQuery(api,'/v1/metrics');
  const capabilities=object(object(version.data).capabilities);
  return <>
    {showAdminClock&&<SimulationClock api={api} onDone={()=>{version.refresh();checks.refresh();overview.refresh();metrics.refresh();}}/>}
    <Panel title="构建与服务能力" description="服务在线只代表 HTTP 可以响应，不代表资源账本、任务执行或比赛验收已经通过。" actions={<Refresh onClick={version.refresh} loading={version.loading}/>}>
      <QueryFeedback query={version}><dl className="facts"><div><dt>API 版本</dt><dd>{text(object(version.data).api_version)}</dd></div><div><dt>实现版本</dt><dd>{text(object(version.data).implementation_version)}</dd></div><div><dt>当前阶段</dt><dd>{text(object(version.data).checkpoint)}</dd></div></dl><div className="capabilities">{Object.entries(capabilities).map(([key,value])=><div key={key}><code>{key}</code><span>{value===true?'服务声明已提供':value===false?'服务声明未提供':text(value)}</span></div>)}</div><RawData data={version.data}/></QueryFeedback>
    </Panel>
    <Panel title="验收证据" description="展示目标及证据状态。NOT_RUN 表示尚未执行；只有与当前构建匹配的实际回执才能支持通过。" actions={<Refresh onClick={checks.refresh} loading={checks.loading}/>}>
      <Metadata data={checks.data}/><QueryFeedback query={checks} empty={!items(checks.data).length}><Table rows={items(checks.data)} rowKey={(r,i)=>`${text(r.target_id)}-${i}`} columns={[{label:'验收目标',render:r=>text(r.target_id)},{label:'状态',render:r=><Status value={r.status}/>},{label:'实际结果',render:r=>text(r.actual,'未运行')},{label:'执行证据',render:r=><RawData data={r} label="查看条件与证据"/>}]}/><button className="button" onClick={()=>downloadJson(checks.data,'acceptance-records.json')}>导出验收记录</button></QueryFeedback>
    </Panel>
    <Panel title="当前身份的状态计数" description="计数只描述可见对象，不代表真实 GPU 利用率或吞吐测量。" actions={<Refresh onClick={metrics.refresh} loading={metrics.loading}/>}><Metadata data={metrics.data}/><QueryFeedback query={metrics}><RawData data={metrics.data} label="展开当前指标"/></QueryFeedback></Panel>
    <Panel title="管理员总览" description="全局视图需要服务端管理员授权。更改页面视角不会增加权限。" actions={<Refresh onClick={overview.refresh} loading={overview.loading}/>}><Metadata data={overview.data}/><QueryFeedback query={overview}><RawData data={overview.data} label="展开受权总览"/></QueryFeedback></Panel>
  </>;
}

function SimulationClock({api,onDone}:{api:ApiClient;onDone:()=>void}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null),[result,setResult]=useState<ApiResult|null>(null);
  async function advance(event:FormEvent<HTMLFormElement>){
    event.preventDefault();setBusy(true);setError(null);setResult(null);
    const advance_seconds=Number(new FormData(event.currentTarget).get('seconds'));
    try{setResult(await api.request('/x/v1/admin/clock',{method:'POST',body:{advance_seconds}}));onDone();}
    catch(error){setError(error instanceof ApiError?error:new ApiError('逻辑时钟推进失败',0));}
    finally{setBusy(false);}
  }
  return <Panel title="管理员 · 模拟时钟" description="推进逻辑时间会触发任务完成、超时和租约到期。它不会加速真实 CPU/GPU 计算；CP1 任务采用 10 秒固定逻辑工期。"><form className="inline-form" onSubmit={advance}><Field label="推进秒数"><input name="seconds" type="number" min="0" max="86400" step="any" defaultValue="10" disabled={busy} required/></Field><button className="button primary" disabled={busy}>{busy?'正在推进…':'推进模拟时间'}</button></form>{error&&<ErrorNotice error={error}/>} {result&&<Receipt result={result}/>}</Panel>;
}
