import {useState} from 'react';
import {ApiClient,detail,encodeId,list,object,text,versionOf} from '../api';
import {useLookupQuery} from '../hooks';
import {decisionHref,workEventsHref} from '../navigation';
import {modeLabel,useService} from '../service';
import {ArtifactDownload,Budget,CheckpointFacts,StopStatus} from '../components/CP2';
import {ActionButton,Field,LookupStatus,Metadata,Panel,QueryFeedback,RawData,Refresh,Status} from '../components/common';

export function ContinuityPage({api,initialId,onSelect}:{api:ApiClient;initialId:string;onSelect?:(id:string)=>void}) {
  const service=useService();
  const {input,setInput,id,query,submit}=useLookupQuery(api,'/x/v1/work-intents',initialId,onSelect);
  const work=detail(query.data),version=versionOf(query.data),result=object(work.result);
  const legal=list(object(query.data).legal_actions),stages=list(work.stages);
  const [actionRevision,setActionRevision]=useState(0);
  const allocations=list(object(query.data).allocations),jobs=list(object(query.data).jobs);
  const holding=allocations.filter(a=>a.pending_terminal||['Allocated','Running','PreemptRequested'].includes(text(a.state)));
  const decisionIds=[...new Set(list(object(query.data).events).map(event=>text(event.decision_id,'')).filter(Boolean))];
  const spec=object(work.specification);
  const stopping=list(work.attempts).some(attempt=>['stopping','quarantined'].includes(text(attempt.state))&&jobs.some(job=>object(job.attempt).attempt_id===object(attempt.ref).attempt_id));
  const operationFor:Record<string,string>={allocate:'workflow.create_allocation_and_bind',create_job:'workflow.create_job_and_bind',cancel:'job.cancel',release:'allocation.release',resume:'allocation.resume'};
  const allowed=(action:string)=>legal.some(item=>item.operation===operationFor[action]);
  const allocationAction=(action:string,allocationId:string)=>legal.some(item=>item.operation===action&&object(item.entity).entity_id===allocationId);
  const done=()=>{setActionRevision(n=>n+1);query.refresh();};
  const resultUris=[...new Set([...(Array.isArray(result.artifact_uris)?result.artifact_uris:[]),...jobs.map(job=>job.result_uri)].filter((uri):uri is string=>typeof uri==='string'))];
  return <>
    <Panel title="追踪同一项工作" description="核对同一工作、租约和任务的各次执行，分别查看保存报告、工件核验与实际接续。">
      <form className="inline-form" onSubmit={submit}><Field label="工作 ID"><input value={input} onChange={e=>setInput(e.target.value)} required placeholder="work-…"/></Field><button className="button primary" disabled={query.loading}>{query.loading?'正在查询…':'查询工作'}</button>{id&&<Refresh loading={query.loading} onClick={query.refresh}/>}</form>
      <LookupStatus query={query}/>{!id&&<div className="empty">输入工作 ID，或在运行工作台选择一项工作。</div>}
    </Panel>
    <QueryFeedback query={query}>
      <Panel title={id} actions={<Status value={work.state}/>}>
        <Metadata data={query.data}/><dl className="facts"><div><dt>首次需求到达</dt><dd>{text(work.first_arrival_at)} 逻辑秒</dd></div><div><dt>执行模式</dt><dd>{modeLabel(work.execution_mode)}</dd></div><div><dt>租约</dt><dd>{Array.isArray(work.allocation_ids)?work.allocation_ids.map(String).join('，')||'尚无关联租约':'尚无关联租约'}</dd></div><div><dt>结算选择</dt><dd>{({keep_lease:'保留租约，手动归还',release_on_success:'成功后自动归还',release_on_terminal:'结束后自动归还'} as Record<string,string>)[text(work.settlement_action)]||text(work.settlement_action)}</dd></div></dl>
        <div className="lease-summary" aria-label="租约与资源占用"><strong>租约与资源占用</strong>{allocations.length?allocations.map(a=><div key={text(a.allocation_id??a.id)}><code>{text(a.allocation_id??a.id)}</code><Status value={a.state}/><StopStatus allocation={a}/></div>):<p>尚无关联租约记录。</p>}{holding.length>0&&<p>{work.settlement_action==='keep_lease'?'你选择了保留租约；任务完成或取消后仍会占用资源，需要手动释放。':'租约当前仍持有资源，是否归还以这里的租约状态为准。'}</p>}{allocations.length>0&&allocations.every(a=>!a.pending_terminal&&['Released','Expired','Evicted','Rejected'].includes(text(a.state)))&&<p>这项工作当前没有持有资源。资源池若仍有占用，请查看其他工作的租约。</p>}</div>
        {stopping&&<div className="feedback warning" role="status"><strong>当前执行正在停止或隔离中</strong><p>已收到停止请求，尚未确认进程退出。请刷新核对；不能据此认定任务已停机或 GPU 已归还。</p></div>}
        <p className="helper simulation-note">{work.execution_mode==='logical_simulation'?<>任务需要 10 个逻辑秒。现实中等待不会推进时间，请管理员在<a href="#acceptance">系统验收</a>推进模拟时钟，再刷新这项工作。</>:<>固定 CPU 参考程序随真实时间运行，未执行用户镜像与命令，GPU 库存仍为模拟。点击刷新核对最新进度和停止确认。</>}</p>
        <div className="actions related-links"><a className="button" href={workEventsHref(id)}>查看关联事件</a>{decisionIds.map((decisionId,index)=><a className="button" key={decisionId} href={decisionHref(decisionId,id)}>{decisionIds.length===1?'查看关联决策':`查看关联决策 ${index+1}`}</a>)}</div>
        {!decisionIds.length&&<p className="helper">尚无可见的关联决策记录。</p>}
        {stages.some(s=>s.state==='failed')&&allocations.length>0&&<div className="feedback warning"><strong>已有租约，后续阶段失败</strong><p>请核对下面的阶段回执。重试应使用已有工作与租约，避免重新申请另一份资源。</p></div>}
        <div className="actions">{(['allocate','create_job','resume','cancel','release'] as const).map(action=><ActionButton key={`${action}-${actionRevision}`} api={api} path={`/x/v1/work-intents/${encodeId(id)}/actions`} request={()=>api.workAction(id,action,version!,action==='allocate'?'allocate-v1':action==='create_job'?'job-v1':undefined)} disabled={!version||!allowed(action)||(action==='resume'&&service.capabilities.checkpoint_recovery!==true)} onDone={done} confirmation={['cancel','release'].includes(action)?'确定执行此操作？请核对租约及正在运行的任务。':undefined}>{({allocate:'重试资源申请',create_job:'重试任务创建',resume:'恢复这项工作',cancel:'取消工作',release:'释放租约'})[action]}</ActionButton>)}</div>
        <p className="helper">恢复沿用工作、租约与任务 ID，新执行代次由服务端创建。等待资源时再次刷新，不会提前计算成功恢复。按钮是当前快照的可用提示，提交时仍会复查权限、状态与预算。</p>
      </Panel>
      <Panel title="中断与恢复条件">
        {allocations.map(a=>{const aid=text(a.allocation_id??a.id,''),preemption=object(a.preemption);return <div className="record" key={aid}><div className="record-title"><code>{aid}</code><Status value={a.state}/></div><dl className="facts"><Budget label="收回恢复预算" used={a.restart_count} max={object(spec.allocation).max_restarts}/><div><dt>租约截止</dt><dd>{text(a.expires_at_logical,'未记录')} 逻辑秒</dd></div>{Boolean(a.preemption)&&<><div><dt>收回通知 / 截止</dt><dd>{text(preemption.requested_at)} / {text(preemption.deadline)} 逻辑秒</dd></div><div><dt>保存报告截止</dt><dd>{text(preemption.checkpoint_due)} 逻辑秒</dd></div></>}</dl><CheckpointFacts api={api} checkpoint={a.checkpoint}/>{work.execution_mode==='logical_simulation'&&service.capabilities.checkpoint_recovery===true&&allocationAction('allocation.checkpoint',aid)&&<div className="logical-report"><p className="helper">逻辑模拟：仅报告保存状态，不上传真实文件；ready 不代表工件已核验。</p><div className="actions"><ActionButton api={api} path={`/v1/allocations/${encodeId(aid)}/checkpoint`} body={{status:'ready'}} onDone={done}>报告保存 ready</ActionButton><ActionButton api={api} path={`/v1/allocations/${encodeId(aid)}/checkpoint`} body={{status:'failed'}} onDone={done}>报告保存 failed</ActionButton></div></div>}{work.execution_mode==='cpu_reference'&&<p className="helper">CPU 保存点由实际执行器自动报告与核验。</p>}</div>;})}
        {!allocations.length&&<p className="empty">尚无租约，无法恢复或报告保存点。</p>}
        {jobs.map(job=><div className="record" key={text(job.job_id??job.id)}><div className="record-title"><strong>任务 <code>{text(job.job_id??job.id)}</code></strong><Status value={job.state}/></div><dl className="facts"><Budget label="任务失败重试预算" used={job.retry_count} max={object(spec.job).max_retries??0}/><div><dt>当前执行代次</dt><dd>#{text(object(job.attempt).generation)} · <code>{text(object(job.attempt).attempt_id)}</code></dd></div></dl><p className="helper">任务失败重试与收回恢复分别计数，不共享预算。</p>{Boolean(job.failure)&&<RawData data={job.failure} label="任务失败记录"/>}</div>)}
      </Panel>
      <Panel title="执行与保存时间线">
        {!list(work.attempts).length?<div className="empty">尚无执行代次记录。</div>:<ol className="timeline">{list(work.attempts).map(attempt=>{const ref=object(attempt.ref);return <li key={text(ref.attempt_id)}><div className="record-title"><strong>执行 #{text(ref.generation)} · <code>{text(ref.attempt_id)}</code></strong><Status value={attempt.state}/></div><p>任务 <code>{text(ref.job_id)}</code> · 创建 {text(attempt.created_at)} s · 开始 {text(attempt.started_at)} s · 结束 {text(attempt.finished_at)} s</p><CheckpointFacts api={api} checkpoint={attempt.checkpoint}/>{!attempt.checkpoint&&work.execution_mode==='cpu_reference'&&<p className="helper">新代次已登记不代表保存点已应用；请等待执行器确认并刷新。</p>}<RawData data={attempt}/></li>;})}</ol>}
      </Panel>
      <Panel title="持久阶段回执"><div className="record-list">{stages.length?stages.map(s=><div className="record" key={text(s.stage_id)}><div className="record-title"><strong>{text(s.operation)}</strong><Status value={s.state}/></div><code>{text(s.stage_id)}</code><RawData data={s.receipt} label="阶段提交结果"/></div>):<p className="empty">尚无阶段回执。</p>}</div></Panel>
      <Panel title="结果与结算">{work.result?<><Status value={result.kind}/><p>记录于 {text(result.recorded_at)} 逻辑秒</p>{result.failure&&<div className="feedback error">{text(object(result.failure).detail)}</div>}<RawData data={result}/><p className="helper">租约按所选结算方式处理，最终状态以上方服务端记录为准。</p></>:<p className="empty">尚未产生终态结果。</p>}<div className="actions">{resultUris.map((uri,index)=><ArtifactDownload api={api} key={uri} uri={uri} label={`下载真实结果 ${index+1}`}/>)}</div>{work.execution_mode==='logical_simulation'&&<p className="helper">逻辑模拟不产生真实计算工件。</p>}{work.execution_mode==='cpu_reference'&&<p className="helper">下载只读取服务端登记且当前身份获授权的 CPU 工件；真实 CPU 参考结果不代表真实 GPU 训练。</p>}</Panel>
      <RawData data={query.data} label="完整工作记录"/>
    </QueryFeedback>
  </>;
}
