import {useState} from 'react';
import {ApiClient, detail, encodeId, list, object, text, versionOf} from '../api';
import {useLookupQuery} from '../hooks';
import {decisionHref,workEventsHref} from '../navigation';
import {ActionButton, Field, LookupStatus, Metadata, Panel, QueryFeedback, RawData, Refresh, Status} from '../components/common';

export function ContinuityPage({api, initialId, onSelect}: {api: ApiClient; initialId: string; onSelect?: (id:string)=>void}) {
  const {input,setInput,id,query,submit}=useLookupQuery(api,'/x/v1/work-intents',initialId,onSelect);
  const work = detail(query.data);
  const version = versionOf(query.data);
  const result = object(work.result);
  const legal = list(object(query.data).legal_actions);
  const [actionRevision, setActionRevision] = useState(0);
  const stages = list(work.stages);
  const allocations=list(object(query.data).allocations);
  const holding=allocations.filter(allocation=>['Allocated','Running','PreemptRequested'].includes(text(allocation.state)));
  const decisionIds=[...new Set(list(object(query.data).events).map(event=>text(event.decision_id,'')).filter(Boolean))];
  const stage = `ui-${id}-${version?.epoch ?? 0}-${version?.revision ?? 0}-${actionRevision}`;
  const operationFor: Record<string,string> = {allocate:'workflow.create_allocation_and_bind',create_job:'workflow.create_job_and_bind',cancel:'job.cancel',release:'allocation.release'};
  const allowed = (action:string) => legal.some(item=>item.operation===operationFor[action]);
  const done = () => { setActionRevision(n=>n+1); query.refresh(); };
  return <>
    <Panel title="追踪同一项工作" description="查看已记录的执行与租约结算；保存点报告、核验和接续能力将在 CP2 接入。">
      <form className="inline-form" onSubmit={submit}><Field label="工作 ID"><input value={input} onChange={e=>setInput(e.target.value)} required placeholder="work-…"/></Field><button className="button primary" disabled={query.loading}>{query.loading?'正在查询…':'查询工作'}</button>{id && <Refresh loading={query.loading} onClick={query.refresh}/>}</form>
      <LookupStatus query={query}/>
      {!id && <div className="empty">输入工作 ID，或在运行工作台选择一项工作。</div>}
    </Panel>
    <QueryFeedback query={query}>
      <Panel title={id} actions={<Status value={work.state}/>}>
        <Metadata data={query.data}/><dl className="facts"><div><dt>首次需求到达</dt><dd>{text(work.first_arrival_at)} 逻辑秒</dd></div><div><dt>执行模式</dt><dd>{work.execution_mode === 'cpu_reference' ? '真实 CPU 参考执行' : '逻辑模拟'}</dd></div><div><dt>租约</dt><dd>{Array.isArray(work.allocation_ids) ? work.allocation_ids.map(String).join('，') || '尚无关联租约' : '尚无关联租约'}</dd></div><div><dt>结算选择</dt><dd>{({keep_lease:'保留租约，手动归还',release_on_success:'成功后自动归还',release_on_terminal:'结束后自动归还'} as Record<string,string>)[text(work.settlement_action)]||text(work.settlement_action)}</dd></div></dl>
        <div className="lease-summary" aria-label="租约与资源占用"><strong>租约与资源占用</strong>{allocations.length?allocations.map(allocation=><div key={text(allocation.allocation_id??allocation.id)}><code>{text(allocation.allocation_id??allocation.id)}</code><Status value={allocation.state}/></div>):<p>尚无关联租约记录。</p>}{holding.length>0&&<p>{work.settlement_action==='keep_lease'?'你选择了保留租约；任务完成或取消后仍会占用资源，需要手动释放。':'租约当前仍持有资源，是否归还以这里的租约状态为准。'}</p>}{allocations.length>0&&allocations.every(allocation=>['Released','Expired','Evicted','Rejected'].includes(text(allocation.state)))&&<p>这项工作当前没有持有资源。资源池若仍有占用，请查看其他工作的租约。</p>}</div>
        {work.execution_mode==='logical_simulation'&&<p className="helper simulation-note">CP1 任务需要 10 个逻辑秒。现实中等待不会推进时间，请管理员在<a href="#acceptance">系统验收</a>推进模拟时钟，再刷新这项工作。</p>}
        <div className="actions related-links"><a className="button" href={workEventsHref(id)}>查看关联事件</a>{decisionIds.map((decisionId,index)=><a className="button" key={decisionId} href={decisionHref(decisionId,id)}>{decisionIds.length===1?'查看关联决策':`查看关联决策 ${index+1}`}</a>)}</div>
        {!decisionIds.length&&<p className="helper">尚无可见的关联决策记录。</p>}
        {stages.some(s=>s.state==='failed') && Array.isArray(work.allocation_ids) && work.allocation_ids.length>0 && <div className="feedback warning"><strong>已有租约，后续阶段失败</strong><p>请核对下面的阶段回执。重试应使用已有工作与租约，避免重新申请另一份资源。</p></div>}
        <div className="actions">{(['allocate','create_job','cancel','release'] as const).map(action=><ActionButton key={`${action}-${stage}`} api={api} path={`/x/v1/work-intents/${encodeId(id)}/actions`} body={{action,stage_id:action==='allocate'?'allocate-v1':action==='create_job'?'job-v1':stage,expected_version:version}} disabled={!version || !allowed(action)} onDone={done} confirmation={['cancel','release'].includes(action)?'确定执行此操作？请核对租约及正在运行的任务。':undefined}>{({allocate:'重试资源申请',create_job:'重试任务创建',cancel:'取消工作',release:'释放租约'})[action]}</ActionButton>)}</div>
        <p className="helper">操作由服务端合法动作列表控制；不可用时请查看状态和回执。动作仍会在服务端复查权限与版本。</p>
      </Panel>
      <Panel title="执行与保存时间线">
        {!list(work.attempts).length ? <div className="empty">尚无执行代次记录。</div> : <ol className="timeline">{list(work.attempts).map(attempt=>{
          const ref=object(attempt.ref), checkpoint=object(attempt.checkpoint);
          return <li key={text(ref.attempt_id)}><div className="record-title"><strong>执行 #{text(ref.generation)} · <code>{text(ref.attempt_id)}</code></strong><Status value={attempt.state}/></div><p>创建 {text(attempt.created_at)} s · 开始 {text(attempt.started_at)} s · 结束 {text(attempt.finished_at)} s</p>{attempt.checkpoint ? <div className="checkpoint-grid"><div><span>保存报告</span><Status value={checkpoint.report_status}/><small>{text(checkpoint.reported_at)} 逻辑秒</small></div><div><span>工件核验</span><Status value={checkpoint.verification_status ?? 'unverified'}/><small>{text(checkpoint.verifier_id,'没有核验器记录')}</small></div><div><span>实际接续</span><Status value={checkpoint.continuation_status ?? 'not_attempted'}/><small>{text(checkpoint.resumed_attempt_id,'没有接续代次')}</small></div></div> : <p className="helper">这次执行尚无保存点报告。</p>}<RawData data={attempt}/></li>;
        })}</ol>}
      </Panel>
      <Panel title="持久阶段回执"><div className="record-list">{stages.length ? stages.map(s=><div className="record" key={text(s.stage_id)}><div className="record-title"><strong>{text(s.operation)}</strong><Status value={s.state}/></div><code>{text(s.stage_id)}</code><RawData data={s.receipt} label="阶段提交结果"/></div>):<p className="empty">尚无阶段回执。</p>}</div></Panel>
      <Panel title="结果与结算"><div className="record-list">{list(object(query.data).allocations).map(allocation=><div className="record" key={text(allocation.allocation_id ?? allocation.id)}><div className="record-title"><code>{text(allocation.allocation_id ?? allocation.id)}</code><Status value={allocation.state}/></div><p>租约截止：{text(allocation.expires_at_logical)} 逻辑秒</p></div>)}</div>{work.result ? <><Status value={result.kind}/><p>记录于 {text(result.recorded_at)} 逻辑秒</p>{result.failure && <div className="feedback error">{text(object(result.failure).detail)}</div>}<RawData data={result}/><p className="helper">租约按所选结算方式处理，最终状态以上方服务端记录为准。当前逻辑模拟不产生真实训练工件，工件下载待后续接口接入。</p></>:<p className="empty">尚未产生终态结果。</p>}</Panel>
      <RawData data={query.data} label="完整工作记录"/>
    </QueryFeedback>
  </>;
}
