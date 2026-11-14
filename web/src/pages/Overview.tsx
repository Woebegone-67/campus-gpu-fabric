import {CopyWorkId} from '../components/CopyWorkId';
import {useState} from 'react';
import {ApiClient,idOf,items,list,object,text,versionOf,type JsonObject} from '../api';
import {useQuery} from '../hooks';
import {useAccount} from '../session';
import {modeLabel,useService} from '../service';
import {QueryFeedback,Refresh,Status} from '../components/common';
import {workHref} from '../navigation';
import {workName} from '../workName';
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
  const account=useAccount(),service=useService();
  const canUse=account?.capabilities.includes('use_compute')&&Boolean(account.owner_id);
  const canContribute=account?.capabilities.includes('contribute_resources')&&Boolean(account.owner_id);
  const works=useQuery(api,canUse?'/x/v1/work-intents?limit=25':null);
  const allocations=useQuery(api,canUse?'/v1/allocations?limit=50':null);
  const contributions=useQuery(api,canContribute?'/x/v1/contributions?limit=50':null);
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
  const allAttention=rows.map(row=>workAttention(row,allocationIds(row).flatMap(id=>leaseById.has(id)?[leaseById.get(id)!]:[]),list(object(allocations.data).legal_actions))).filter((entry):entry is Attention=>entry!==null).sort(compareAttention);
  const attention=allAttention.filter(entry=>!dismissedKeys.includes(entry.signature));
  const hiddenCount=allAttention.length-attention.length;
  const attentionTitle=attention.length?'从这几件事继续':hiddenCount?`本次已收起 ${hiddenCount} 项提示`:scopeIncomplete?'提醒仍需核对':'当前加载范围暂无待处理提示';
  const refreshAttention=()=>{works.refresh();allocations.refresh();};
  const current=rows.filter(row=>!terminal.has(text(row.state))).slice(0,5);
  const results=rows.filter(row=>terminal.has(text(row.state))).sort((a,b)=>(logicalTime(object(b.result).recorded_at)??-Infinity)-(logicalTime(object(a.result).recorded_at)??-Infinity)||idOf(a).localeCompare(idOf(b))).slice(0,3);
  return <div className="overview-layout">
    <section className="overview-hero"><p className="eyebrow">CAMPUS FABRIC / OVERVIEW</p><h1 tabIndex={-1}>让计算，继续发生。</h1><p>从这里看见进展，回到工作台继续。</p><div className="hero-actions">{canUse&&<a className="button primary" href="#run">进入我的工作 <span aria-hidden>↗</span></a>}{canContribute&&<a className="button" href="#resources">管理我的设备</a>}</div><span className="overview-mode">{modeLabel(service.mode)}{service.mode==='cpu_reference'?' · 固定程序，模拟 GPU 库存':''}</span></section>
    {!account?.owner_id&&<section className="panel overview-admin"><h2>管理空间已就绪</h2><p>当前账户未绑定个人 Owner。可进入管理工具查看受权的系统数据。</p>{account?.is_administrator&&<a className="button primary" href="#acceptance">打开系统与验收</a>}</section>}
    {canUse&&<>
      <section className="panel overview-attention"><div className="panel-heading"><div><p className="eyebrow">需要关注</p><h2>{attentionTitle}</h2></div><Refresh onClick={refreshAttention} loading={works.loading||allocations.loading}/></div><QueryFeedback query={works}>
        {attention.slice(0,3).map(({row,reason,deadline,signature})=><div className="attention-row" key={signature}><span className="attention-mark" aria-hidden>!</span><div><a href={workHref(idOf(row))}>{reason}</a><p>{workName(row)}<CopyWorkId id={idOf(row)} scope={api} compact/>{deadline!==null?` · 截止 ${deadline} 逻辑秒`:''}</p></div><a className="button quiet" href={workHref(idOf(row))}>查看详情 →</a><button className="link-button attention-dismiss" onClick={()=>setDismissed({scope:scopeKey,keys:[...dismissedKeys,signature]})}>本次收起</button></div>)}
        {attention.length>3&&<p className="helper">当前显示前 3 项，还有 {attention.length-3} 项提示。按停止待确认、收回通知、需处理、可恢复、未归还排序；同类按截止、发生时间与 ID 排序。</p>}
        {hiddenCount>0&&<p className="helper">已收起 {hiddenCount} 项提示。<button className="link-button" onClick={()=>setDismissed({scope:scopeKey,keys:[]})}>展开已收起提示</button></p>}
        {!attention.length&&!hiddenCount&&!scopeIncomplete&&<p className="helper">正常等待无需重复提交；进入工作详情查看已有信息。</p>}
      </QueryFeedback>
      <QueryFeedback query={allocations}><></></QueryFeedback>
      {scopeIncomplete&&<p className="helper" role="status">{!sameEpoch&&workVersion&&allocationVersion?'工作与租约属于不同实例代次，已停止关联。':differentRevision?'工作与租约读取时点不一致，当前提示不代表同一快照。':missingLeases?'部分关联租约不在当前已加载范围内。':'工作或租约尚未完成读取或更新。'} 请进入工作详情核对；未取得的租约不能视为已释放或无需处理。</p>}
      <div className="overview-scope"><span>仅依据已加载的最多 25 项工作与 50 项授权租约；不是全部待办。两份记录独立读取。</span><span>工作：<ReadStamp time={works.receivedAt}/></span><span>租约：<ReadStamp time={allocations.receivedAt}/></span></div></section>
      <section className="panel overview-work"><div className="panel-heading"><div><p className="eyebrow">继续工作</p><h2>正在进行</h2></div><a href="#run">打开工作列表 ↗</a></div><QueryFeedback query={works}>{current.length?<div className="overview-work-list">{current.map(row=><div className="overview-work-item" key={idOf(row)}><a className="overview-work-row" href={workHref(idOf(row))}><span className="work-icon" aria-hidden>⌘</span><span className="work-row-copy"><strong>{workName(row)}</strong><small>{text(object(object(row.specification).job).image,'未提供镜像登记信息')}</small></span><Status value={row.state}/><span aria-hidden>↗</span></a><CopyWorkId id={idOf(row)} scope={api} compact/></div>)}</div>:<div className="empty"><p>当前加载的工作没有进行中的项目。</p><a className="button" href="#run">查看或新建工作</a></div>}</QueryFeedback></section>
    </>}
    {canContribute&&<section className="panel overview-devices"><div className="panel-heading"><div><p className="eyebrow">共享供给</p><h2>我的设备</h2></div><Refresh onClick={contributions.refresh} loading={contributions.loading}/></div><QueryFeedback query={contributions}>{nodes.length?<div className="overview-device-list">{nodes.slice(0,3).map(node=><a href="#resources" className="overview-device" key={idOf(node)}><span className="work-icon" aria-hidden>▦</span><div><strong>{text(node.node_id)}</strong><Status value={node.state}/><p>可用 GPU {text(object(node.available).gpu_count)} / {text(object(node.capacity).gpu_count)}</p>{list(node.devices).filter(d=>!d.profile).map(device=><p key={text(device.device_id)}>{text(device.model)} · {text(device.device_id)} <Status value={device.state}/></p>)}<small>库存记录 · 非实时性能监测</small></div><span aria-hidden>↗</span></a>)}</div>:<div className="empty"><p>当前加载范围内暂无自己的设备。</p><a href="#resources">了解供给接入 →</a></div>}</QueryFeedback><div className="overview-scope"><span>当前已加载的自有节点，最多 50 项。</span><ReadStamp time={contributions.receivedAt}/></div></section>}
    {canUse&&<section className="panel overview-results"><div className="panel-heading"><div><p className="eyebrow">当前范围内最近记录</p><h2>已结束的工作</h2></div><a href="#run">查看列表 ↗</a></div><QueryFeedback query={works}>{results.length?results.map(row=><div className="overview-result-item" key={idOf(row)}><a className="overview-result" href={workHref(idOf(row))}><span>{workName(row)}</span><Status value={row.state}/><span>核对结果与归还 →</span></a><CopyWorkId id={idOf(row)} scope={api} compact/></div>):<p className="helper">当前加载范围内暂无已结束工作。</p>}</QueryFeedback><p className="helper">按已加载的最多 25 项工作中的结果记录时间倒序，展示最多 3 项；未记录时间的排在最后。工作结束和租约归还是独立状态；工件入口以详情中的真实记录为准。</p></section>}
  </div>;
}
