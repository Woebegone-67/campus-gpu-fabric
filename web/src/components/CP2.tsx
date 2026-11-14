import {useEffect,useRef,useState} from 'react';
import {ApiClient,ApiError,object,text,type JsonObject} from '../api';
import {downloadJsonText,ErrorNotice,Status} from './common';

export function ArtifactDownload({api,uri,label='下载文件',previewLabel}:{api:ApiClient;uri:unknown;label?:string;previewLabel?:string}) {
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
  return <div className="action-unit artifact-access"><div className="actions">{previewLabel&&<button type="button" className="button" onClick={()=>read(false)} disabled={busy}>{previewLabel}</button>}<button type="button" className="button" onClick={()=>read(true)} disabled={busy}>{busy?'读取中…':label}</button></div>{error&&<ErrorNotice error={error}/>} {preview&&preview.api===api&&preview.uri===uri&&<div className="raw" aria-label={previewLabel}><pre tabIndex={0}>{preview.raw}</pre><button type="button" className="button quiet" onClick={()=>setPreview(null)}>收起预览</button></div>}</div>;
}
export function CheckpointFacts({checkpoint,api}:{checkpoint:unknown;api:ApiClient}) {
  const fact=object(checkpoint);
  if(!Object.keys(fact).length)return null;
  const summary=fact.continuation_status==='applied'?'已从保存点继续':fact.continuation_status==='failed'?'恢复失败':fact.verification_status==='failed'?'保存点无法使用':fact.verification_status==='verified'?'进度已保存':fact.report_status==='failed'?'保存失败':fact.report_status==='ready'?'正在检查保存点':'等待保存';
  return <div className="checkpoint-facts"><p className="checkpoint-summary">{summary}</p><details className="work-disclosure checkpoint-details"><summary>保存点记录</summary><div className="checkpoint-grid">
    <div><span>保存报告</span><Status value={fact.report_status}/><small>报告于 {text(fact.reported_at)} 逻辑秒</small></div>
    <div><span>工件核验</span><Status value={fact.verification_status??'unverified'}/><small>{text(fact.verifier_id,'未记录核验器')} · {text(fact.verified_at,'未记录核验时间')}</small></div>
    <div><span>实际接续</span><Status value={fact.continuation_status??'not_attempted'}/><small>{text(fact.resumed_attempt_id,'未记录接续代次')}</small></div>
  </div></details>{fact.verification_status==='verified'&&<ArtifactDownload api={api} uri={fact.artifact_uri} label="下载保存点"/>}</div>;
}
export function StopStatus({allocation}:{allocation:JsonObject}) {
  return allocation.pending_terminal?<div className="feedback warning" role="status"><strong>正在停止执行</strong><p>资源将在停止确认后归还。</p></div>:null;
}
export function Budget({label,used,max}:{label:string;used:unknown;max:unknown}) {
  return <div><dt>{label}</dt><dd>{text(used,'未记录')} / {text(max,'未记录')} <small>已用 / 上限</small></dd></div>;
}
