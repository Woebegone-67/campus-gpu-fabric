import {useState,type FormEvent} from 'react';
import {ApiClient,detail,encodeId,items,list,object,text} from '../api';
import {useQuery} from '../hooks';
import {downloadJson,Field,Metadata,Panel,QueryFeedback,RawData,Refresh,Status,Table} from '../components/common';

export function EventsPage({api}:{api:ApiClient}) {
  const [cursors,setCursors]=useState<string[]>(['']),[filter,setFilter]=useState('');
  const cursor=cursors[cursors.length-1];
  const query=useQuery(api,`/v1/events?limit=50${cursor?`&after=${encodeURIComponent(cursor)}`:""}`);
  const rows=items(query.data).filter(row=>JSON.stringify(row).toLowerCase().includes(filter.toLowerCase()));
  const [runInput,setRunInput]=useState(''),[runId,setRunId]=useState('');
  const runQuery=useQuery(api,runId?`/x/v1/experiments/${encodeId(runId)}`:null);
  const run=detail(runQuery.data);
  return <>
    <Panel title="事件时间线" description="已提交事实按逻辑时间和事件序号展示；当前身份只能读取授权范围内的事件。" actions={<Refresh onClick={()=>{setCursors(['']);query.refresh();}} loading={query.loading}/>}>
      <input className="search" aria-label="筛选当前页事件" placeholder="筛选当前页事件、对象 ID 或决定 ID" value={filter} onChange={e=>setFilter(e.target.value)}/><Metadata data={query.data}/>
      <QueryFeedback query={query} empty={!items(query.data).length}>
        {rows.length?<ol className="timeline">{rows.map(event=><li key={text(event.event_id)}><div className="record-title"><strong>{text(event.event_type)}</strong><span>{text(event.logical_time)} s · #{text(event.sequence)}</span></div><code>{text(event.event_id)}</code>{list(event.state_changes).map((change,index)=><p key={index}>{text(object(change.entity).entity_id)}：{text(change.before,'未记录')} → {text(change.after)}</p>)}{Boolean(event.decision_id)&&<p>关联决定：<code>{text(event.decision_id)}</code></p>}<RawData data={event}/></li>)}</ol>:<p className="empty">当前页没有匹配项。</p>}
        <div className="actions"><button className="button quiet" disabled={cursors.length===1} onClick={()=>setCursors(values=>values.slice(0,-1))}>上一页</button><span className="helper">第 {cursors.length} 页</span><button className="button quiet" disabled={!object(query.data).has_more || typeof object(query.data).next_after!=='string'} onClick={()=>setCursors(values=>[...values,object(query.data).next_after as string])}>下一页</button><button className="button" onClick={()=>downloadJson(query.data,'events-current-page.json')}>导出当前页真实记录</button></div>
      </QueryFeedback>
    </Panel>
    <Panel title="隔离实验记录" description="按 run ID 查询已登记的实验与原始证据。运行环境、输入和策略身份彼此独立。"><form className="inline-form" onSubmit={(event:FormEvent)=>{event.preventDefault();setRunId(runInput.trim());}}><Field label="Run ID"><input value={runInput} onChange={e=>setRunInput(e.target.value)} required placeholder="run-…"/></Field><button className="button primary">查询实验</button></form><p className="helper">实验提交需接入管理员写入口与已登记场景清单。本版提供读取与导出，不使用运营实例重放或重置。</p></Panel>
    <QueryFeedback query={runQuery}>
      <Panel title={`实验 ${runId}`} actions={<Status value={run.status}/>}><Metadata data={runQuery.data}/><dl className="facts"><div><dt>运行类型</dt><dd>{text(run.run_kind)}</dd></div><div><dt>执行模式</dt><dd>{text(object(run.environment).execution_mode)}</dd></div><div><dt>策略</dt><dd>{text(object(run.policy).name)} · {text(object(run.policy).version)}</dd></div><div><dt>种子 / 窗口</dt><dd>{text(run.seed)} · {text(object(run.window).start)}—{text(object(run.window).end)} s</dd></div></dl><Table rows={list(run.checks)} rowKey={(r,i)=>`${text(r.target_id)}-${i}`} columns={[{label:'目标',render:r=>text(r.target_id)},{label:'结果',render:r=><Status value={r.status}/>},{label:'实际观察',render:r=>text(r.actual,'尚未运行')}]}/><RawData data={runQuery.data} label="输入、输出、版本与异常记录"/><button className="button" onClick={()=>downloadJson(runQuery.data,`experiment-${runId}.json`)}>导出实验记录</button></Panel>
    </QueryFeedback>
  </>;
}
