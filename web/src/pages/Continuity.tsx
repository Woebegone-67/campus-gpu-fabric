import {CopyWorkId} from '../components/CopyWorkId';
import {useState} from 'react';
import {ApiClient,detail,encodeId,list,object,text,versionOf,type ApiResult} from '../api';
import {useLookupQuery} from '../hooks';
import {decisionHref,workEventsHref} from '../navigation';
import {modeLabel,useService} from '../service';
import {workName} from '../workName';
import {Budget,CheckpointFacts,StopStatus} from '../components/CP2';
import {SettlementSummary} from '../components/Settlement';
import {ActionButton,Field,LookupStatus,Metadata,Panel,QueryFeedback,RawData,Receipt,Refresh,Status} from '../components/common';

export function ContinuityPage({api,initialId,onSelect}:{api:ApiClient;initialId:string;onSelect?:(id:string)=>void}) {
  const service=useService();
  const {input,setInput,id,query,submit}=useLookupQuery(api,'/x/v1/work-intents',initialId,onSelect);
  const work=detail(query.data),version=versionOf(query.data);
  const legal=list(object(query.data).legal_actions),stages=list(work.stages);
  const [actionRevision,setActionRevision]=useState(0);
  const [lastAction,setLastAction]=useState<{id:string;api:ApiClient;result:ApiResult}|null>(null);
  const allocations=list(object(query.data).allocations),jobs=list(object(query.data).jobs);
  const holding=allocations.filter(a=>a.pending_terminal||['Allocated','Running','PreemptRequested'].includes(text(a.state)));
  const decisionIds=[...new Set(list(object(query.data).events).map(event=>text(event.decision_id,'')).filter(Boolean))];
  const spec=object(work.specification);
  const stopping=list(work.attempts).some(attempt=>['stopping','quarantined'].includes(text(attempt.state))&&jobs.some(job=>object(job.attempt).attempt_id===object(attempt.ref).attempt_id));
  const operationFor:Record<string,string>={allocate:'workflow.create_allocation_and_bind',create_job:'workflow.create_job_and_bind',cancel:'job.cancel',release:'allocation.release',resume:'allocation.resume'};
  const allowed=(action:string)=>legal.some(item=>item.operation===operationFor[action]);
  const allocationAction=(action:string,allocationId:string)=>legal.some(item=>item.operation===action&&object(item.entity).entity_id===allocationId);
  const done=()=>{setActionRevision(n=>n+1);query.refresh();};
  const terminal=['completed','failed','cancelled'].includes(text(work.state));
  const attempts=list(work.attempts);
  const currentAttempts=attempts.filter(a=>jobs.some(job=>object(job.attempt).attempt_id===object(a.ref).attempt_id));
  const previousAttempts=attempts.filter(a=>!currentAttempts.includes(a));
  const actions=['allocate','create_job','resume','cancel','release'] as const;
  const primaryAction=terminal&&allowed('release')?'release':allowed('resume')&&service.capabilities.checkpoint_recovery===true?'resume':allowed('create_job')?'create_job':allowed('allocate')?'allocate':null;
  function workAction(action:typeof actions[number]){return <ActionButton key={`${action}-${actionRevision}`} api={api} path={`/x/v1/work-intents/${encodeId(id)}/actions`} request={()=>api.workAction(id,action,version!,action==='allocate'?'allocate-v1':action==='create_job'?'job-v1':undefined)} disabled={!version||!allowed(action)||(action==='resume'&&service.capabilities.checkpoint_recovery!==true)} onDone={done} onResult={result=>setLastAction({id,api,result})} confirmation={['cancel','release'].includes(action)?'确定执行此操作？请核对租约及正在运行的任务。':undefined}>{({allocate:'重试资源申请',create_job:'重试任务创建',resume:'恢复这项工作',cancel:'取消工作',release:'释放租约'})[action]}</ActionButton>;}
  function attemptDetails(attempt:ReturnType<typeof object>){const ref=object(attempt.ref);return <li key={text(ref.attempt_id)}><div className="record-title"><strong>执行 #{text(ref.generation)} · <code>{text(ref.attempt_id).length>22?`${text(ref.attempt_id).slice(0,13)}…${text(ref.attempt_id).slice(-6)}`:text(ref.attempt_id)}</code></strong><Status value={attempt.state}/></div><CheckpointFacts api={api} checkpoint={attempt.checkpoint}/>{!attempt.checkpoint&&work.execution_mode==='cpu_reference'&&!terminal&&currentAttempts.includes(attempt)&&<p className="helper">新代次已登记不代表保存点已应用；请等待执行器确认并刷新。</p>}<details className="work-disclosure work-technical"><summary>执行信息</summary><p>执行 <code>{text(ref.attempt_id)}</code> · 任务 <code>{text(ref.job_id)}</code></p><p>创建 {text(attempt.created_at)} s · 开始 {text(attempt.started_at)} s · 结束 {text(attempt.finished_at)} s</p><RawData data={attempt}/></details></li>;}
  return <div className="work-experience work-detail">
    <Panel title="查找工作" description="输入完整工作 ID，或从我的工作打开详情。">
      <form className="inline-form" onSubmit={submit}><Field label="工作 ID"><input value={input} onChange={e=>setInput(e.target.value)} required placeholder="work-…"/></Field><button className="button primary" disabled={query.loading}>{query.loading?'正在查询…':'查询工作'}</button>{id&&<Refresh loading={query.loading} onClick={query.refresh}/>}</form>
      <LookupStatus query={query}/>{!id&&<div className="empty">输入工作 ID，或在运行工作台选择一项工作。</div>}
    </Panel>
    {lastAction?.id===id&&lastAction.api===api&&<Receipt result={lastAction.result}/>}
    <QueryFeedback query={query}>
      <Panel title={workName(work,id)} actions={<div className="work-copy-actions"><CopyWorkId id={id} scope={api}/><Status value={work.state}/></div>}>
        <p className="helper work-detail-subtitle">{modeLabel(work.execution_mode)}{object(spec.job).image?` · ${text(object(spec.job).image)}`:''}</p>
        <SettlementSummary api={api} work={work} jobs={jobs} allocations={allocations} legal={legal}/>
        <div className="work-detail-actions">
          {primaryAction&&<div className="work-primary-action">{workAction(primaryAction)}</div>}
          <details className="work-disclosure work-secondary-actions"><summary>更多操作</summary><div className="actions">{actions.filter(action=>action!==primaryAction).map(workAction)}</div><p className="helper">不可用操作会保持禁用；提交时仍会复查权限、状态与恢复预算。</p></details>
        </div>
        <div className="lease-summary" aria-label="租约与资源占用"><strong>资源占用</strong>{allocations.length?allocations.map(a=><div key={text(a.allocation_id??a.id)}><code>{text(a.allocation_id??a.id)}</code><Status value={a.state}/><StopStatus allocation={a}/></div>):<p>尚无关联租约记录。</p>}{holding.length>0&&<p>{work.settlement_action==='keep_lease'?'你选择了保留租约；任务完成或取消后仍会占用资源，需要手动释放。':'租约当前仍持有资源，是否归还以这里的租约状态为准。'}</p>}{allocations.length>0&&allocations.every(a=>!a.pending_terminal&&['Released','Expired','Evicted','Rejected'].includes(text(a.state)))&&<p>这项工作当前没有持有资源。资源池若仍有占用，请查看其他工作的租约。</p>}</div>
        {stopping&&<div className="feedback warning" role="status"><strong>当前执行正在停止或隔离中</strong><p>已收到停止请求，尚未确认进程退出。请刷新核对；不能据此认定任务已停机或 GPU 已归还。</p></div>}
        {!terminal&&<p className="helper simulation-note">{work.execution_mode==='logical_simulation'?<>任务需要 10 个逻辑秒。现实中等待不会推进时间，请管理员在<a href="#acceptance">系统验收</a>推进模拟时钟，再刷新这项工作。</>:<>固定 CPU 参考程序随真实时间运行，未执行用户镜像与命令，GPU 库存仍为模拟。点击刷新核对最新进度和停止确认。</>}</p>}
        <details className="work-disclosure"><summary>相关事件与决策</summary><div className="actions related-links"><a className="button" href={workEventsHref(id)}>查看关联事件</a>{decisionIds.map((decisionId,index)=><a className="button" key={decisionId} href={decisionHref(decisionId,id)}>{decisionIds.length===1?'查看关联决策':`查看关联决策 ${index+1}`}</a>)}</div>
        {!decisionIds.length&&<p className="helper">尚无可见的关联决策记录。</p>}
        </details>
        <details className="work-disclosure work-technical"><summary>工作信息与技术详情</summary>
          <div className="work-full-id"><code>{id}</code></div>
          <Metadata data={query.data}/><dl className="facts"><div><dt>首次需求到达</dt><dd>{text(work.first_arrival_at)} 逻辑秒</dd></div><div><dt>结算选择</dt><dd>{({keep_lease:'保留租约，手动归还',release_on_success:'成功后自动归还',release_on_terminal:'结束后自动归还'} as Record<string,string>)[text(work.settlement_action)]||text(work.settlement_action)}</dd></div></dl>
          <p className="helper">恢复沿用工作、租约与任务 ID，新执行代次由服务端创建。等待资源时再次刷新，不会提前计算成功恢复。</p>
          <RawData data={query.data} label="完整工作记录"/>
        </details>
      </Panel>
      <Panel title="中断与恢复条件">
        {allocations.map(a=>{const aid=text(a.allocation_id??a.id,''),preemption=object(a.preemption);return <div className="record" key={aid}><div className="record-title"><code>{aid}</code><Status value={a.state}/></div><dl className="facts"><Budget label="收回恢复预算" used={a.restart_count} max={object(spec.allocation).max_restarts}/><div><dt>租约截止</dt><dd>{text(a.expires_at_logical,'未记录')} 逻辑秒</dd></div>{Boolean(a.preemption)&&<><div><dt>收回通知 / 截止</dt><dd>{text(preemption.requested_at)} / {text(preemption.deadline)} 逻辑秒</dd></div><div><dt>保存报告截止</dt><dd>{text(preemption.checkpoint_due)} 逻辑秒</dd></div></>}</dl><CheckpointFacts api={api} checkpoint={a.checkpoint}/>{work.execution_mode==='logical_simulation'&&service.capabilities.checkpoint_recovery===true&&allocationAction('allocation.checkpoint',aid)&&<div className="logical-report"><p className="helper">逻辑模拟：仅报告保存状态，不上传真实文件；ready 不代表工件已核验。</p><div className="actions"><ActionButton api={api} path={`/v1/allocations/${encodeId(aid)}/checkpoint`} body={{status:'ready'}} onDone={done}>报告保存 ready</ActionButton><ActionButton api={api} path={`/v1/allocations/${encodeId(aid)}/checkpoint`} body={{status:'failed'}} onDone={done}>报告保存 failed</ActionButton></div></div>}{work.execution_mode==='cpu_reference'&&<p className="helper">CPU 保存点由实际执行器自动报告与核验。</p>}</div>;})}
        {!allocations.length&&<p className="empty">尚无租约，无法恢复或报告保存点。</p>}
        {jobs.map(job=><div className="record" key={text(job.job_id??job.id)}><div className="record-title"><strong>任务 <code>{text(job.job_id??job.id)}</code></strong><Status value={job.state}/></div><dl className="facts"><Budget label="任务失败重试预算" used={job.retry_count} max={object(spec.job).max_retries??0}/><div><dt>当前执行代次</dt><dd>#{text(object(job.attempt).generation)} · <code>{text(object(job.attempt).attempt_id)}</code></dd></div></dl><p className="helper">任务失败重试与收回恢复分别计数，不共享预算。</p>{Boolean(job.failure)&&<RawData data={job.failure} label="任务失败记录"/>}</div>)}
      </Panel>
      <Panel title="执行与保存时间线">
        {!attempts.length?<div className="empty">尚无执行代次记录。</div>:<><ol className="timeline">{currentAttempts.map(attemptDetails)}</ol>{previousAttempts.length>0&&<details className="history"><summary>查看历史执行（{previousAttempts.length} 次）</summary><ol className="timeline">{previousAttempts.map(attemptDetails)}</ol></details>}</>}
      </Panel>
      <Panel title="持久阶段回执"><details className="history"><summary>查看阶段回执（{stages.length} 条{stages.some(s=>s.state==='failed')?'，含失败记录':''}）</summary><div className="record-list">{stages.length?stages.map(s=><div className="record" key={text(s.stage_id)}><div className="record-title"><strong>{text(s.operation)}</strong><Status value={s.state}/></div><code>{text(s.stage_id)}</code><RawData data={s.receipt} label="阶段提交结果"/></div>):<p className="empty">尚无阶段回执。</p>}</div></details></Panel>
    </QueryFeedback>
  </div>;
}
