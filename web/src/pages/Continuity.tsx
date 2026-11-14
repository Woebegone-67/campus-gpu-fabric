import {CopyWorkId} from '../components/CopyWorkId';
import {useState} from 'react';
import {ApiClient,detail,encodeId,list,object,text,versionOf,type ApiResult} from '../api';
import {useLookupQuery} from '../hooks';
import {decisionHref,workEventsHref} from '../navigation';
import {useService} from '../service';
import {workName} from '../workName';
import {Budget,CheckpointFacts,StopStatus} from '../components/CP2';
import {SettlementSummary} from '../components/Settlement';
import {ActionButton,Field,LookupStatus,Metadata,Panel,QueryFeedback,RawData,Refresh,Status} from '../components/common';

export function ContinuityPage({api,initialId,onSelect}:{api:ApiClient;initialId:string;onSelect?:(id:string)=>void}) {
  const service=useService();
  const {input,setInput,id,query,submit}=useLookupQuery(api,'/x/v1/work-intents',initialId,onSelect);
  const work=detail(query.data),version=versionOf(query.data);
  const legal=list(object(query.data).legal_actions),stages=list(work.stages);
  const [lastAction,setLastAction]=useState<{id:string;api:ApiClient;result:ApiResult}|null>(null);
  const allocations=list(object(query.data).allocations),jobs=list(object(query.data).jobs);
  const decisionIds=[...new Set(list(object(query.data).events).map(event=>text(event.decision_id,'')).filter(Boolean))];
  const spec=object(work.specification);
  const operationFor:Record<string,string>={allocate:'workflow.create_allocation_and_bind',create_job:'workflow.create_job_and_bind',cancel:'job.cancel',release:'allocation.release',resume:'allocation.resume'};
  const allowed=(action:string)=>legal.some(item=>item.operation===operationFor[action])&&(action!=='resume'||service.capabilities.checkpoint_recovery===true);
  const allocationAction=(action:string,allocationId:string)=>legal.some(item=>item.operation===action&&object(item.entity).entity_id===allocationId);
  const done=()=>query.refresh();
  const terminal=['completed','failed','cancelled'].includes(text(work.state));
  const attempts=list(work.attempts);
  const currentAttempts=attempts.filter(a=>jobs.some(job=>object(job.attempt).attempt_id===object(a.ref).attempt_id));
  const previousAttempts=attempts.filter(a=>!currentAttempts.includes(a));
  const actions=['allocate','create_job','resume','cancel','release'] as const;
  const primaryAction=terminal&&allowed('release')?'release':allowed('resume')?'resume':allowed('create_job')?'create_job':allowed('allocate')?'allocate':null;
  const secondaryActions=actions.filter(action=>action!==primaryAction&&allowed(action));
  function workAction(action:typeof actions[number]){return <ActionButton key={`${action}-${query.receivedAt??0}`} api={api} path={`/x/v1/work-intents/${encodeId(id)}/actions`} request={()=>api.workAction(id,action,version!,action==='allocate'?'allocate-v1':action==='create_job'?'job-v1':undefined)} disabled={!version||query.loading||Boolean(query.error)} onDone={done} onResult={result=>setLastAction({id,api,result})} confirmation={action==='cancel'?'确定取消这项工作？':action==='release'?'确定归还资源？正在执行的任务会停止。':undefined}>{({allocate:'重试资源申请',create_job:'重试任务创建',resume:'恢复这项工作',cancel:'取消工作',release:'释放租约'})[action]}</ActionButton>;}
  function attemptDetails(attempt:ReturnType<typeof object>){const ref=object(attempt.ref);return <li key={text(ref.attempt_id)}><div className="record-title"><strong>第 {text(ref.generation)} 次执行</strong><Status value={attempt.state}/></div><CheckpointFacts api={api} checkpoint={attempt.checkpoint}/></li>;}
  return <div className="work-experience work-detail">
    <details className="work-disclosure work-lookup" open={!id}><summary>{id?'查找其他工作':'查找工作'}</summary><form className="inline-form" onSubmit={submit}><Field label="工作 ID"><input value={input} onChange={e=>setInput(e.target.value)} required placeholder="work-…"/></Field><button className="button primary" disabled={query.loading}>{query.loading?'正在查询…':'查询工作'}</button></form></details>
    {id&&<QueryFeedback query={query}>
      <Panel title={workName(work,id)} actions={<div className="work-copy-actions"><Status value={work.state}/><Refresh loading={query.loading} onClick={done}/></div>}>
        <SettlementSummary api={api} work={work} jobs={jobs} allocations={allocations} legal={legal}/>
        {(primaryAction||secondaryActions.length>0)&&<div className="work-detail-actions">
          {primaryAction&&<div className="work-primary-action">{workAction(primaryAction)}</div>}
          {secondaryActions.length>0&&<details className="work-disclosure work-secondary-actions"><summary>更多操作</summary><div className="actions">{secondaryActions.map(workAction)}</div></details>}
        </div>}
        {attempts.length>0&&<section className="work-execution"><h3>执行进度</h3><ol className="timeline">{currentAttempts.map(attemptDetails)}</ol>{previousAttempts.length>0&&<details className="history"><summary>之前的执行（{previousAttempts.length} 次）</summary><ol className="timeline">{previousAttempts.map(attemptDetails)}</ol></details>}</section>}
        <div className="actions related-links"><a className="button quiet" href={workEventsHref(id)}>活动记录</a>{decisionIds.map((decisionId,index)=><a className="button quiet" key={decisionId} href={decisionHref(decisionId,id)}>{decisionIds.length===1?'分配说明':`分配说明 ${index+1}`}</a>)}</div>
        <LookupStatus query={query}/>
        <details className="work-disclosure work-technical"><summary>技术详情</summary>
          {lastAction?.id===id&&lastAction.api===api&&<RawData data={lastAction.result} label="最近操作"/>}
          <div className="work-full-id"><code>{id}</code><CopyWorkId id={id} scope={api}/></div><Metadata data={query.data}/>
          {allocations.map(a=>{const aid=text(a.allocation_id??a.id,''),preemption=object(a.preemption);return <div className="record" key={aid}><div className="record-title"><strong>租约 <code>{aid}</code></strong><Status value={a.state}/></div><StopStatus allocation={a}/><CheckpointFacts api={api} checkpoint={a.checkpoint}/><dl className="facts"><Budget label="收回恢复次数" used={a.restart_count} max={object(spec.allocation).max_restarts}/><div><dt>租约截止</dt><dd>{text(a.expires_at_logical)} 秒</dd></div>{Boolean(a.preemption)&&<div><dt>收回截止</dt><dd>{text(preemption.deadline)} 秒</dd></div>}</dl>{work.execution_mode==='logical_simulation'&&service.capabilities.checkpoint_recovery===true&&allocationAction('allocation.checkpoint',aid)&&<div className="actions"><ActionButton api={api} path={`/v1/allocations/${encodeId(aid)}/checkpoint`} body={{status:'ready'}} onDone={done}>报告保存成功</ActionButton><ActionButton api={api} path={`/v1/allocations/${encodeId(aid)}/checkpoint`} body={{status:'failed'}} onDone={done}>报告保存失败</ActionButton></div>}</div>;})}
          {jobs.map(job=><div className="record" key={text(job.job_id??job.id)}><strong>任务 <code>{text(job.job_id??job.id)}</code></strong><dl className="facts"><Budget label="失败重试次数" used={job.retry_count} max={object(spec.job).max_retries??0}/></dl></div>)}
          {stages.length>0&&<RawData data={stages} label="操作记录"/>}<RawData data={query.data} label="完整工作记录"/>
        </details>
      </Panel>
    </QueryFeedback>}
  </div>;
}
