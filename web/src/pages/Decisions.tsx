import {ApiClient,detail,encodeId,list,object,text} from '../api';
import {useLookupQuery,useQuery} from '../hooks';
import {workHref} from '../navigation';
import {workName} from '../workName';
import {CopyWorkId} from '../components/CopyWorkId';
import {Field,Panel,QueryFeedback,RawData,Refresh,Status,Table} from '../components/common';

const actionLabels:Record<string,string>={allocate:'分配资源',resume:'恢复工作',wait:'等待资源',reject:'不予分配'};
export function DecisionsPage({api,initialId='',workId='',onSelect}:{api:ApiClient;initialId?:string;workId?:string;onSelect?:(id:string)=>void}) {
  const {input,setInput,id,query,submit}=useLookupQuery(api,'/x/v1/decisions',initialId,onSelect);
  const workQuery=useQuery(api,workId?`/x/v1/work-intents/${encodeId(workId)}`:null);
  const plan=detail(query.data),coverage=object(plan.coverage);
  const ownerView=object(query.data).scope==='owner'&&coverage.scope==='partial';
  const receipts=list(object(query.data).commit_receipts),events=list(object(query.data).actual_events);
  const committed=events.length>0;
  const lookup=<form className="inline-form" onSubmit={submit}><Field label="决策 ID"><input value={input} onChange={e=>setInput(e.target.value)} required placeholder="decision-…"/></Field><button className="button primary" disabled={query.loading}>{query.loading?'正在查询…':'查询'}</button></form>;
  return <>
    {workId&&<QueryFeedback query={workQuery}>{detail(workQuery.data).intent_id===workId&&<div className="event-context"><a href={workHref(workId)}>{workName(detail(workQuery.data))}</a><CopyWorkId id={workId} scope={api} compact/></div>}</QueryFeedback>}
    {!id?<Panel title="分配说明">{lookup}</Panel>:<details className="work-disclosure"><summary>查找其他决定</summary>{lookup}</details>}
    <QueryFeedback query={query}>
      <Panel title="分配说明" actions={<Refresh onClick={query.refresh} loading={query.loading}/>}>
        <div className="record-title"><strong>{committed?'决定已提交':plan.selected_candidate_id?'已选择方案 · 尚无执行记录':'暂无选中方案'}</strong>{ownerView&&<span>我的候选</span>}{!ownerView&&coverage.scope==='partial'&&<span>部分候选</span>}</div>
        {!ownerView&&plan.rationale!=null&&<p className="decision-rationale">{text(plan.rationale)}</p>}
        <dl className="facts"><div><dt>分配策略</dt><dd>{text(object(plan.policy).name)}</dd></div><div><dt>检查候选</dt><dd>{text(coverage.examined_count)}{!ownerView&&typeof coverage.domain_size==='number'?` / ${coverage.domain_size}`:''}</dd></div></dl>
        {list(plan.candidates).length?<Table rows={list(plan.candidates)} rowKey={r=>text(r.candidate_id)} columns={[
          {label:'方案',render:r=><>{actionLabels[text(r.action)]??text(r.action)}{r.candidate_id===plan.selected_candidate_id&&<span className="status">已选中</span>}</>},
          {label:'可行性',render:r=><Status value={r.feasibility}/>},
          {label:'限制',render:r=>list(r.violations).length?list(r.violations).map((v,i)=><p key={i}>{text(v.detail,text(v.constraint))}</p>):'—'},
          ...(!ownerView?[{label:'比较分值',render:(r:ReturnType<typeof object>)=>list(r.scores).length?list(r.scores).map((score,i)=><p key={i}>{text(score.name)}：{text(score.value)}</p>):'—'}]:[]),
        ]}/>:<p className="empty">暂无候选记录</p>}
        <RawData data={{...object(query.data),commit_receipts:receipts,actual_events:events}} label="技术详情"/>
      </Panel>
    </QueryFeedback>
  </>;
}
