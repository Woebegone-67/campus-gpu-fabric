import {createContext,useContext,useEffect,useMemo,useState,type ReactNode} from 'react';
import {ApiClient,encodeId,list,object,text,type JsonObject} from '../api';
import {useQuery,type QueryState} from '../hooks';
import {workHref} from '../navigation';
import {QueryFeedback,RawData,Refresh} from './common';

const finite=(value:unknown):number|null=>typeof value==='number'&&Number.isFinite(value)?value:null;
export type RecallState={query:QueryState;borrowed:JsonObject[];owned:JsonObject[];now:number|null;mode:unknown;elapsed:number;fresh:boolean};
export const RecallContext=createContext<RecallState|null>(null);
export const useRecalls=()=>useContext(RecallContext);
export function RecallProvider({api,children}:{api:ApiClient;children:ReactNode}){
  const query=useQuery(api,api.identity.owner?'/x/v1/recalls':null,0,2000);
  const data=object(query.data);
  const anchor=useMemo(()=>performance.now(),[query.data,query.receivedAt]);
  const [tick,setTick]=useState(()=>performance.now());
  const ticking=list(data.borrowed).length>0||list(data.owned).some(row=>row.state!=='ended');
  useEffect(()=>{
    if(!ticking)return;
    const update=()=>{if(document.visibilityState!=='hidden')setTick(performance.now());};
    const timer=window.setInterval(update,500);document.addEventListener('visibilitychange',update);
    return()=>{window.clearInterval(timer);document.removeEventListener('visibilitychange',update);};
  },[ticking]);
  const elapsed=Math.max(0,(tick-anchor)/1000);
  const value={query,borrowed:list(data.borrowed),owned:list(data.owned),now:finite(data.now),mode:data.execution_mode,elapsed,fresh:Boolean(query.data)&&!query.error&&elapsed<=6};
  return <RecallContext.Provider value={value}>{children}</RecallContext.Provider>;
}

