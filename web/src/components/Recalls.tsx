import {createContext,useContext,useEffect,useMemo,useState,type ReactNode} from 'react';
import {ApiClient,encodeId,list,object,text,type JsonObject} from '../api';
import {useQuery,type QueryState} from '../hooks';
import {workHref} from '../navigation';
import {QueryFeedback,Refresh} from './common';

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
  return <details className="raw" onToggle={event=>setOpen(event.currentTarget.open)}><summary>收回决策记录</summary>{open&&visible&&<><Refresh loading={query.loading} onClick={query.refresh}/><QueryFeedback query={query}>{list(object(query.data).items).length?<pre>{JSON.stringify(query.data,null,2)}</pre>:<p className="empty">暂无收回决策</p>}</QueryFeedback></>}</details>;
}
