import {useEffect,useRef,useState} from 'react';
import {ApiClient,ApiError,object,text,type JsonObject} from '../api';
import {downloadJsonText,ErrorNotice,Status} from './common';

export function ArtifactDownload({api,uri,label='下载真实工件',previewLabel}:{api:ApiClient;uri:unknown;label?:string;previewLabel?:string}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null);
  const [preview,setPreview]=useState<{api:ApiClient;uri:unknown;raw:string}|null>(null);
  const pending=useRef<AbortController|null>(null);
  const current=useRef({api,uri});
  current.current={api,uri};
  useEffect(()=>{
    setBusy(false);setError(null);setPreview(null);
    return ()=>{pending.current?.abort();pending.current=null;};
  },[api,uri]);
  if(typeof uri!=='string'||!uri.startsWith('fabric-cpu://'))return null;
  async function read(download:boolean){
    pending.current?.abort();
    const controller=new AbortController();
    pending.current=controller;setBusy(true);setError(null);setPreview(null);
    const isCurrent=()=>pending.current===controller&&!controller.signal.aborted
      &&current.current.api===api&&current.current.uri===uri;
    try {const response=await api.request(`/x/v1/artifacts?uri=${encodeURIComponent(uri as string)}`,{signal:controller.signal});
      if(!isCurrent())return;
      if(download){const name=`cpu-${(uri as string).slice('fabric-cpu://'.length).replace(/[^a-zA-Z0-9._-]+/g,'-')}`;downloadJsonText(response.raw,name.endsWith('.json')?name:`${name}.json`);}
      else setPreview({api,uri,raw:response.raw});
    }
    catch(error){if(isCurrent())setError(error instanceof ApiError?error:new ApiError('工件读取失败',0));}
    finally{if(isCurrent()){pending.current=null;setBusy(false);}}
  }
  return <div className="action-unit artifact-access"><div className="actions">{previewLabel&&<button type="button" className="button" onClick={()=>read(false)} disabled={busy}>{previewLabel}</button>}<button type="button" className="button" onClick={()=>read(true)} disabled={busy}>{busy?'正在读取工件…':label}</button></div>{error&&<ErrorNotice error={error}/>} {preview&&preview.api===api&&preview.uri===uri&&<div className="raw" aria-label={previewLabel}><p className="helper">服务端原始 JSON，保留原始数字与内容。</p><pre tabIndex={0}>{preview.raw}</pre><button type="button" className="button quiet" onClick={()=>setPreview(null)}>收起工件</button></div>}</div>;
}
export function CheckpointFacts({checkpoint,api}:{checkpoint:unknown;api:ApiClient}) {
  const fact=object(checkpoint);
  if(!Object.keys(fact).length)return <p className="helper">尚无保存点报告。</p>;
  const summary=fact.continuation_status==='applied'?'已从保存点继续执行。':fact.continuation_status==='failed'?'从保存点接续失败，请查看记录。':fact.verification_status==='failed'?'保存点核验失败，不能确认其可用于接续。':fact.verification_status==='verified'?'保存点已核验，尚未确认应用到后续执行。':fact.report_status==='failed'?'保存点报告失败，未确认保存进度。':fact.report_status==='ready'?'已收到保存报告，尚未核验保存点。':'保存点状态待确认。';
  return <div className="checkpoint-facts"><p className="checkpoint-summary">{summary}</p><details className="work-disclosure checkpoint-details"><summary>保存点记录</summary><div className="checkpoint-grid">
    <div><span>保存报告</span><Status value={fact.report_status}/><small>报告于 {text(fact.reported_at)} 逻辑秒</small></div>
    <div><span>工件核验</span><Status value={fact.verification_status??'unverified'}/><small>{text(fact.verifier_id,'未记录核验器')} · {text(fact.verified_at,'未记录核验时间')}</small></div>
    <div><span>实际接续</span><Status value={fact.continuation_status??'not_attempted'}/><small>{text(fact.resumed_attempt_id,'未记录接续代次')}</small></div>
  </div><p className="helper">收到 ready 报告、核验工件、实际应用到新执行是三件独立事实。</p></details>{fact.verification_status==='verified'&&<ArtifactDownload api={api} uri={fact.artifact_uri} label="下载已核验保存点"/>}</div>;
}
export function StopStatus({allocation}:{allocation:JsonObject}) {
  return allocation.pending_terminal?<div className="feedback warning" role="status"><strong>正在停止执行 · 等待确认</strong><p>请求目标：<Status value={allocation.pending_terminal}/>。停止确认前，资源仍由此租约持有；当前资源状态以账本为准。</p></div>:null;
}
export function Budget({label,used,max}:{label:string;used:unknown;max:unknown}) {
  return <div><dt>{label}</dt><dd>{text(used,'未记录')} / {text(max,'未记录')} <small>已用 / 上限</small></dd></div>;
}