export function remainingRecallSeconds(deadline:unknown,now:number|null,mode:unknown,elapsed:number):number|null{
  const due=finite(deadline);
  if(due===null||now===null)return null;
  return Math.max(0,Math.ceil(due-now-(mode==='cpu_reference'?elapsed:0)));
}
function Remaining({deadline,label,atZero='正在确认停止'}:{deadline:unknown;label:string;atZero?:string}){
  const recall=useRecalls();
  if(!recall?.fresh)return <span className="recall-countdown">状态待更新</span>;
  const seconds=remainingRecallSeconds(deadline,recall.now,recall.mode,recall.elapsed);
  if(seconds===null)return null;
  if(seconds===0)return <span className="recall-countdown">{atZero}</span>;
  return <span className="recall-countdown" aria-live="off">{label} <strong>{seconds}</strong> {recall.mode==='logical_simulation'?'逻辑秒':'秒'}</span>;
}
function BorrowedState({row}:{row:JsonObject}){
  const recall=useRecalls();
  if(!recall?.fresh)return <span>状态待更新</span>;
  const decision=object(row.decision);
  if(row.state==='saving'){
    if(decision.phase==='planned')return decision.action==='stop'?<span>准备停止</span>:<Remaining deadline={decision.dispatch_at} label={decision.action==='finish_first'?'最多继续计算':'开始保存前'} atZero="正在更新收回进度"/>;
    return <Remaining deadline={decision.stop_at??row.checkpoint_due} label="保存剩余"/>;
  }
  return <span>{row.state==='stopping'?'正在确认停止':row.state==='attention'?'停止未确认 · 设备保持隔离':row.checkpoint_verified?'进度已保存 · 可恢复工作':'工作已中断'}</span>;
}
const decisionNames:Record<string,string>={finish_first:'优先完成',save_now:'立即保存',save_at:'稍后保存',stop:'直接停止'};
const decisionReasons:Record<string,string>={cold_observation:'进度信息不足，先保存',finish_fits:'预计能在收回前完成',delay_valuable:'多算一段后再保存',save_minimizes_cost:'现在保存更划算',insufficient_window:'剩余时间不足以保存',save_not_worth_cost:'重新计算的代价更小'};
function RecallDecision({row}:{row:JsonObject}){
  const recall=useRecalls(),decision=object(row.decision),name=decisionNames[text(decision.action,'')];
  if(!recall?.fresh||!name||row.state!=='saving'||decision.phase!=='planned')return null;
  const reason=decisionReasons[text(decision.reason,'')];
  return <p className="recall-decision">{name}{reason&&<> · {reason}</>}</p>;
}
export function RecallBanner({currentIntent=''}:{currentIntent?:string}){
  const recall=useRecalls();
  const rows=recall?.borrowed.filter(row=>row.state!=='interrupted'&&(!currentIntent||row.intent_id!==currentIntent))??[];
  if(!rows.length)return null;
  return <aside className="shell-recall" aria-label="设备收回提醒">{rows.slice(0,3).map(row=><div className="shell-recall-row" key={text(row.allocation_id)}><strong>{text(row.display_name,'正在进行的工作')}</strong><span>设备正在收回</span><BorrowedState row={row}/>{typeof row.intent_id==='string'&&<a href={workHref(row.intent_id)}>查看工作 →</a>}</div>)}</aside>;
}
export function WorkRecall({intentId}:{intentId:string}){
  const recall=useRecalls(),rows=recall?.borrowed.filter(row=>row.intent_id===intentId)??[];
  if(!rows.length)return null;
  return <section className="work-recall" aria-label="设备收回进度">{rows.map(row=><div key={text(row.allocation_id)}><div className="recall-state"><strong>{!recall?.fresh?'收回状态待更新':row.state==='interrupted'?'设备已归还':'设备正在收回'}</strong><BorrowedState row={row}/></div><RecallDecision row={row}/>{row.state==='saving'&&recall?.fresh&&<p>到时自动停止，无需手动归还。</p>}</div>)}</section>;
}
export function OwnedRecalls({deviceIds}:{deviceIds:string[]}){
  const recall=useRecalls();
  const rows=recall?.owned.filter(row=>Array.isArray(row.device_ids)&&row.device_ids.some(id=>deviceIds.includes(String(id))))??[];
  if(!rows.length)return null;
  const active=rows.filter(row=>row.state!=='ended');
  const displayed=active.length?active:[...rows].sort((a,b)=>(finite(b.issued_at)??0)-(finite(a.issued_at)??0)).slice(0,1);
  return <div className="owned-recalls" aria-label="设备收回进度">{displayed.map(row=>{
    const decision=object(row.decision);
    const savingLabel=decision.phase==='planned'?(decision.action==='finish_first'?'等待计算完成':decision.action==='stop'?'准备停止':'等待保存'):decision.phase==='saving'?'正在保存':'已通知使用者';
    return <div key={text(row.notice_id)}><div className="recall-state"><strong>{recall?.fresh?({saving:savingLabel,stopping:'正在确认停止',ready:'已收回 · 自用中',ended:'自用已结束',attention:'停止未确认 · 设备保持隔离'} as Record<string,string>)[text(row.state)]||'正在收回':'状态待更新'}</strong>{row.state==='saving'&&<Remaining deadline={row.deadline} label="收回剩余"/>}{row.state==='stopping'&&recall?.fresh&&<span>等待 {text(row.held_count)} 项任务停止</span>}{row.state==='ready'&&recall?.fresh&&<span>自用剩余 {remainingRecallSeconds(row.unavailable_until,recall.now,recall.mode,recall.elapsed)} {recall.mode==='logical_simulation'?'逻辑秒':'秒'}</span>}</div><RecallDecision row={row}/></div>;
  })}</div>;
}
export function ReclaimAudit({api,intentId,visible}:{api:ApiClient;intentId:string;visible:boolean}){
  const [open,setOpen]=useState(false);
  const query=useQuery(api,open&&visible?`/x/v1/work-intents/${encodeId(intentId)}/reclaim-decisions`:null,0,2000);
  const items=list(object(query.data).items);
  return <details className="raw reclaim-audit" onToggle={event=>setOpen(event.currentTarget.open)}><summary>收回决策记录</summary>{open&&visible&&<><Refresh loading={query.loading} onClick={query.refresh}/><QueryFeedback query={query}>{items.length?<><div className="record-list">{items.map((row,index)=><ReclaimDecisionRecord key={`${row.allocation_id}-${row.attempt_id}-${row.planned_at}-${index}`} row={row}/>)}</div><RawData data={query.data} label="查看原始决策记录"/></>:<p className="empty">暂无收回决策</p>}</QueryFeedback></>}</details>;
}

