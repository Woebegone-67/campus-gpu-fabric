import {useState} from 'react';
import {ApiClient,idOf,items,list,object,text,versionOf,type JsonObject} from '../api';
import {useQuery} from '../hooks';
import {useAccount} from '../session';
import {QueryFeedback,Refresh,Status} from '../components/common';
import {workHref} from '../navigation';
import {workName} from '../workName';
import {useRecalls} from '../components/Recalls';
export {shortWorkId} from '../workName';

const terminal=new Set(['completed','failed','cancelled']);
const holding=new Set(['Allocated','Running','PreemptRequested']);
const logicalTime=(value:unknown):number|null=>typeof value==='number'&&Number.isFinite(value)?value:null;
const allocationIds=(work:JsonObject):string[]=>Array.isArray(work.allocation_ids)?work.allocation_ids.filter((id):id is string=>typeof id==='string'):[];
type Attention={row:JsonObject;reason:string;severity:number;deadline:number|null;occurred:number|null;signature:string};
const compareTime=(a:number|null,b:number|null)=>(a??Infinity)-(b??Infinity)||0;
const compareAttention=(a:Attention,b:Attention)=>a.severity-b.severity||compareTime(a.deadline,b.deadline)||compareTime(a.occurred,b.occurred)||idOf(a.row).localeCompare(idOf(b.row))||a.signature.localeCompare(b.signature);

