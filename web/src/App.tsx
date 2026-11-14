import {useCallback,useEffect,useLayoutEffect,useMemo,useRef,useState} from 'react';
import {ApiClient,object,type Identity} from './api';
import {IdentityPicker} from './components/IdentityPicker';
import {QueryActivityContext,QueryEpochContext,useQuery} from './hooks';
import {RunPage} from './pages/Run';
import {ResourcesPage} from './pages/Resources';
import {ContinuityPage} from './pages/Continuity';
import {DecisionsPage} from './pages/Decisions';
import {EventsPage} from './pages/Events';
import {AcceptancePage} from './pages/Acceptance';
import {OverviewPage} from './pages/Overview';
import {decisionHref,workHref} from './navigation';
import {ServiceContext,serviceInfo,modeLabel,useService} from './service';
import {AccountContext,readAccountSession,type AccountSession} from './session';
import {StarwaveSurface,StarwaveMotionControl} from './visual/starwave/StarwaveSurface';
import {SpaceGesture} from './components/SpaceGesture';
import {ErrorNotice} from './components/common';

const sections=[
  {id:'run',name:'我的工作',description:'查看工作、提交需求，以及管理资源归还。'},
  {id:'resources',name:'我的设备',description:'查看共享供给，管理自己的设备与收回。'},
  {id:'continuity',name:'工作详情',description:'结果、资源归还与各次执行，都留在同一项工作里。'},
  {id:'decisions',name:'分配说明',description:'核对当时的候选、约束与实际提交。'},
  {id:'events',name:'活动记录',description:'查看授权范围内已经发生的事件。'},
  {id:'acceptance',name:'系统与验收',description:'检查当前构建、证据与受权管理能力。'},
];
function parseHash(hash:string){const [section,search='']=hash.slice(1).split('?');const params=new URLSearchParams(search);return {section:sections.some(w=>w.id===section)?section:'overview',intent:params.get('intent')||'',decision:params.get('decision')||''};}
export function readLocation(){return parseHash(location.hash);}
function workbenchTarget(account:AccountSession|null,hash:string){
  const section=parseHash(hash).section;
  const permitted=section==='acceptance'?account?.is_administrator:section==='resources'?Boolean(account?.capabilities.length):account?.capabilities.includes('use_compute');
  if(permitted)return hash;
  return account?.owner_id&&account.capabilities.includes('use_compute')?'#run':account?.owner_id&&account.capabilities.includes('contribute_resources')?'#resources':account?.is_administrator?'#acceptance':'#run';
}
export default function App(){
  const [starwaveEnabled]=useState(()=>new URLSearchParams(location.search).get('visual')!=='classic');
  const [identity,setIdentity]=useState<Identity>({owner:'',token:''});
  const [identityOpen,setIdentityOpen]=useState(false),[mutations,setMutations]=useState(0),[identityGeneration,setIdentityGeneration]=useState(0);
  const writeIdentity=useRef<Identity|null>(null);
  const onMutating=useCallback((amount:number)=>setMutations(value=>Math.max(0,value+amount)),[]);
  const api=useMemo(()=>new ApiClient(identity,onMutating,()=>writeIdentity.current===identity),[identity,onMutating]);
  const [route,setRoute]=useState(readLocation);
  const lastWorkbench=useRef(location.hash&&route.section!=='overview'?location.hash:'#run');
  const [gestureEnabled,setGestureEnabled]=useState(false),[preview,setPreview]=useState(0);
  const [settingsOpen,setSettingsOpen]=useState(false);
  const [epoch,setEpoch]=useState(0),epochRef=useRef(0);
  const acceptEpoch=useCallback((next:number)=>{if(next<epochRef.current)return false;if(next>epochRef.current){epochRef.current=next;setEpoch(next);setPreview(0);}return true;},[]);
  const health=useQuery(api,'/healthz'),version=useQuery(api,'/v1/version');
  const session=useQuery(api,identity.token||identity.owner?'/x/v1/session':null);
  const account=readAccountSession(session.data);
  const accountReady=Boolean(account)&&!session.error&&!session.loading;
  writeIdentity.current=accountReady?identity:null;
  const service=serviceInfo(version.data),isOnline=health.data!==null&&!health.error;
  const space=route.section==='overview'?'overview':'workbench';
  useEffect(()=>{const change=()=>{const next=readLocation();if(next.section!=='overview')lastWorkbench.current=location.hash;setRoute(next);setPreview(0);};window.addEventListener('hashchange',change);return()=>window.removeEventListener('hashchange',change);},[]);
  function applyIdentity(next:Identity){if(mutations)return;setIdentity(next);setIdentityGeneration(n=>n+1);setPreview(0);setGestureEnabled(false);lastWorkbench.current='#run';epochRef.current=0;setEpoch(0);}
  const workbenchHash=workbenchTarget(account,lastWorkbench.current);
  function navigateSpace(target:'overview'|'workbench'){setPreview(0);const hash=target==='overview'?'#overview':workbenchHash;if(location.hash!==hash)location.hash=hash;}
  const refresh=()=>{health.refresh();version.refresh();session.refresh();};
  const canUse=account?.capabilities.includes('use_compute');
  const canContribute=account?.capabilities.includes('contribute_resources');
  return <div className={`app dual-space${starwaveEnabled?' cp4-starwave':''}`} data-space={space}>
    <aside className="sidebar"><a className="wordmark" href="#overview"><span>织算<span className="brand-dot">✦</span></span><small>CAMPUS FABRIC</small></a>
      <nav aria-label="空间"><a href="#overview" aria-current={space==='overview'?'page':undefined}><span aria-hidden>◈</span>总览</a><a href={workbenchHash} aria-current={space==='workbench'?'page':undefined}><span aria-hidden>▦</span>工作台</a></nav>
      <div className="sidebar-separator"/>
      {account&&<nav aria-label="工作台导航"><p className="nav-group-label">工作空间</p>{canUse&&<a href="#run" aria-current={route.section==='run'?'page':undefined}>我的工作</a>}{(canUse||canContribute)&&<a href="#resources" aria-current={route.section==='resources'?'page':undefined}>{canContribute?'我的设备':'可见资源'}</a>}{canUse&&<a href="#events" aria-current={route.section==='events'?'page':undefined}>活动记录</a>}{account.is_administrator&&<><p className="nav-group-label">管理</p><a href="#events?admin=experiments">实验与报告</a><a href="#acceptance" aria-current={route.section==='acceptance'?'page':undefined}>系统与验收</a></>}</nav>}
      <footer><span className={`connection-dot ${isOnline?'online':''}`}/>{isOnline?'服务在线':'服务未连接'}<br/><span>临时共享 · 持续接续</span></footer>
    </aside>
    <StarwaveSurface enabled={starwaveEnabled&&space==='overview'}>
      <header className="topbar"><div className="space-breadcrumb">{space==='overview'?'总览':'工作台'} <span>/</span> <span>{space==='overview'?'你的计算空间':sections.find(s=>s.id===route.section)?.name}</span></div><div className="topbar-controls"><StarwaveMotionControl/><button className="button quiet" aria-expanded={settingsOpen} onClick={()=>setSettingsOpen(value=>!value)}>显示设置</button><button className="button account-trigger" aria-expanded={identityOpen} onClick={()=>setIdentityOpen(value=>!value)}><span className="account-avatar" aria-hidden>{(account?.owner_id||identity.profile||'?').slice(0,1).toUpperCase()}</span>{account?account.owner_id||'管理账户':'连接账户'}</button></div></header>
      <div className="shell-popovers" data-space-gesture-blocked={identityOpen||settingsOpen?'true':'false'}>
        <div hidden={!identityOpen}><div className="popover-heading"><strong>连接本地账户</strong><button className="button quiet" onClick={()=>setIdentityOpen(false)}>关闭账户面板</button></div><IdentityPicker identity={identity} onApply={applyIdentity} busy={mutations>0}/></div>
        {settingsOpen&&<section className="display-settings"><label><input type="checkbox" checked={gestureEnabled} onChange={event=>setGestureEnabled(event.target.checked)}/>试用页头横向手势</label><p className="helper">仅在页头空白区域生效。默认关闭；鼠标与键盘可直接使用空间导航，触控板实机兼容性尚待验证。</p><button className="button quiet" onClick={()=>setSettingsOpen(false)}>关闭显示设置</button></section>}
      </div>
      <SpaceGesture activeSpace={space} enabled={gestureEnabled} blocked={identityOpen||settingsOpen||mutations>0} scopeKey={`${identityGeneration}:${epoch}:${route.section}:${route.intent}:${route.decision}`} onNavigate={navigateSpace} onPreview={setPreview} className="space-gesture-header"><div className="space-switch" data-space-gesture-ignore><button className={space==='overview'?'selected':''} aria-pressed={space==='overview'} onClick={()=>navigateSpace('overview')}>总览</button><button className={space==='workbench'?'selected':''} aria-pressed={space==='workbench'} onClick={()=>navigateSpace('workbench')}>工作台</button></div><span className="gesture-hint">{gestureEnabled?'页头空白处可横向切换空间':'一个账户，连接计算与共享'}</span></SpaceGesture>
      {health.error&&<div className="shell-notice" role="status">服务暂未连接。已填内容保留，可检查本地服务后<button className="link-button" onClick={refresh}>重新连接</button>。</div>}
      {isOnline&&object(health.data).control_plane_ready===false&&<div className="shell-notice">业务内核尚未就绪，暂不能处理工作。</div>}
      {account&&session.error&&<div className="shell-notice" role="status">账户连接暂未确认，保留当前内容并暂停提交。<button className="link-button" onClick={session.refresh}>重新检查账户</button></div>}
      {!account?<div className="account-gate"><p className="eyebrow">YOUR COMPUTE SPACE</p><h1>连接账户，开始工作。</h1><p>{session.loading?'正在确认账户能力…':identity.token||identity.owner?'账户能力尚未确认。连接恢复后可重新检查。':'导入本地身份文件，查看你的工作与共享设备。'}</p>{session.error&&<details><summary>查看连接详情</summary><ErrorNotice error={session.error}/></details>}<div className="actions"><button className="button primary" onClick={()=>setIdentityOpen(true)}>导入或切换账户</button>{Boolean(identity.token||identity.owner)&&<button className="button" onClick={refresh}>重新检查账户</button>}</div><p className="helper">凭证只保留在当前页面内存，账户权限由服务端确认。</p></div>:
      <ServiceContext.Provider value={service}><AccountContext.Provider value={account}><QueryEpochContext.Provider value={acceptEpoch}><Workspace key={`${identityGeneration}:${epoch}`} api={api} account={account} route={route} workRoute={space==='overview'?parseHash(workbenchHash):route} preview={preview} accountReady={accountReady}/></QueryEpochContext.Provider></AccountContext.Provider></ServiceContext.Provider>}
    </StarwaveSurface>
  </div>;
}
function Workspace({api,account,route:activeRoute,workRoute:route,preview,accountReady}:{api:ApiClient;account:AccountSession;route:ReturnType<typeof readLocation>;workRoute:ReturnType<typeof readLocation>;preview:number;accountReady:boolean}) {
  const service=useService();
  const overview=activeRoute.section==='overview',canUse=account.capabilities.includes('use_compute'),canContribute=account.capabilities.includes('contribute_resources');
  const [visitedRun,setVisitedRun]=useState(route.section==='run');
  useEffect(()=>{if(route.section==='run')setVisitedRun(true);},[route.section]);
  const workScroll=useRef<HTMLDivElement>(null),overviewScroll=useRef<HTMLDivElement>(null),overviewPosition=useRef(0),positions=useRef<Record<string,number>>({});
  const routeKey=`${route.section}:${route.intent}:${route.decision}`;
  useLayoutEffect(()=>{const element=overview?overviewScroll.current:workScroll.current;if(element)element.scrollTop=overview?overviewPosition.current:positions.current[routeKey]||0;},[routeKey,overview]);
  const first=useRef(true);
  useEffect(()=>{if(first.current){first.current=false;return;}const id=requestAnimationFrame(()=>{const active=document.querySelector(`[data-space-scroll="${overview?'overview':'workbench'}"]`);(active?.querySelector('h1') as HTMLElement|null)?.focus({preventScroll:true});});return()=>cancelAnimationFrame(id);},[routeKey,overview]);
  const selected=sections.find(s=>s.id===route.section);
  const openIntent=(id:string)=>{location.hash=workHref(id);};
  const allowed=route.section==='run'?canUse:route.section==='resources'?(canUse||canContribute):route.section==='acceptance'?account.is_administrator:canUse||account.is_administrator;
  const style={transform:preview?`translateX(${preview}px)`:undefined};
  return <div className="space-body" style={style}>
    <div ref={overviewScroll} className="space-scroll" data-space-scroll="overview" hidden={!overview} onScroll={event=>{if(overview&&event.currentTarget.clientHeight)overviewPosition.current=event.currentTarget.scrollTop;}}><QueryActivityContext.Provider value={overview}><OverviewPage api={api}/></QueryActivityContext.Provider><footer className="page-footer">共享计算，以真实状态为准。</footer></div>
    <div ref={workScroll} className="space-scroll" data-space-scroll="workbench" hidden={overview} onScroll={event=>{if(!overview&&event.currentTarget.clientHeight)positions.current[routeKey]=event.currentTarget.scrollTop;}}>
      <QueryActivityContext.Provider value={!overview}><fieldset disabled={!accountReady} className="workbench-content workbench-availability"><section className="workbench-intro"><p className="eyebrow">WORKBENCH</p><h1 tabIndex={-1}>{selected?.name}</h1><p>{selected?.description}</p>{route.intent&&<a href="#run" className="link-button">← 返回我的工作</a>}</section>
      {!allowed?<section className="panel"><h2>此账户未获得该能力</h2><p>请选择已授权的工作区，或连接有对应权限的账户。</p><a href="#overview">返回总览</a></section>:<>
        {canUse&&(visitedRun||route.section==='run')&&<div hidden={route.section!=='run'}><QueryActivityContext.Provider value={!overview&&route.section==='run'}><RunPage api={api} onOpen={openIntent}/></QueryActivityContext.Provider></div>}
        {route.section==='resources'&&<ResourcesPage api={api} canContribute={canContribute&&Boolean(account.owner_id)}/>}
        {route.section==='continuity'&&<ContinuityPage key={route.intent} api={api} initialId={route.intent} onSelect={openIntent}/>}
        {route.section==='decisions'&&<DecisionsPage key={route.decision} api={api} initialId={route.decision} workId={route.intent} onSelect={id=>{location.hash=decisionHref(id,route.intent);}}/>}
        {route.section==='events'&&<EventsPage key={route.intent} api={api} intentId={route.intent} showExperiments={account.is_administrator&&!route.intent}/>}
        {route.section==='acceptance'&&account.is_administrator&&<AcceptancePage api={api} showAdminClock/>}
      </>}
      <footer className="page-footer">{modeLabel(service.mode)} · 状态以服务端为准</footer></fieldset></QueryActivityContext.Provider>
    </div>
  </div>;
}
