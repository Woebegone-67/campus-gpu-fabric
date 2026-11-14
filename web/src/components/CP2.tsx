import {useState} from 'react';
import {ApiClient,ApiError,object,text,type JsonObject} from '../api';
import {downloadJsonText,ErrorNotice,Status} from './common';

export function ArtifactDownload({api,uri,label='下载真实工件'}:{api:ApiClient;uri:unknown;label?:string}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null);
  if(typeof uri!=='string'||!uri.startsWith('fabric-cpu://'))return null;
  async function download(){
    setBusy(true);setError(null);
    try {const response=await api.request(`/x/v1/artifacts?uri=${encodeURIComponent(uri as string)}`);downloadJsonText(response.raw,'cpu-reference-artifact.json');}
    catch(error){setError(error instanceof ApiError?error:new ApiError('工件读取失败',0));}
    finally{setBusy(false);}
  }
  return <div className="action-unit"><button type="button" className="button" onClick={download} disabled={busy}>{busy?'正在读取工件…':label}</button>{error&&<ErrorNotice error={error}/>}</div>;
}
export function CheckpointFacts({checkpoint,api}:{checkpoint:unknown;api:ApiClient}) {
  const fact=object(checkpoint);
  if(!Object.keys(fact).length)return <p className="helper">尚无保存点报告。</p>;
  return <><div className="checkpoint-grid">
    <div><span>保存报告</span><Status value={fact.report_status}/><small>报告于 {text(fact.reported_at)} 逻辑秒</small></div>
    <div><span>工件核验</span><Status value={fact.verification_status??'unverified'}/><small>{text(fact.verifier_id,'未记录核验器')} · {text(fact.verified_at,'未记录核验时间')}</small></div>
    <div><span>实际接续</span><Status value={fact.continuation_status??'not_attempted'}/><small>{text(fact.resumed_attempt_id,'未记录接续代次')}</small></div>
  </div><p className="helper">收到 ready 报告、核验工件、实际应用到新执行是三件独立事实。</p>{fact.verification_status==='verified'&&<ArtifactDownload api={api} uri={fact.artifact_uri} label="下载已核验保存点"/>}</>;
}
export function StopStatus({allocation}:{allocation:JsonObject}) {
  return allocation.pending_terminal?<div className="feedback warning" role="status"><strong>正在停止执行 · 等待确认</strong><p>请求目标：<Status value={allocation.pending_terminal}/>。停止确认前，资源仍由此租约持有；当前资源状态以账本为准。</p></div>:null;
}
export function Budget({label,used,max}:{label:string;used:unknown;max:unknown}) {
  return <div><dt>{label}</dt><dd>{text(used,'未记录')} / {text(max,'未记录')} <small>已用 / 上限</small></dd></div>;
}
