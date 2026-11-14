import {useState} from 'react';
import {ApiClient,detail,encodeId,items,list,object,text} from '../api';
import {useQuery} from '../hooks';
import {Experiments} from '../components/Experiments';
import {decisionHref,workHref} from '../navigation';
import {downloadJson,Metadata,Panel,QueryFeedback,RawData,Refresh} from '../components/common';
import {CopyWorkId} from '../components/CopyWorkId';
import {workName} from '../workName';

const eventLabels:Record<string,string>={
  'intent.registered':'工作已登记','allocation.requested':'已申请资源','allocation.granted':'资源已分配',
  'decision.committed':'决策已提交','job.started':'任务开始运行','job.succeeded':'任务运行成功',
  'job.failed':'任务运行失败','job.cancelled':'任务已取消','intent.completed':'工作已完成',
  'intent.cancelled':'工作已取消','allocation.released':'租约已释放','intent.lease_settled':'租约已结算',
  'intent.stage_bound':'执行阶段已关联','allocation.preempt_requested':'收到资源收回通知','allocation.preempted':'资源已中断','allocation.resumed':'租约已恢复','job.resumed':'任务已恢复','checkpoint.reported':'已收到保存报告','checkpoint.verified':'保存点已核验','checkpoint.applied':'保存点已应用','contribution.recalled':'贡献者收回资源',
};

export function EventsPage({api,intentId='',showExperiments=true}:{api:ApiClient;intentId?:string;showExperiments?:boolean}) {
  const [cursors,setCursors]=useState<string[]>(['']),[filter,setFilter]=useState('');
  const cursor=cursors[cursors.length-1];
  const query=useQuery(api,intentId?`/x/v1/work-intents/${encodeId(intentId)}`:`/v1/events?limit=50${cursor?`&after=${encodeURIComponent(cursor)}`:''}`);
  const allRows=intentId?list(object(query.data).events):items(query.data);
  const work=detail(query.data);
  const contextName=intentId&&work.intent_id===intentId?workName(work):'';
  const rows=allRows.filter(row=>`${contextName} ${JSON.stringify(row)}`.toLowerCase().includes(filter.toLowerCase()));
  const exportData=intentId?{version:object(query.data).version,now:object(query.data).now,scope:'work',intent_id:intentId,items:allRows}:query.data;
  return <>
    <Panel title={intentId?'这项工作的事件':'事件时间线'} description={intentId?'集中查看这项工作及其关联租约、任务的已提交事件。':'已提交事实按逻辑时间和事件序号展示；当前身份只能读取授权范围内的事件。'} actions={<div className="panel-actions">
      <Refresh onClick={query.refresh} loading={query.loading}/>
      <button type="button" className="button" disabled={query.loading||query.data===null||Boolean(query.error)} onClick={()=>downloadJson(exportData,intentId?`events-${encodeId(intentId)}.json`:'events-current-page.json')}>{intentId?'导出这项工作的事件':'导出当前页真实记录'}</button>
    </div>}>
      {intentId&&<div className="event-context">{contextName&&<strong>{contextName}</strong>}<CopyWorkId id={intentId} scope={api} compact/><a href={workHref(intentId)}>返回这项工作</a><a href="#events">查看全部事件</a></div>}
      <input className="search" aria-label={intentId?'筛选这项工作的事件':'筛选当前页事件'} placeholder={intentId?'筛选工作名称、事件或 ID':'筛选事件、对象 ID 或决定 ID'} value={filter} onChange={e=>setFilter(e.target.value)}/>
      <p className="helper">{intentId?'导出包含服务端返回的这项工作及关联租约、任务事件。':'每页最多 50 条；导出包含当前页全部记录。'} 筛选只改变页面显示，不改变导出范围。</p>
      <details className="raw"><summary>事件范围与技术信息</summary><Metadata data={query.data}/></details>
      <QueryFeedback query={query} empty={!allRows.length}>
        {rows.length?<ol className="timeline">{rows.map(event=><li key={text(event.event_id)}>
          <div className="record-title"><strong>{eventLabels[text(event.event_type)]||text(event.event_type)}</strong><span>{text(event.logical_time)} s · #{text(event.sequence)}</span></div>
          {list(event.state_changes).map((change,index)=><p key={index}>{contextName&&object(change.entity).kind==='intent'&&object(change.entity).entity_id===intentId?contextName:text(object(change.entity).entity_id)}：{text(change.before,'未记录')} → {text(change.after)}</p>)}
          {typeof event.decision_id==='string'&&<p><a className="button" href={decisionHref(event.decision_id,intentId)}>查看这次决定</a></p>}
          <RawData data={event} label="事件技术详情"/>
        </li>)}</ol>:<p className="empty">{intentId?'这项工作中':'当前页'}没有匹配项。</p>}
        {!intentId&&<div className="actions"><button type="button" className="button quiet" disabled={cursors.length===1} onClick={()=>setCursors(values=>values.slice(0,-1))}>上一页</button><span className="helper">第 {cursors.length} 页</span><button type="button" className="button quiet" disabled={!object(query.data).has_more || typeof object(query.data).next_after!=='string'} onClick={()=>setCursors(values=>[...values,object(query.data).next_after as string])}>下一页</button></div>}
      </QueryFeedback>
    </Panel>
    {showExperiments&&<Experiments api={api}/>}
  </>;
}
