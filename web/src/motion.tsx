import {useContext,useEffect,useLayoutEffect,useRef,type ReactNode,type RefObject} from 'react';
import {QueryActivityContext} from './hooks';

const easing='cubic-bezier(.22,1,.36,1)';
const reducedQuery=()=>typeof window.matchMedia==='function'?window.matchMedia('(prefers-reduced-motion: reduce)'):null;

/** No retained outgoing view, delayed reads, or changes to the business lifecycle. */
export function reveal(element:HTMLElement|null,refresh=false){
  const preference=reducedQuery();
  if(!element||typeof element.animate!=='function'||preference?.matches)return ()=>{};
  const animation=element.animate([
    {opacity:refresh ? .72 : 0,transform:`translateY(${refresh?3:8}px)`},
    {opacity:1,transform:'translateY(0)'},
  ],{duration:refresh?220:300,easing});
  const stop=()=>animation.cancel();
  const change=()=>{if(preference?.matches)stop();};
  preference?.addEventListener?.('change',change);
  const removeListener=()=>preference?.removeEventListener?.('change',change);
  animation.addEventListener('finish',removeListener,{once:true});
  animation.addEventListener('cancel',removeListener,{once:true});
  return ()=>{stop();removeListener();};
}

export function MotionReveal({trigger,children}:{trigger:unknown;children:ReactNode}){
  const ref=useRef<HTMLDivElement>(null),seen=useRef(false);
  const active=useContext(QueryActivityContext);
  useLayoutEffect(()=>{
    if(!active)return;
    const stop=reveal(ref.current,seen.current);seen.current=true;
    return stop;
  },[trigger,active]);
  return <div className="query-result-motion" ref={ref}>{children}</div>;
}

/** The selection surface travels independently of the links and their focus. */
export function MovingSelection({value,selector,className='nav-selection'}:{value:string;selector:string;className?:string}){
  const ref=useRef<HTMLSpanElement>(null);
  useLayoutEffect(()=>{
    const indicator=ref.current,parent=indicator?.parentElement;
    if(!indicator||!parent)return;
    let frame=0;
    const measure=()=>{
      const target=parent.querySelector<HTMLElement>(selector);
      if(!target){delete indicator.dataset.visible;return;}
      indicator.style.width=`${target.offsetWidth}px`;
      indicator.style.height=`${target.offsetHeight}px`;
      indicator.style.transform=`translate(${target.offsetLeft}px,${target.offsetTop}px)`;
      indicator.dataset.visible='true';
      if(!indicator.dataset.ready)frame=requestAnimationFrame(()=>{indicator.dataset.ready='true';});
    };
    measure();
    const observer=typeof ResizeObserver==='function'?new ResizeObserver(measure):null;
    observer?.observe(parent);
    for(const child of parent.children)if(child!==indicator)observer?.observe(child);
    window.addEventListener('resize',measure);
    return ()=>{observer?.disconnect();window.removeEventListener('resize',measure);cancelAnimationFrame(frame);};
  },[value,selector]);
  return <span ref={ref} className={className} aria-hidden="true"/>;
}

/** Native click/keyboard actions remain untouched. Only visual state is delegated. */
export function usePressMotion(root:RefObject<HTMLElement|null>){
  useEffect(()=>{
    const host=root.current;
    if(!host)return;
    const preference=reducedQuery();
    const animations=new Map<HTMLElement,Animation>();
    let pressed:HTMLElement|null=null,pointer:number|null=null,startX=0,startY=0;
    const targetOf=(target:EventTarget|null)=>{
      const element=target instanceof Element?target.closest<HTMLElement>('button,a.button,.sidebar nav a,summary,.link-button'):null;
      return element&&host.contains(element)&&!element.matches(':disabled,[aria-disabled="true"]')&&!element.closest('[hidden],[inert]')?element:null;
    };
    const clear=(spring=false)=>{
      const element=pressed;pressed=null;pointer=null;
      if(!element)return;
      delete element.dataset.motionPressed;
      if(!spring||preference?.matches||!element.isConnected||typeof element.animate!=='function')return;
      animations.get(element)?.cancel();
      const animation=element.animate([{scale:'.96',offset:0},{scale:'1.018',offset:.48},{scale:'.996',offset:.76},{scale:'1',offset:1}],{duration:340,easing:'ease-out'});
      animations.set(element,animation);
      const done=()=>{if(animations.get(element)===animation)animations.delete(element);};
      animation.addEventListener('finish',done,{once:true});animation.addEventListener('cancel',done,{once:true});
    };
    const press=(element:HTMLElement)=>{
      clear();animations.get(element)?.cancel();
      pressed=element;element.dataset.motionPressed='true';
    };
    const down=(event:PointerEvent)=>{
      if(event.button!==0||!event.isPrimary||event.metaKey||event.ctrlKey||event.altKey)return;
      const element=targetOf(event.target);if(!element)return;
      const box=element.getBoundingClientRect();
      element.style.setProperty('--press-origin-x',`${Math.max(20,Math.min(80,(event.clientX-box.left)/Math.max(box.width,1)*100))}%`);
      element.style.setProperty('--press-origin-y',`${Math.max(20,Math.min(80,(event.clientY-box.top)/Math.max(box.height,1)*100))}%`);
      press(element);pointer=event.pointerId;startX=event.clientX;startY=event.clientY;
    };
    const move=(event:PointerEvent)=>{if(pointer===event.pointerId&&Math.hypot(event.clientX-startX,event.clientY-startY)>10)clear();};
    const up=(event:PointerEvent)=>{if(pointer===event.pointerId)clear(targetOf(event.target)===pressed);};
    const cancel=()=>clear();
    const focusout=(event:FocusEvent)=>{if(event.target instanceof Node&&pressed?.contains(event.target))clear();};
    const keydown=(event:KeyboardEvent)=>{
      if(event.key==='Escape'){clear();return;}
      if(event.repeat||event.metaKey||event.ctrlKey||event.altKey||!['Enter',' '].includes(event.key))return;
      const element=targetOf(event.target);if(!element||(event.key===' '&&element.tagName==='A'))return;
      element.style.setProperty('--press-origin-x','50%');element.style.setProperty('--press-origin-y','50%');press(element);
    };
    const keyup=(event:KeyboardEvent)=>{if(['Enter',' '].includes(event.key)&&pressed)clear(targetOf(event.target)===pressed);};
    const motionChange=()=>{if(preference?.matches){clear();for(const animation of animations.values())animation.cancel();animations.clear();}};
    host.addEventListener('pointerdown',down);host.addEventListener('keydown',keydown);
    window.addEventListener('pointermove',move,{passive:true});window.addEventListener('pointerup',up);
    window.addEventListener('pointercancel',cancel);window.addEventListener('keyup',keyup);window.addEventListener('blur',cancel);
    host.addEventListener('focusout',focusout);preference?.addEventListener?.('change',motionChange);
    return ()=>{
      clear();for(const animation of animations.values())animation.cancel();animations.clear();
      host.removeEventListener('pointerdown',down);host.removeEventListener('keydown',keydown);host.removeEventListener('focusout',focusout);
      window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up);window.removeEventListener('pointercancel',cancel);
      window.removeEventListener('keyup',keyup);window.removeEventListener('blur',cancel);preference?.removeEventListener?.('change',motionChange);
    };
  },[root]);
}
