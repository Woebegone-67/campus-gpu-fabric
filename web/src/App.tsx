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
import {ServiceContext,serviceInfo} from './service';
import {AccountContext,type AccountSession} from './session';
import {useBrowserSession} from './useBrowserSession';
import {StarwaveSurface,StarwaveMotionControl} from './visual/starwave/StarwaveSurface';
import {SpaceGesture} from './components/SpaceGesture';
import {MovingSelection,reveal,usePressMotion} from './motion';

const sections=[
  {id:'run',name:'我的工作'},
  {id:'resources',name:'我的设备'},
  {id:'continuity',name:'工作详情'},
  {id:'decisions',name:'分配说明'},
  {id:'events',name:'活动记录'},
  {id:'acceptance',name:'系统管理'},
];
function parseHash(hash:string){const [section,search='']=hash.slice(1).split('?');const params=new URLSearchParams(search);return {section:sections.some(w=>w.id===section)?section:'overview',intent:params.get('intent')||'',decision:params.get('decision')||'',create:params.get('new')==='1'};}
export function readLocation(){return parseHash(location.hash);}
function workbenchTarget(account:AccountSession|null,hash:string){
  const section=parseHash(hash).section;
  const permitted=section==='acceptance'?account?.is_administrator:section==='resources'?Boolean(account?.capabilities.length):account?.capabilities.includes('use_compute');
  if(permitted)return hash;
  return account?.owner_id&&account.capabilities.includes('use_compute')?'#run':account?.owner_id&&account.capabilities.includes('contribute_resources')?'#resources':account?.is_administrator?'#acceptance':'#run';
}
export default function App(){
  const appRoot=useRef<HTMLDivElement>(null);
  usePressMotion(appRoot);
  const [starwaveEnabled]=useState(()=>new URLSearchParams(location.search).get('visual')!=='classic');
  const session=useBrowserSession(),account=session.account;
  const identity=useMemo<Identity>(()=>({owner:account?.owner_id||'',token:'',actorId:account?.actor_id}),[account?.actor_id,account?.owner_id]);
  const [identityOpen,setIdentityOpen]=useState(false),[mutations,setMutations]=useState(0);
  const identityGeneration=account?.actor_id||'';
  const writeIdentity=useRef<Identity|null>(null);
  const onMutating=useCallback((amount:number)=>setMutations(value=>Math.max(0,value+amount)),[]);
  const api=useMemo(()=>new ApiClient(identity,onMutating,()=>writeIdentity.current===identity,session.invalidate),[identity,onMutating,session.invalidate]);
  const [route,setRoute]=useState(readLocation);
  const lastWorkbench=useRef(location.hash&&route.section!=='overview'?location.hash:'#run');
  const [gestureEnabled,setGestureEnabled]=useState(false),[preview,setPreview]=useState(0);
  const accountPanel=useRef<HTMLDivElement>(null),accountTrigger=useRef<HTMLButtonElement>(null);
  const [epoch,setEpoch]=useState(0),epochRef=useRef(0);
  const acceptEpoch=useCallback((next:number)=>{if(next<epochRef.current)return false;if(next>epochRef.current){epochRef.current=next;setEpoch(next);setPreview(0);}return true;},[]);
  const health=useQuery(api,'/healthz'),version=useQuery(api,'/v1/version');
  const accountReady=Boolean(account)&&!session.error&&!session.loading;
  writeIdentity.current=accountReady?identity:null;
  const service=serviceInfo(version.data),isOnline=health.data!==null&&!health.error;
  const space=route.section==='overview'?'overview':'workbench';
  useEffect(()=>{const change=()=>{const next=readLocation();if(next.section!=='overview')lastWorkbench.current=location.hash;setRoute(next);setPreview(0);};window.addEventListener('hashchange',change);return()=>window.removeEventListener('hashchange',change);},[]);
  useEffect(()=>{setPreview(0);setGestureEnabled(false);lastWorkbench.current='#run';epochRef.current=0;setEpoch(0);},[account?.actor_id]);
  async function logout(){if(mutations||session.loading)return;await session.logout();setIdentityOpen(false);}
  const workbenchHash=workbenchTarget(account,lastWorkbench.current);
  function navigateSpace(target:'overview'|'workbench'){setPreview(0);const hash=target==='overview'?'#overview':workbenchHash;if(location.hash!==hash)location.hash=hash;}
  const refresh=()=>{health.refresh();version.refresh();session.refresh();};
  useEffect(()=>{if(accountReady)setIdentityOpen(false);},[accountReady,identityGeneration]);
  useEffect(()=>{
    if(!identityOpen)return;
    const close=(event:PointerEvent)=>{if(!accountPanel.current?.contains(event.target as Node)&&!accountTrigger.current?.contains(event.target as Node))setIdentityOpen(false);};
    const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'){setIdentityOpen(false);accountTrigger.current?.focus();}};
    document.addEventListener('pointerdown',close);document.addEventListener('keydown',escape);
    return()=>{document.removeEventListener('pointerdown',close);document.removeEventListener('keydown',escape);};
  },[identityOpen]);
  const canUse=account?.capabilities.includes('use_compute');
  const canContribute=account?.capabilities.includes('contribute_resources');
  return <div ref={appRoot} className={`app dual-space${starwaveEnabled?' cp4-starwave':''}`} data-space={space}>
    <aside className="sidebar"><a className="wordmark" href="#overview"><span>织算<span className="brand-dot">✦</span></span><small>CAMPUS FABRIC</small></a>
      {account&&<nav aria-label="工作台导航"><MovingSelection value={`${route.section}:${account.actor_id}`} selector="a[aria-current]"/>{canUse&&<a href="#run" aria-current={route.section==='run'?'page':undefined}>我的工作</a>}{(canUse||canContribute)&&<a href="#resources" aria-current={route.section==='resources'?'page':undefined}>{canContribute?'我的设备':'可用资源'}</a>}{canUse&&<><a href="#events" aria-current={route.section==='events'?'page':undefined}>活动记录</a><a href="#decisions" aria-current={route.section==='decisions'?'page':undefined}>分配说明</a></>}{account.is_administrator&&<a href="#acceptance" aria-current={route.section==='acceptance'?'page':undefined}>系统管理</a>}</nav>}
      <footer><span className={`connection-dot ${isOnline?'online':''}`}/>{isOnline?'服务在线':'服务未连接'}</footer>
    </aside>
    <StarwaveSurface enabled={starwaveEnabled} visible={!account||space==='overview'}>
      <SpaceGesture activeSpace={space} enabled={gestureEnabled&&Boolean(account)} blocked={identityOpen||mutations>0} scopeKey={`${identityGeneration}:${epoch}:${route.section}:${route.intent}:${route.decision}`} onNavigate={navigateSpace} onPreview={setPreview} className="topbar space-gesture-header">
        <div className="space-switch" data-space-gesture-ignore><MovingSelection value={space} selector="button[aria-pressed='true']" className="space-switch-selection"/><button className={space==='overview'?'selected':''} aria-pressed={space==='overview'} onClick={()=>navigateSpace('overview')}>总览</button><button className={space==='workbench'?'selected':''} aria-pressed={space==='workbench'} onClick={()=>navigateSpace('workbench')}>工作台</button></div>
        <button ref={accountTrigger} className="button account-trigger" aria-expanded={Boolean(account)&&identityOpen} aria-controls="account-panel" onClick={()=>{if(account){setIdentityOpen(value=>!value);}else{document.querySelector<HTMLInputElement>('.identity-manual input')?.focus();}}}><span className="account-avatar" aria-hidden>{(account?.username||account?.owner_id||account?.actor_id||'?').slice(0,1).toUpperCase()}</span>{account?account.username||account.owner_id||'管理员':'登录'}</button>
      </SpaceGesture>
      <div ref={accountPanel} id="account-panel" className={account?'shell-popovers account-menu':'account-gate'} hidden={Boolean(account)&&!identityOpen} role={account?'dialog':undefined} aria-label={account?'账户菜单':undefined} data-space-gesture-blocked="true">
        <div className="popover-heading">{account?<strong>{account.username||account.owner_id||'管理员'}</strong>:<h1>登录织算</h1>}{account&&<button className="button quiet" aria-label="关闭账户菜单" onClick={()=>setIdentityOpen(false)}>×</button>}</div>
        <div className="account-menu-actions" hidden={!account}>
          <button className="account-menu-action" disabled={mutations>0||session.loading} onClick={()=>void logout()}>切换账户</button>
          <details className="account-preferences"><summary>显示偏好</summary><StarwaveMotionControl/><label><input type="checkbox" checked={gestureEnabled} onChange={event=>setGestureEnabled(event.target.checked)}/>页头横向手势</label></details>
          <div className="account-instance">{service.mode==='cpu_reference'?'CPU 演示':service.mode==='logical_simulation'?'逻辑模拟':'实例未连接'}</div>
          <button className="account-menu-action" disabled={mutations>0||session.loading} onClick={()=>void logout()}>退出登录</button>
        </div>
        <div hidden={Boolean(account)}><IdentityPicker onLogin={session.login} busy={mutations>0||session.loading} error={session.error}/>{session.loading&&<p className="account-connection-status" role="status">正在验证账户…</p>}</div>
      </div>
      {health.error&&<div className="shell-notice" role="status">服务未连接。<button className="link-button" onClick={refresh}>重新连接</button></div>}
      {isOnline&&object(health.data).control_plane_ready===false&&<div className="shell-notice">服务尚未就绪。</div>}
      {account&&session.error&&<div className="shell-notice" role="status">账户暂时无法验证，已暂停提交。<button className="link-button" onClick={()=>void session.refresh()}>重新检查账户</button></div>}
      {account&&
      <ServiceContext.Provider value={service}><AccountContext.Provider value={account}><QueryEpochContext.Provider value={acceptEpoch}><Workspace key={`${identityGeneration}:${epoch}`} api={api} account={account} route={route} workRoute={space==='overview'?parseHash(workbenchHash):route} preview={preview} accountReady={accountReady}/></QueryEpochContext.Provider></AccountContext.Provider></ServiceContext.Provider>}
    </StarwaveSurface>
  </div>;
}
function Workspace({api,account,route:activeRoute,workRoute:route,preview,accountReady}:{api:ApiClient;account:AccountSession;route:ReturnType<typeof readLocation>;workRoute:ReturnType<typeof readLocation>;preview:number;accountReady:boolean}) {
  const overview=activeRoute.section==='overview',canUse=account.capabilities.includes('use_compute'),canContribute=account.capabilities.includes('contribute_resources');
  const [visitedRun,setVisitedRun]=useState(route.section==='run');
  useEffect(()=>{if(route.section==='run')setVisitedRun(true);},[route.section]);
  const workScroll=useRef<HTMLDivElement>(null),overviewScroll=useRef<HTMLDivElement>(null),overviewPosition=useRef(0),positions=useRef<Record<string,number>>({});
  const routeKey=`${route.section}:${route.intent}:${route.decision}`;
  useLayoutEffect(()=>{const scroll=overview?overviewScroll.current:workScroll.current;return reveal(scroll?.querySelector<HTMLElement>(overview?'.overview-layout':'.workbench-content')??null);},[routeKey,overview]);
  useLayoutEffect(()=>{const element=overview?overviewScroll.current:workScroll.current;if(element)element.scrollTop=overview?overviewPosition.current:positions.current[routeKey]||0;},[routeKey,overview]);
  const first=useRef(true);
  useEffect(()=>{if(first.current){first.current=false;return;}const id=requestAnimationFrame(()=>{const active=document.querySelector(`[data-space-scroll="${overview?'overview':'workbench'}"]`);(active?.querySelector('h1') as HTMLElement|null)?.focus({preventScroll:true});});return()=>cancelAnimationFrame(id);},[routeKey,overview]);
  const selected=sections.find(s=>s.id===route.section);
  const openIntent=(id:string)=>{location.hash=workHref(id);};
  const allowed=route.section==='run'?canUse:route.section==='resources'?(canUse||canContribute):route.section==='acceptance'?account.is_administrator:canUse||account.is_administrator;
  const style={transform:preview?`translateX(${preview}px)`:undefined};
  return <div className="space-body" style={style}>
    <div ref={overviewScroll} className="space-scroll" data-space-scroll="overview" hidden={!overview} onScroll={event=>{if(overview&&event.currentTarget.clientHeight)overviewPosition.current=event.currentTarget.scrollTop;}}><QueryActivityContext.Provider value={overview}><OverviewPage api={api}/></QueryActivityContext.Provider></div>
    <div ref={workScroll} className="space-scroll" data-space-scroll="workbench" hidden={overview} onScroll={event=>{if(!overview&&event.currentTarget.clientHeight)positions.current[routeKey]=event.currentTarget.scrollTop;}}>
      <QueryActivityContext.Provider value={!overview}><fieldset disabled={!accountReady} className="workbench-content workbench-availability"><div className="workbench-intro"><h1 className="visually-hidden" tabIndex={-1}>{selected?.name}</h1>{route.intent&&<a href="#run" className="link-button">← 返回我的工作</a>}</div>
      {!allowed?<section className="panel"><h2>此账户未获得该能力</h2><p>请选择已授权的工作区，或登录有对应权限的账户。</p><a href="#overview">返回总览</a></section>:<>
        {canUse&&(visitedRun||route.section==='run')&&<div hidden={route.section!=='run'}><QueryActivityContext.Provider value={!overview&&route.section==='run'}><RunPage api={api} onOpen={openIntent} initialCreate={route.create}/></QueryActivityContext.Provider></div>}
        {route.section==='resources'&&<ResourcesPage api={api} canContribute={canContribute&&Boolean(account.owner_id)}/>}
        {route.section==='continuity'&&<ContinuityPage key={route.intent} api={api} initialId={route.intent} onSelect={openIntent}/>}
        {route.section==='decisions'&&<DecisionsPage key={route.decision} api={api} initialId={route.decision} workId={route.intent} onSelect={id=>{location.hash=decisionHref(id,route.intent);}}/>}
        {route.section==='events'&&<EventsPage key={route.intent} api={api} intentId={route.intent}/>}
        {route.section==='acceptance'&&account.is_administrator&&<AcceptancePage api={api} showAdminClock/>}
      </>}
      </fieldset></QueryActivityContext.Provider>
    </div>
  </div>;
}
