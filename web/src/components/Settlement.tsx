import {ApiClient,list,object,text,type JsonObject} from '../api';
import {ArtifactDownload} from './CP2';
import {RawData} from './common';

export function SettlementSummary({api,work,jobs,allocations,legal}:{api:ApiClient;work:JsonObject;jobs:JsonObject[];allocations:JsonObject[];legal:JsonObject[]}) {
  const result=object(work.result),failure=object(result.failure);
  const timedOut=failure.code==='TASK_TIMEOUT'||failure.detail==='Job timeout';
  const terminal=['completed','failed','cancelled'].includes(text(work.state));
  const holding=allocations.some(a=>a.pending_terminal||['Allocated','Running','PreemptRequested'].includes(text(a.state)));
  const stopping=allocations.some(a=>a.pending_terminal);
  const released=allocations.length>0&&allocations.every(a=>!a.pending_terminal&&['Released','Expired','Evicted','Rejected'].includes(text(a.state)));
  const canResume=legal.some(a=>a.operation==='allocation.resume');
  const failedStage=list(work.stages).find(s=>s.state==='failed');
  const stageError=object(object(failedStage?.receipt).error);
  const uris=[...new Set([...(Array.isArray(result.artifact_uris)?result.artifact_uris:[]),...jobs.map(job=>job.result_uri)].filter((uri):uri is string=>typeof uri==='string'&&uri.startsWith('fabric-cpu://')))];
  const guidance=stopping?'正在停止执行':terminal&&holding?'资源尚未归还':released?'资源已归还':canResume?'可以恢复这项工作':jobs.length===0&&allocations.some(a=>a.state==='PreemptRequested')?'资源正在收回':work.state==='waiting'&&allocations.some(a=>a.state==='Allocated')&&jobs.length===0?'资源已就绪，准备启动':work.state==='waiting'?'等待可用资源':'';
  if(!guidance&&!work.result&&!failedStage&&!uris.length)return null;
  return <section className="work-outcome" aria-label="结果与结算">
    {guidance&&<p className="next-action" role="status">{guidance}</p>}
    {result.kind==='failure'&&<div className="feedback error" role="alert"><strong>{timedOut?'运行超时':({EXECUTOR_FAILED:'执行失败'} as Record<string,string>)[text(failure.code)]||'工作失败'}</strong>{!timedOut&&<p>{text(failure.detail,text(failure.code,'未提供原因'))}</p>}</div>}
    {failedStage&&<div className="feedback warning"><strong>操作未完成</strong><p>{text(stageError.detail,text(stageError.code,'请重试当前操作。'))}</p><RawData data={failedStage.receipt} label="失败详情"/></div>}
    {uris.length>0&&<div className="result-artifacts">{uris.map((uri,index)=><ArtifactDownload api={api} key={uri} uri={uri} label={`下载结果 ${index+1}`} previewLabel={`查看结果 ${index+1}`}/>)}</div>}
  </section>;
}
