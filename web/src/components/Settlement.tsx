import {ApiClient,list,object,text,type JsonObject} from '../api';
import {ArtifactDownload} from './CP2';
import {RawData,Status} from './common';

// These are read-model explanations, never a second lifecycle state machine.
export function SettlementSummary({api,work,jobs,allocations,legal}:{api:ApiClient;work:JsonObject;jobs:JsonObject[];allocations:JsonObject[];legal:JsonObject[]}) {
  const result=object(work.result),failure=object(result.failure);
  const terminal=['completed','failed','cancelled'].includes(text(work.state));
  const holding=allocations.some(a=>a.pending_terminal||['Allocated','Running','PreemptRequested'].includes(text(a.state)));
  const stopping=allocations.some(a=>a.pending_terminal);
  const released=allocations.length>0&&allocations.every(a=>!a.pending_terminal&&['Released','Expired','Evicted','Rejected'].includes(text(a.state)));
  const canRelease=legal.some(a=>a.operation==='allocation.release');
  const canResume=legal.some(a=>a.operation==='allocation.resume');
  const failedStage=list(work.stages).find(s=>s.state==='failed');
  const stageError=object(object(failedStage?.receipt).error);
  const uris=[...new Set([...(Array.isArray(result.artifact_uris)?result.artifact_uris:[]),...jobs.map(job=>job.result_uri)].filter((uri):uri is string=>typeof uri==='string'&&uri.startsWith('fabric-cpu://')))];
  let guidance='刷新可读取服务端最新状态；结果与资源归还会分别显示。';
  if(stopping)guidance='停止请求正在确认中。请刷新核对，确认前不能视为资源已归还。';
  else if(terminal&&holding&&canRelease)guidance='工作已经结束，租约仍持有资源。请点击下方“释放租约”，再刷新确认归还。';
  else if(terminal&&holding)guidance='工作已经结束，租约仍持有资源。请刷新并核对阶段回执；当前快照未提供释放操作。';
  else if(terminal&&released)guidance='工作已经结束，关联租约已不再持有资源。可在这里查看结果，或打开关联事件核对过程。';
  else if(canResume)guidance='当前快照允许恢复。点击下方“恢复这项工作”后，核对新执行代次与实际接续状态。';
  else if(work.state==='waiting')guidance='工作正在等待资源。保留当前工作 ID，刷新查看分配结果，无需重新提交工作。';
  else if(failedStage)guidance='工作流程有阶段失败。请查看下方失败回执，并使用当前工作可用的重试或释放操作。';
  return <section className="work-outcome" aria-label="结果与结算">
    <h3>结果与结算</h3>
    {work.result?<><p><Status value={result.kind}/> · 记录于 {text(result.recorded_at)} 逻辑秒</p>{result.kind==='failure'&&<div className="feedback error" role="alert"><strong>工作失败 · {text(failure.code,'原因未提供')}</strong><p>{text(failure.detail,'请查看服务端结果记录。')}</p></div>}<RawData data={result} label="查看结果记录"/></>:<p>{terminal?'工作状态已结束，尚未取得终态结果记录，请刷新核对。':'尚未产生终态结果。'}</p>}
    <p className="next-action" role="status">{guidance}</p>
    {failedStage&&<div className="feedback warning"><strong>阶段失败：{text(failedStage.operation)}</strong><p>{text(stageError.code,'未提供错误码')} · {text(stageError.detail,'请展开阶段提交结果核对。')}</p><RawData data={failedStage.receipt} label="查看失败阶段回执"/></div>}
    <div className="result-artifacts">{uris.map((uri,index)=><ArtifactDownload api={api} key={uri} uri={uri} label={`下载真实结果 ${index+1}`} previewLabel={`查看真实结果 ${index+1}`}/>)}</div>
    {work.execution_mode==='logical_simulation'&&Boolean(work.result)&&<p className="helper">逻辑模拟不产生真实计算工件。</p>}
    {work.execution_mode==='cpu_reference'&&<p className="helper">真实 CPU 参考结果可直接在页面查看或下载原始 JSON；不代表真实 GPU 训练。{terminal&&!uris.length?'当前响应尚未提供已登记的结果工件。':''}</p>}
  </section>;
}
