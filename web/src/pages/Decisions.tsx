import {ApiClient,detail,list,object,text} from '../api';
import {useLookupQuery} from '../hooks';
import {workHref} from '../navigation';
import {Field,LookupStatus,Metadata,Panel,QueryFeedback,RawData,Refresh,Status,Table} from '../components/common';

export function DecisionsPage({api,initialId='',workId='',onSelect}:{api:ApiClient;initialId?:string;workId?:string;onSelect?:(id:string)=>void}) {
  const {input,setInput,id,query,submit}=useLookupQuery(api,'/x/v1/decisions',initialId,onSelect);
  const plan=detail(query.data), coverage=object(plan.coverage);
  const ownerView=object(query.data).scope==='owner'&&coverage.scope==='partial';
  const receipts=list(object(query.data).commit_receipts), events=list(object(query.data).actual_events);
  return <>
    <Panel title="追问一次决定" description="读取决策时的候选、排除约束与实际提交记录。计划选中不代表已经执行。" actions={workId&&<a className="button quiet" href={workHref(workId)}>返回这项工作</a>}><form className="inline-form" onSubmit={submit}><Field label="决策 ID"><input value={input} onChange={e=>setInput(e.target.value)} required placeholder="decision-…"/></Field><button className="button primary" disabled={query.loading}>{query.loading?'正在查询…':'查询决定'}</button>{id&&<Refresh onClick={query.refresh} loading={query.loading}/>}</form><LookupStatus query={query}/>{!id&&<p className="empty">从工作详情点击“查看关联决策”，或在事件中点击“查看这次决定”，即可直接打开。</p>}</Panel>
    <QueryFeedback query={query}>
      <Panel title="决策依据"><Metadata data={query.data}/><p className="decision-rationale">{ownerView?(plan.selected_candidate_id?'决策记录选中了你的需求；实际是否提交，请查看下方的已提交事件。':'当前授权视图未显示你的需求被选中，这不代表你的需求不可行。'):text(plan.rationale,'服务端未记录说明。')}</p><dl className="facts"><div><dt>策略</dt><dd>{text(object(plan.policy).name)} · {text(object(plan.policy).version)}</dd></div><div><dt>决策时间</dt><dd>{text(plan.decision_at)} 逻辑秒</dd></div><div><dt>计划选中的候选</dt><dd>{text(plan.selected_candidate_id,ownerView?'当前视图未显示':'未选择')}</dd></div><div><dt>同分规则</dt><dd>{ownerView?'其他用户的比较细节不公开':text(plan.tie_break_rule)}</dd></div></dl>
        <div className={`feedback ${coverage.scope==='partial'?'warning':''}`}><strong>{ownerView?'仅展示你的候选':coverage.scope==='complete'?'声明覆盖完整候选域':'搜索范围有限'}</strong>{ownerView?<p>已检查 {text(coverage.examined_count)} 个属于你的候选记录。其他用户的需求和比较分值不公开；这个视图不能代表完整搜索范围或全局排名。</p>:<><p>{text(coverage.domain_description)} · 已检查 {text(coverage.examined_count)} / {text(coverage.domain_size,'未知')} 个候选。</p>{coverage.scope==='partial'&&<p>{text(coverage.truncation_reason)}。未检查部分不能据此认定不可行。</p>}</>}</div>
        <Table rows={list(plan.candidates)} rowKey={r=>text(r.candidate_id)} columns={[{label:'候选 / 动作',render:r=><><code>{text(r.candidate_id)}</code><p>{text(r.action)}</p></>},{label:'可行性',render:r=><Status value={r.feasibility}/>},{label:'排除约束',render:r=><>{list(r.violations).map((v,i)=><p key={i}>{text(v.constraint)}：{text(v.detail)}</p>)}</>},{label:'比较依据',render:r=>list(r.scores).length?<>{list(r.scores).map((score,i)=><p key={i}>{text(score.name)}：{text(score.value)} · {text(score.explanation)}</p>)}</>:<span className="helper">{ownerView?'比较分值未公开':'未记录比较分值'}</span>}]}/>
      </Panel>
      <Panel title="实际提交与影响">{receipts.length||events.length?<><p>以下为关联回执和已提交事件。只有这些记录能够确认实际发生的动作。</p><RawData data={{commit_receipts:receipts,actual_events:events}} label="展开实际记录"/></>:<p className="empty">当前读取结果没有实际提交回执或事件，不能将这个计划标记为已执行。</p>}</Panel><RawData data={query.data}/>
    </QueryFeedback>
  </>;
}
