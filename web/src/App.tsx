import {useCallback,useEffect,useMemo,useState} from 'react';
import {ApiClient,object,type Identity} from './api';
import {IdentityPicker} from './components/IdentityPicker';
import {useQuery} from './hooks';
import {RunPage} from './pages/Run';
import {ResourcesPage} from './pages/Resources';
import {ContinuityPage} from './pages/Continuity';
import {DecisionsPage} from './pages/Decisions';
import {EventsPage} from './pages/Events';
import {AcceptancePage} from './pages/Acceptance';

const workspaces=[
  {id:'run',name:'运行工作台',description:'提交工作，处理等待与运行，核对任务和租约。'},
  {id:'resources',name:'资源与贡献',description:'查看兼容资源，管理自有供给与收回。'},
  {id:'continuity',name:'任务连续性',description:'同一项工作，跨越中断、恢复与结果结算。'},
  {id:'decisions',name:'决策解释',description:'查看当时的候选、约束和实际提交。'},
  {id:'events',name:'事件与实验',description:'追踪已提交事件，检查独立实验与证据。'},
  {id:'acceptance',name:'系统验收',description:'区分已经验证、尚未运行和仍待接入的能力。'},
];
function readLocation(){const [section,search='']=location.hash.slice(1).split('?');return {section:workspaces.some(w=>w.id===section)?section:'run',intent:new URLSearchParams(search).get('intent')||''};}
export default function App(){
  const [identity,setIdentity]=useState<Identity>({owner:'alice',token:''});
  const [identityOpen,setIdentityOpen]=useState(false),[mutations,setMutations]=useState(0);
  const onMutating=useCallback((amount:number)=>setMutations(value=>Math.max(0,value+amount)),[]);
  const api=useMemo(()=>new ApiClient(identity,onMutating),[identity,onMutating]);
  const [route,setRoute]=useState(readLocation);
  useEffect(()=>{const change=()=>setRoute(readLocation());window.addEventListener('hashchange',change);return()=>window.removeEventListener('hashchange',change);},[]);
  const health=useQuery(api,'/healthz');
  const selected=workspaces.find(w=>w.id===route.section)!;
  const openIntent=(id:string)=>{location.hash=`continuity?intent=${encodeURIComponent(id)}`;};
  const isOnline=health.data!==null&&!health.error;
  return <div className="app">
    <aside className="sidebar"><a className="wordmark" href="#run"><span>织算</span><small>CAMPUS FABRIC</small></a><nav aria-label="工作区">{workspaces.map((workspace,index)=><a key={workspace.id} href={`#${workspace.id}`} aria-current={route.section===workspace.id?'page':undefined}><span className="number">0{index+1}</span>{workspace.name}</a>)}</nav><footer>临时共享 · 持续接续<br/>B · 服务决策与体验</footer></aside>
    <main><header className="topbar"><div><span className={`connection-dot ${isOnline?'online':''}`}/><span role="status">{health.loading?'正在连接服务':isOnline?'服务在线':'服务未连接'}</span><button className="link-button" onClick={health.refresh} disabled={health.loading}>重新检查</button></div><button className="button quiet" onClick={()=>setIdentityOpen(!identityOpen)}>当前身份：{identity.profile || identity.owner || '未设置'}</button></header>
      <div hidden={!identityOpen}><IdentityPicker identity={identity} onApply={setIdentity} busy={mutations>0}/></div>
      <section className="intro"><p className="eyebrow">CAMPUS COMPUTE / {selected.id.toUpperCase()}</p><h1>{selected.name}</h1><p>{selected.description}</p></section>
      {isOnline&&object(health.data).control_plane_ready===false&&<div className="service-banner" role="status"><strong>业务内核待接入</strong><span>当前服务已启动，资源账本与执行尚未就绪。界面会显示真实接口回执，不预填运行结果。</span></div>}
      {health.error&&<div className="service-banner"><strong>无法连接 API</strong><span>{health.error.message} 请检查本地服务和代理地址。</span></div>}
      <div key={`${identity.owner}\0${identity.token}`} className="workspace">
        {route.section==='run'&&<RunPage api={api} onOpen={openIntent}/>}
        {route.section==='resources'&&<ResourcesPage api={api}/>}
        {route.section==='continuity'&&<ContinuityPage key={route.intent} api={api} initialId={route.intent}/>}
        {route.section==='decisions'&&<DecisionsPage api={api}/>}
        {route.section==='events'&&<EventsPage api={api}/>}
        {route.section==='acceptance'&&<AcceptancePage api={api} showAdminClock={identity.profile==='admin' && Boolean(identity.token)}/>}
      </div><footer className="page-footer">当前为逻辑模拟 · 所有业务状态以服务端为准</footer>
    </main>
  </div>;
}
