import {useState} from 'react';
import {ApiClient,detail,encodeId,items,list,object,text} from '../api';
import {useLookupQuery,useQuery} from '../hooks';
import {decisionHref,workHref} from '../navigation';
import {downloadJson,Field,LookupStatus,Metadata,Panel,QueryFeedback,RawData,Refresh,Status,Table} from '../components/common';

const eventLabels:Record<string,string>={
  'intent.registered':'工作已登记','allocation.requested':'已申请资源','allocation.granted':'资源已分配',
  'decision.committed':'决策已提交','job.started':'任务开始运行','job.succeeded':'任务运行成功',
  'job.failed':'任务运行失败','job.cancelled':'任务已取消','intent.completed':'工作已完成',
  'intent.cancelled':'工作已取消','allocation.released':'租约已释放','intent.lease_settled':'租约已结算',
  'intent.stage_bound':'执行阶段已关联',
};

export function EventsPage({api,intentId=''}:{api:ApiClient;intentId?:string}) {
  const [cursors,setCursors]=useState<string[]>(['']),[filter,setFilter]=useState('');
  const cursor=cursors[cursors.length-1];
  const query=useQuery(api,intentId?`/x/v1/work-intents/${encodeId(intentId)}`:`/v1/events?limit=50${cursor?`&after=${encodeURIComponent(cursor)}`:''}`);
  const allRows=intentId?list(object(query.data).events):items(query.data);
  const rows=allRows.filter(row=>JSON.stringify(row).toLowerCase().includes(filter.toLowerCase()));
  const exportData=intentId?{version:object(query.data).version,now:object(query.data).now,scope:'work',intent_id:intentId,items:allRows}:query.data;
  const {input:runInput,setInput:setRunInput,id:runId,query:runQuery,submit:queryRun}=useLookupQuery(api,'/x/v1/experiments');
  const run=detail(runQuery.data);
  return <>
    <Panel title={intentId?'这项工作的事件':'事件时间线'} description={intentId?'集中查看这项工作及其关联租约、任务的已提交事件。':'已提交事实按逻辑时间和事件序号展示；当前身份只能读取授权范围内的事件。'} actions={<div className="panel-actions">
      <Refresh onClick={()=>{setCursors(['']);query.refresh();}} loading={query.loading}/>
      <button type="button" className="button" disabled={query.loading||query.data===null||Boolean(query.error)} onClick={()=>downloadJson(exportData,intentId?`events-${encodeId(intentId)}.json`:'events-current-page.json')}>{intentId?'导出这项工作的事件':'导出当前页真实记录'}</button>
    </div>}>
      {intentId&&<div className="event-context"><code>{intentId}</code><a href={workHref(intentId)}>返回这项工作</a><a href="#events">查看全部事件</a></div>}
      <input className="search" aria-label={intentId?'筛选这项工作的事件':'筛选当前页事件'} placeholder="筛选事件、对象 ID 或决定 ID" value={filter} onChange={e=>setFilter(e.target.value)}/>
      <p className="helper">{intentId?'导出包含服务端返回的这项工作及关联租约、任务事件。':'每页最多 50 条；导出包含当前页全部记录。'} 筛选只改变页面显示，不改变导出范围。</p>
      <Metadata data={query.data}/>
      <QueryFeedback query={query} empty={!allRows.length}>
        {rows.length?<ol className="timeline">{rows.map(event=><li key={text(event.event_id)}>
          <div className="record-title"><strong>{eventLabels[text(event.event_type)]||text(event.event_type)}</strong><span>{text(event.logical_time)} s · #{text(event.sequence)}</span></div>
          <code>{text(event.event_type)} · {text(event.event_id)}</code>
          {list(event.state_changes).map((change,index)=><p key={index}>{text(object(change.entity).entity_id)}：{text(change.before,'未记录')} → {text(change.after)}</p>)}
          {typeof event.decision_id==='string'&&<p><a className="button" href={decisionHref(event.decision_id,intentId)}>查看这次决定</a></p>}
          <RawData data={event}/>
        </li>)}</ol>:<p className="empty">{intentId?'这项工作中':'当前页'}没有匹配项。</p>}
        {!intentId&&<div className="actions"><button type="button" className="button quiet" disabled={cursors.length===1} onClick={()=>setCursors(values=>values.slice(0,-1))}>上一页</button><span className="helper">第 {cursors.length} 页</span><button type="button" className="button quiet" disabled={!object(query.data).has_more || typeof object(query.data).next_after!=='string'} onClick={()=>setCursors(values=>[...values,object(query.data).next_after as string])}>下一页</button></div>}
      </QueryFeedback>
    </Panel>
    <Panel title="隔离实验记录" description="按 run ID 查询已登记的实验与原始证据。运行环境、输入和策略身份彼此独立。"><form className="inline-form" onSubmit={queryRun}><Field label="Run ID"><input value={runInput} onChange={e=>setRunInput(e.target.value)} required placeholder="run-…"/></Field><button className="button primary" disabled={runQuery.loading}>{runQuery.loading?'正在查询…':'查询实验'}</button></form><LookupStatus query={runQuery}/><p className="helper">实验提交需接入管理员写入口与已登记场景清单。本版提供读取与导出，不使用运营实例重放或重置。</p></Panel>
    <QueryFeedback query={runQuery}>
      <Panel title={`实验 ${runId}`} actions={<Status value={run.status}/>}><Metadata data={runQuery.data}/><dl className="facts"><div><dt>运行类型</dt><dd>{text(run.run_kind)}</dd></div><div><dt>执行模式</dt><dd>{text(object(run.environment).execution_mode)}</dd></div><div><dt>策略</dt><dd>{text(object(run.policy).name)} · {text(object(run.policy).version)}</dd></div><div><dt>种子 / 窗口</dt><dd>{text(run.seed)} · {text(object(run.window).start)}—{text(object(run.window).end)} s</dd></div></dl><Table rows={list(run.checks)} rowKey={(r,i)=>`${text(r.target_id)}-${i}`} columns={[{label:'目标',render:r=>text(r.target_id)},{label:'结果',render:r=><Status value={r.status}/>},{label:'实际观察',render:r=>text(r.actual,'尚未运行')}]}/><RawData data={runQuery.data} label="输入、输出、版本与异常记录"/><button type="button" className="button" onClick={()=>downloadJson(runQuery.data,`experiment-${runId}.json`)}>导出实验记录</button></Panel>
    </QueryFeedback>
  </>;
}
