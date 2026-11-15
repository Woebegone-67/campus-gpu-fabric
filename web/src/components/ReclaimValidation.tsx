import {useEffect,useRef,useState} from 'react';
import {ApiClient,ApiError,list,object,text} from '../api';
import {useQuery} from '../hooks';
import {downloadJsonText,ErrorNotice,Panel,QueryFeedback,RawData,Refresh,Table} from './common';

const amount=(value:unknown)=>typeof value==='number'&&Number.isFinite(value)?value.toFixed(2):'—';
export function ReclaimValidation({api}:{api:ApiClient}){
  const query=useQuery(api,'/x/v1/reclaim-validation'),data=object(query.data),rows=list(data.summary);
  const preferences=[...new Set(rows.map(row=>text(row.preference??row.return_policy,'')))];
  return <Panel title="收回策略对照" actions={<Refresh loading={query.loading} onClick={query.refresh}/>}>
    <QueryFeedback query={query}>
      {typeof data.model_label==='string'&&<p>{data.model_label}</p>}
      {data.planner_matches_current===false&&<span className="status">历史策略版本</span>}
      {rows.length?preferences.map(preference=><section key={preference}>{preference&&<h3>{({preserve_work:'尽量保留计算',return_first:'尽快归还'} as Record<string,string>)[preference]??preference}</h3>}<Table rows={rows.filter(row=>text(row.preference??row.return_policy,'')===preference)} rowKey={row=>text(row.policy)} columns={[
        {label:'策略',render:row=>text(row.label??row.policy)},
        {label:'场景数',render:row=>text(row.cases)},
        {label:'平均代价 · 等价计算秒',render:row=>amount(row.mean_cost)},
        {label:'设备主平均等待 · 秒',render:row=>amount(row.mean_owner_wait_seconds)},
        {label:'超时归还',render:row=>text(row.deadline_misses)},
        {label:'保存失败',render:row=>text(row.checkpoint_failures)},
      ]}/></section>):<p className="empty">暂无对照结果</p>}
      {rows.length>0&&<div className="actions"><ReclaimDownload api={api} id="report"/><ReclaimDownload api={api} id="protocol"/></div>}
      <RawData data={data} label="完整记录"/>
    </QueryFeedback>
  </Panel>;
}
function ReclaimDownload({api,id}:{api:ApiClient;id:'report'|'protocol'}){
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null);
  const pending=useRef<AbortController|null>(null),current=useRef({api,id});current.current={api,id};
  useEffect(()=>{setBusy(false);setError(null);return()=>{pending.current?.abort();pending.current=null;};},[api,id]);
  async function download(){
    pending.current?.abort();const controller=new AbortController();pending.current=controller;setBusy(true);setError(null);
    const isCurrent=()=>pending.current===controller&&!controller.signal.aborted&&current.current.api===api&&current.current.id===id;
    try{
      const response=await api.request(`/x/v1/reclaim-validation/artifacts/${id}`,{signal:controller.signal});
      if(isCurrent())downloadJsonText(response.raw,`reclaim-${id}.json`);
    }catch(error){if(isCurrent())setError(error instanceof ApiError?error:new ApiError('文件读取失败',0));}
    finally{if(isCurrent()){pending.current=null;setBusy(false);}}
  }
  return <div className="action-unit"><button className="button" type="button" disabled={busy} onClick={download}>{busy?'正在读取…':id==='report'?'下载对照报告':'下载实验协议'}</button>{error&&<ErrorNotice error={error}/>}</div>;
}