const auditReasons:Record<string,string>={
  finish_fits:'根据当时的进度与速度估计，预计任务可在收回期限内完成，并留出保存与停止的余量，因此选择优先完成。若未按预计完成，仍会按计划触发保存与停止。',
  delay_valuable:'按当时估计，先继续计算一段再保存的综合代价较低，同时仍为保存和停止预留时间。',
  save_minimizes_cost:'按当时估计，立即保存的综合代价较低；比较已考虑剩余计算、保存与重启开销，以及贡献者等待成本。',
  cold_observation:'缺少足够新鲜、可信的进度或速度观测，无法可靠比较继续运行的收益，因此先尝试保存。',
  insufficient_window:'按当时的保存开销估计、停止预留和报告余量，可用窗口不足，因此直接停止。这不表示已经尝试保存并失败。',
  save_not_worth_cost:'当前工作允许从头重算；按当时估计，停止后重算的综合代价更低，因此不新建保存点。',
};
const auditPhases:Record<string,string>={planned:'已制定计划',saving:'保存阶段',stopping:'停止阶段',complete:'流程已结束'};
const auditPolicies:Record<string,string>={return_first:'尽快归还',preserve_work:'尽量保留计算'};
const auditNumber=(value:number)=>value.toLocaleString('zh-CN',{maximumFractionDigits:2});
const auditSeconds=(value:unknown)=>{const number=finite(value);return number===null?'未记录':`${auditNumber(number)} 秒`;};
function ReclaimDecisionRecord({row}:{row:JsonObject}){
  const inputs=object(row.inputs),action=text(row.action,''),reason=text(row.reason,''),phase=text(row.phase,'');
  const now=finite(inputs.now),deadline=finite(inputs.owner_deadline);
  const progress=finite(inputs.progress_steps),total=finite(inputs.total_steps);
  const completed=finite(row.completed_at),stopped=finite(row.stop_confirmed_at);
  const progressLabel=progress!==null&&total!==null&&total>0&&progress>=0&&progress<=total
    ?`${auditNumber(progress/total*100)}%（${progress.toLocaleString('zh-CN')} / ${total.toLocaleString('zh-CN')} 步）`:'未记录有效进度';
  return <article className="record reclaim-decision-record" aria-label="收回选择与理由">
    <div className="record-title"><h3>本次选择：{decisionNames[action]??(action?`未知动作（${action}）`:'未记录动作')}</h3><span>{auditPhases[phase]??(phase?`记录阶段：${phase}`:'阶段未记录')}</span></div>
    <p className="reclaim-reason"><strong>选择理由：</strong>{auditReasons[reason]??(reason?`服务端返回了暂未解释的原因代码：${reason}。可查看原始记录。`:'服务端未记录选择理由。')}</p>
    <dl className="facts">
      <div><dt>决策时可用通知窗口</dt><dd>{now!==null&&deadline!==null?auditSeconds(Math.max(0,deadline-now)):'未记录'}</dd></div>
      <div><dt>决策时观测进度</dt><dd>{progressLabel}</dd></div>
      <div><dt>保存耗时保守估计</dt><dd>{auditSeconds(inputs.checkpoint_cost_upper)}</dd></div>
      <div><dt>停止预留时间</dt><dd>{auditSeconds(inputs.stop_cost_upper)}</dd></div>
      <div><dt>贡献者偏好</dt><dd>{auditPolicies[text(row.return_policy,'')]??text(row.return_policy,'未记录')}</dd></div>
      <div><dt>决策时间（逻辑时钟）</dt><dd>{auditSeconds(row.planned_at)}</dd></div>
    </dl>
    <div className="reclaim-confirmation"><strong>执行确认</strong>{completed!==null&&<p>任务完成已确认 · 逻辑时间 {auditSeconds(completed)}</p>}{stopped!==null&&<p>执行停止／结束已确认 · 逻辑时间 {auditSeconds(stopped)}</p>}{completed===null&&stopped===null&&<p>本条决策尚未记录任务完成或停止确认。</p>}</div>
    <p className="helper">以上依据来自决策当时的记录，耗时为估计；选择保存不代表已保存成功，选择完成不代表任务已经完成。</p>
    <p className="helper">租约 {text(row.allocation_id)} · 执行 {text(row.attempt_id)}</p>
  </article>;
}
