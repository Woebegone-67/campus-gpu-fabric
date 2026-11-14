import {useEffect,useId,useRef,useState,type MouseEvent} from 'react';

type CopyPhase='copying'|'copied'|'error';
type CopyState={id:string;scope:unknown;phase:CopyPhase};

/** Clipboard feedback belongs to the displayed ID and account, never to a later view. */
export function CopyWorkId({id,scope,compact=false}:{id:string;scope?:unknown;compact?:boolean}) {
  const [state,setState]=useState<CopyState|null>(null);
  const current=useRef({id,scope});
  current.current={id,scope};
  const alive=useRef(false),request=useRef(0),inFlight=useRef<number|null>(null);
  const timer=useRef<ReturnType<typeof setTimeout>|null>(null);
  const feedbackId=useId();
  const clearFeedbackTimer=()=>{if(timer.current!==null){clearTimeout(timer.current);timer.current=null;}};
  useEffect(()=>{
    alive.current=true;
    inFlight.current=null;
    setState(null);
    return()=>{alive.current=false;request.current+=1;inFlight.current=null;clearFeedbackTimer();};
  },[id,scope]);
  const phase=state?.id===id&&state.scope===scope?state.phase:null;
  const copy=async(event:MouseEvent<HTMLButtonElement>)=>{
    event.stopPropagation();
    if(!id||inFlight.current!==null)return;
    clearFeedbackTimer();
    const sequence=++request.current;
    inFlight.current=sequence;
    setState({id,scope,phase:'copying'});
    const stillCurrent=()=>alive.current&&request.current===sequence&&current.current.id===id&&current.current.scope===scope;
    try{
      if(!navigator.clipboard?.writeText)throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(id);
      if(!stillCurrent())return;
      setState({id,scope,phase:'copied'});
      timer.current=setTimeout(()=>{
        timer.current=null;
        if(stillCurrent())setState(null);
      },2000);
    }catch{
      if(stillCurrent())setState({id,scope,phase:'error'});
    }finally{
      if(inFlight.current===sequence)inFlight.current=null;
    }
  };
  const message=phase==='copied'?'已复制完整工作 ID':phase==='error'?'无法自动复制，请选中下方完整 ID，按 ⌘C 或 Ctrl+C 复制。':phase==='copying'?'正在复制工作 ID':'';
  return <span className={`copy-work-id${compact?' copy-work-id--compact':''}`}>
    <button type="button" className="button quiet copy-work-id-button" onClick={copy} disabled={!id} aria-disabled={phase==='copying'} aria-label={`复制工作 ID ${id}`} aria-busy={phase==='copying'} aria-describedby={feedbackId} title={phase==='copied'?'已复制完整工作 ID':'复制完整工作 ID'}>
      <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true" focusable="false">{phase==='copied'?<path d="m4 10 4 4 8-8"/>:<><rect x="7" y="7" width="10" height="10" rx="2"/><path d="M13 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2"/></>}</svg>
      {!compact&&<span>{phase==='copied'?'已复制':phase==='copying'?'复制中…':'复制 ID'}</span>}
    </button>
    <span id={feedbackId} role="status" aria-live="polite" aria-atomic="true" className={phase==='error'?'copy-work-id-fallback':'copy-work-id-sr-only'}>{message}{phase==='error'&&<input aria-label="完整工作 ID，选中后手动复制" readOnly value={id} spellCheck={false} onFocus={event=>event.currentTarget.select()} onClick={event=>{event.stopPropagation();event.currentTarget.select();}}/>}</span>
  </span>;
}
