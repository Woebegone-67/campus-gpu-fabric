import {useState} from 'react';
import {ApiClient,detail,encodeId,items,list,object,text} from '../api';
import {useQuery} from '../hooks';
import {decisionHref,workHref} from '../navigation';
import {downloadJson,Panel,QueryFeedback,RawData,Refresh,Status} from '../components/common';
import {CopyWorkId} from '../components/CopyWorkId';
import {workName} from '../workName';

const eventLabels:Record<string,string>={
  'intent.registered':'工作已创建','allocation.requested':'正在申请资源','allocation.granted':'资源已分配',
  'decision.committed':'决定已提交','job.started':'任务开始运行','job.succeeded':'任务运行成功',
  'job.failed':'任务运行失败','job.cancelled':'任务已取消','intent.completed':'工作已完成',
  'intent.cancelled':'工作已取消','allocation.released':'资源已归还','intent.lease_settled':'资源已结算',
  'intent.stage_bound':'工作状态已更新','allocation.preempt_requested':'收到设备收回通知',
  'allocation.preempted':'工作已中断','allocation.resume_requested':'已申请恢复工作',
  'allocation.resumed':'资源已恢复','job.resumed':'任务已恢复','job.restarted':'任务已接续',
  'job.stop_requested':'正在停止任务','checkpoint.reported':'收到保存报告',
  'checkpoint.verified':'保存点已核验','checkpoint.applied':'已从保存点接续',
  'contribution.recalled':'设备已申请收回','clock.advanced':'时间已更新',
};
const eventTime=(value:unknown)=>typeof value==='number'?`${Number(value.toFixed(1))} s`:'—';

export function EventsPage({api,intentId=''}:{api:ApiClient;intentId?:string;showExperiments?:boolean}) {
  const [cursors,setCursors]=useState<string[]>(['']),[filter,setFilter]=useState('');
  const cursor=cursors[cursors.length-1];
  const query=useQuery(api,intentId?`/x/v1/work-intents/${encodeId(intentId)}`:`/v1/events?limit=50${cursor?`&after=${encodeURIComponent(cursor)}`:''}`);
  const allRows=intentId?list(object(query.data).events):items(query.data);
  const work=detail(query.data),contextName=intentId&&work.intent_id===intentId?workName(work):'';
  const rows=allRows.filter(row=>`${contextName} ${eventLabels[text(row.event_type)]??''} ${JSON.stringify(row)}`.toLowerCase().includes(filter.toLowerCase()));
  const exportData=intentId?{version:object(query.data).version,now:object(query.data).now,scope:'work',intent_id:intentId,items:allRows}:query.data;
  const more=object(query.data).has_more===true&&typeof object(query.data).next_after==='string';
  return <Panel title="活动记录" actions={<div className="panel-actions"><Refresh onClick={query.refresh} loading={query.loading}/><button type="button" className="button quiet" disabled={query.loading||query.data===null||Boolean(query.error)} onClick={()=>downloadJson(exportData,intentId?`events-${encodeId(intentId)}.json`:'events-current-page.json')}>{intentId?'导出记录':'导出本页'}</button></div>}>
    {intentId&&<div className="event-context">{contextName&&<strong>{contextName}</strong>}<CopyWorkId id={intentId} scope={api} compact/><a href={workHref(intentId)}>返回工作</a><a href="#events">全部活动</a></div>}
    <QueryFeedback query={query}>
      {allRows.length?<>
        <input className="search" aria-label="筛选活动" placeholder="查找活动" value={filter} onChange={e=>setFilter(e.target.value)}/>
        {rows.length?<ol className="timeline">{rows.map(event=><li key={text(event.event_id)}>
          <div className="record-title"><strong>{eventLabels[text(event.event_type)]||'状态已更新'}</strong><span>{eventTime(event.logical_time)}</span></div>
          {list(event.state_changes).map((change,index)=>{
            const entity=object(change.entity),id=text(entity.entity_id,'');
            return <p key={index}>{!intentId&&entity.kind==='intent'&&id&&<a href={workHref(id)}>{workName({intent_id:id})}</a>}{change.after!=null&&<Status value={change.after}/>}</p>;
          })}
          {typeof event.decision_id==='string'&&<a href={decisionHref(event.decision_id,intentId)}>分配说明</a>}
        </li>)}</ol>:<p className="empty">没有匹配的活动</p>}
      </>:<p className="empty">暂无活动</p>}
      {!intentId&&(more||cursors.length>1)&&<div className="actions">{cursors.length>1&&<button type="button" className="button quiet" onClick={()=>setCursors(values=>values.slice(0,-1))}>上一页</button>}<span>第 {cursors.length} 页</span>{more&&<button type="button" className="button quiet" onClick={()=>setCursors(values=>[...values,object(query.data).next_after as string])}>下一页</button>}</div>}
      {allRows.length>0&&<RawData data={exportData} label="技术详情"/>}
    </QueryFeedback>
  </Panel>;
}