/** Only explicitly associated, authorized lease records may supply lease facts. */
function workAttention(work:JsonObject,allocations:JsonObject[],legal:JsonObject[]):Attention|null {
  const candidates:Attention[]=[];
  const add=(reason:string,severity:number,allocation?:JsonObject)=>{
    const preemption=object(allocation?.preemption);
    const deadline=logicalTime(preemption.deadline)??logicalTime(allocation?.expires_at_logical);
    const occurred=logicalTime(preemption.requested_at)??logicalTime(object(work.result).recorded_at)??logicalTime(allocation?.created_at_logical)??logicalTime(work.first_arrival_at);
    candidates.push({row:work,reason,severity,deadline,occurred,signature:JSON.stringify([idOf(work),reason,allocation? idOf(allocation):null,deadline,occurred])});
  };
  if(work.state==='manual_action')add('这项工作需要处理',2);
  for(const allocation of allocations){
    if(allocation.pending_terminal)add('资源停止仍待确认',0,allocation);
    else if(allocation.state==='PreemptRequested')add('收到资源收回通知',1,allocation);
    else if(allocation.state==='Preempted'&&legal.some(action=>action.operation==='allocation.resume'&&object(action.entity).kind==='allocation'&&object(action.entity).entity_id===idOf(allocation)))add('工作已中断，可进入详情恢复',3,allocation);
    else if(terminal.has(text(work.state))&&holding.has(text(allocation.state)))add('工作已结束，资源尚未归还',4,allocation);
  }
  return candidates.sort(compareAttention)[0]??null;
}
function ReadStamp({time}:{time:number|null}){return <small>{time?`上次读取 ${new Date(time).toLocaleTimeString()}`:'尚未读取'}</small>;}
export function OverviewPage({api}:{api:ApiClient}) {
  const account=useAccount(),recalls=useRecalls();
  const canUse=account?.capabilities.includes('use_compute')&&Boolean(account.owner_id);
  const canContribute=account?.capabilities.includes('contribute_resources')&&Boolean(account.owner_id);
  const admin=useQuery(api,account?.is_administrator?'/x/v1/admin/overview':null,0,2000);
  const works=useQuery(api,canUse?'/x/v1/work-intents?limit=25':null,0,2000);
  const allocations=useQuery(api,canUse?'/v1/allocations?limit=50':null,0,2000);
  const contributions=useQuery(api,canContribute?'/x/v1/contributions?limit=50':null,0,2000);
  const rows=items(works.data).filter(row=>row.owner_id===account?.owner_id),nodes=items(contributions.data).filter(node=>node.contributor_owner_id===account?.owner_id);
  const workVersion=versionOf(works.data),allocationVersion=versionOf(allocations.data);
  const sameEpoch=Boolean(workVersion&&allocationVersion&&workVersion.epoch===allocationVersion.epoch);
  const differentRevision=sameEpoch&&workVersion!.revision!==allocationVersion!.revision;
  const leaseById=new Map(sameEpoch?items(allocations.data).filter(lease=>lease.owner_id===account?.owner_id).map(lease=>[idOf(lease),lease]):[]);
  const missingLeases=rows.some(row=>allocationIds(row).some(id=>!leaseById.has(id)));
  const scopeIncomplete=!works.data||!allocations.data||!sameEpoch||missingLeases||Boolean(works.error||allocations.error)||works.loading||allocations.loading||differentRevision;
  const scopeKey=`${api.identity.owner}\0${api.identity.token}\0${workVersion?.epoch??''}`;
  const [dismissed,setDismissed]=useState<{scope:string;keys:string[]}>({scope:'',keys:[]});
  const dismissedKeys=dismissed.scope===scopeKey?dismissed.keys:[];
  const globalRecallIds=new Set(recalls?.borrowed.filter(row=>row.state!=='interrupted').map(row=>row.intent_id)??[]);
  const allAttention=rows.filter(row=>!globalRecallIds.has(idOf(row))).map(row=>workAttention(row,allocationIds(row).flatMap(id=>leaseById.has(id)?[leaseById.get(id)!]:[]),list(object(allocations.data).legal_actions))).filter((entry):entry is Attention=>entry!==null).sort(compareAttention);
  const attention=allAttention.filter(entry=>!dismissedKeys.includes(entry.signature));
  const hiddenCount=allAttention.length-attention.length;
  const [showAll,setShowAll]=useState(false);
  const attentionIds=new Set(attention.map(entry=>idOf(entry.row)));
  const current=rows.filter(row=>!terminal.has(text(row.state))&&!attentionIds.has(idOf(row))).slice(0,5);
  const results=rows.filter(row=>terminal.has(text(row.state))&&!attentionIds.has(idOf(row))).sort((a,b)=>(logicalTime(object(b.result).recorded_at)??-Infinity)-(logicalTime(object(a.result).recorded_at)??-Infinity)||idOf(a).localeCompare(idOf(b))).slice(0,3);
  const refresh=()=>{works.refresh();allocations.refresh();contributions.refresh();admin.refresh();};
  const loading=works.loading||allocations.loading||contributions.loading||admin.loading;
  const adminData=object(admin.data),adminStates=object(adminData.work_by_state),totals=object(object(adminData.capacity).totals);
  const attentionVisible=attention.length>0||hiddenCount>0||scopeIncomplete;
  const scopeMessage=!sameEpoch&&workVersion&&allocationVersion?'实例已更新，请刷新。':differentRevision?'状态正在同步，请刷新。':missingLeases?'部分资源状态未载入。':'部分状态未更新。';
  return <div className="overview-layout">
    <header className="overview-heading"><h1 tabIndex={-1}>{account?.is_administrator?'系统概览':'总览'}</h1><div className="actions"><Refresh onClick={refresh} loading={loading}/>{canUse&&<a className="button primary" href="#run?new=1">新建工作</a>}</div></header>
    {account?.is_administrator&&<section className="panel overview-admin"><div className="panel-heading"><h2>资源池</h2><a href="#acceptance">系统管理 →</a></div><QueryFeedback query={admin}><dl className="overview-metrics"><div><dt>运行中任务</dt><dd>{text(adminData.running_jobs)}</dd></div><div><dt>等待中的工作</dt><dd>{text(adminStates.waiting,admin.data?'0':'—')}</dd></div><div><dt>可用 GPU</dt><dd>{text(object(totals.available).gpu_count)}</dd></div><div><dt>全部工作</dt><dd>{text(object(adminData.counts).work_intents)}</dd></div></dl>{typeof adminStates.manual_action==='number'&&adminStates.manual_action>0&&<a className="admin-attention" href="#acceptance">{adminStates.manual_action} 项工作需要处理 →</a>}</QueryFeedback></section>}
    {canUse&&<>
      {attentionVisible&&<section className="panel overview-attention"><div className="panel-heading"><h2>{attention.length?`需要处理 · ${attention.length}`:hiddenCount?`已收起 ${hiddenCount} 项提示`:'状态待更新'}</h2>{hiddenCount>0&&<button className="link-button" onClick={()=>setDismissed({scope:scopeKey,keys:[]})}>展开已收起提示</button>}</div>
        <QueryFeedback query={works}>{(showAll?attention:attention.slice(0,3)).map(({row,reason,deadline,signature})=><div className="attention-row" key={signature}><span className="attention-mark" aria-hidden>!</span><div><a href={workHref(idOf(row))}><strong>{workName(row)}</strong><span className="attention-reason">{reason}</span></a>{deadline!==null&&<p>截止 {deadline} 逻辑秒</p>}</div><button className="link-button attention-dismiss" aria-label={`收起 ${workName(row)} 的提示`} onClick={()=>setDismissed({scope:scopeKey,keys:[...dismissedKeys,signature]})}>本次收起</button></div>)}</QueryFeedback>
        {attention.length>3&&<button className="link-button attention-more" onClick={()=>setShowAll(value=>!value)}>{showAll?'收起列表':`查看全部 ${attention.length} 项`}</button>}
        <QueryFeedback query={allocations}><></></QueryFeedback>
        {scopeIncomplete&&<p className="overview-sync" role="status">{scopeMessage}</p>}
      </section>}
      {current.length>0&&<section className="panel overview-work"><div className="panel-heading"><h2>进行中</h2><a href="#run">我的工作 →</a></div><div className="overview-work-list">{current.map(row=><a className="overview-work-row" href={workHref(idOf(row))} key={idOf(row)}><span className="work-row-copy"><strong>{workName(row)}</strong></span><Status value={row.state}/><span aria-hidden>→</span></a>)}</div></section>}
      {results.length>0&&<section className="panel overview-results"><div className="panel-heading"><h2>最近结束</h2><a href="#run">全部工作 →</a></div>{results.map(row=><a className="overview-result" href={workHref(idOf(row))} key={idOf(row)}><strong>{workName(row)}</strong><Status value={row.state}/><span aria-hidden>→</span></a>)}</section>}
      {Boolean(works.data)&&!works.error&&rows.length===0&&<section className="overview-empty"><h2>还没有工作</h2></section>}
      <div className="overview-scope"><span>最近 25 项工作</span>{!attentionVisible&&rows.length>0&&<span>暂无待处理事项</span>}<ReadStamp time={works.receivedAt}/></div>
    </>}
    {canContribute&&nodes.length>0&&<section className="panel overview-devices"><div className="panel-heading"><h2>我的设备</h2><a href="#resources">管理设备 →</a></div><div className="overview-device-list">{nodes.slice(0,3).map(node=><a href="#resources" className="overview-device" key={idOf(node)}><div><strong>{text(node.node_id)}</strong><Status value={node.state}/><p>可用 GPU {text(object(node.available).gpu_count)} / {text(object(node.capacity).gpu_count)}</p>{list(node.devices).filter(device=>!device.profile).map(device=><small className="overview-device-state" key={text(device.device_id)}>{text(device.model)} · {text(device.device_id)} <Status value={device.state}/></small>)}</div><span aria-hidden>→</span></a>)}</div></section>}
    {canContribute&&(contributions.error||contributions.loading)&&<div className="overview-device-feedback"><QueryFeedback query={contributions}><></></QueryFeedback></div>}
    {canContribute&&!canUse&&!account?.is_administrator&&Boolean(contributions.data)&&!contributions.error&&!nodes.length&&<section className="overview-empty"><h2>尚未共享设备</h2><a href="#resources">我的设备 →</a></section>}
  </div>;
}
