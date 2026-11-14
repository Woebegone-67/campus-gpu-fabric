import {useRef,useState,type FormEvent} from 'react';
import {ApiClient,ApiError,idOf,items,list,object,text,type ApiResult,type JsonObject} from '../api';
import {useQuery} from '../hooks';
import {useService} from '../service';
import {ErrorNotice,Field,Panel,QueryFeedback,RawData,Receipt,Refresh,Status,Table} from '../components/common';

export function ResourcesPage({api,canContribute=true}:{api:ApiClient;canContribute?:boolean}) {
  const service=useService();
  const [refresh,setRefresh]=useState(0),[cursors,setCursors]=useState<string[]>(['']);
  const cursor=cursors[cursors.length-1];
  const capacity=useQuery(api,'/v1/capacity',refresh);
  const contributions=useQuery(api,canContribute?`/x/v1/contributions?limit=50${cursor?`&after=${encodeURIComponent(cursor)}`:''}`:null,refresh);
  const ownedNodes=items(contributions.data).filter(node=>node.contributor_owner_id===api.identity.owner);
  const nodes=list(object(capacity.data).nodes),totals=object(object(capacity.data).totals);
  const reload=()=>{setCursors(['']);setRefresh(n=>n+1);};
  const nextCursor=object(contributions.data).next_cursor;
  const pool=<Panel title="资源池" actions={<Refresh onClick={reload} loading={capacity.loading}/>}>
    <QueryFeedback query={capacity}>
      {nodes.length?<>
        <div className="stat-row">{[['可用 GPU','gpu_count'],['可用 CPU · millicores','cpu_millicores'],['可用内存 · MiB','memory_mib']].map(([label,key])=><div key={key}><span>{label}</span><strong>{text(object(totals.available)[key])}<small> / {text(object(totals.capacity)[key])}</small></strong></div>)}</div>
        <Table rows={nodes} rowKey={idOf} columns={[
          {label:'节点',render:r=>text(r.node_id??r.id)},
          {label:'状态',render:r=><Status value={r.state}/>},
          {label:'可用 GPU',render:r=>text(object(r.available).gpu_count)},
          {label:'型号',render:r=>[...new Set(list(r.devices).map(device=>text(device.model)))].join(' / ')},
        ]}/>
      </>:<p className="empty">暂无可用设备</p>}
      <RawData data={capacity.data} label="技术详情"/>
    </QueryFeedback>
  </Panel>;
  return <div className="work-experience">
    {canContribute&&<>
      <Panel title="我的设备" actions={<Refresh onClick={reload} loading={contributions.loading}/>}>
        <QueryFeedback query={contributions}>
          {ownedNodes.length?<div className="record-list">{ownedNodes.map(row=>{
            const cards=[...new Map(list(row.devices).map(device=>[text(device.physical_gpu_id??device.device_id),device])).values()];
            return <div className="record" key={idOf(row)}>
              <div className="record-title"><strong>{text(row.node_id??row.id)}</strong><Status value={row.state}/></div>
              <p>{cards.length} 张 GPU{cards.length?` · ${[...new Set(cards.map(device=>text(device.model)))].join(' / ')}`:''}</p>
              <details className="work-disclosure"><summary>设备详情</summary>
                <div className="device-list">{list(row.devices).map(device=><div key={text(device.device_id)}><code>{text(device.device_id)}</code><span>{text(device.model)} · {text(device.memory_mib)} MiB{device.profile?` · ${text(device.profile)}`:''}</span><Status value={device.state}/></div>)}</div>
                <dl className="facts"><div><dt>CPU · millicores</dt><dd>{text(object(row.capacity).cpu_millicores)}</dd></div><div><dt>内存 · MiB</dt><dd>{text(object(row.capacity).memory_mib)}</dd></div></dl>
              </details>
            </div>;
          })}</div>:<p className="empty">暂无自己的设备</p>}
          {(typeof nextCursor==='string'||cursors.length>1)&&<div className="actions">{cursors.length>1&&<button type="button" className="button quiet" onClick={()=>setCursors(values=>values.slice(0,-1))}>上一页</button>}<span>第 {cursors.length} 页</span>{typeof nextCursor==='string'&&<button type="button" className="button quiet" onClick={()=>setCursors(values=>[...values,nextCursor])}>下一页</button>}</div>}
          {ownedNodes.some(node=>list(node.devices).length>0)&&<details className="work-disclosure"><summary>收回设备</summary><RecallForm api={api} nodes={ownedNodes} onDone={reload}/></details>}
          {ownedNodes.length>0&&<RawData data={{version:object(contributions.data).version,items:ownedNodes}} label="技术详情"/>}
        </QueryFeedback>
      </Panel>
      {service.mode==='logical_simulation'&&<details className="work-disclosure"><summary>登记模拟设备</summary><ContributionForm api={api} onDone={reload}/></details>}
    </>}
    {canContribute?<details className="work-disclosure"><summary>资源池</summary>{pool}</details>:pool}
  </div>;
}
function ContributionForm({api,onDone}:{api:ApiClient;onDone:()=>void}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null),[result,setResult]=useState<ApiResult|null>(null);
  const inFlight=useRef(false);
  async function submit(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();if(inFlight.current||error?.uncertain)return;setError(null);setResult(null);
    let payload:unknown;
    try{payload=JSON.parse(String(new FormData(event.currentTarget).get('node')));if(!object(payload).node_id)throw new Error('配置缺少 node_id。');}
    catch(error){setError(new ApiError(error instanceof Error?error.message:'JSON 无法解析',422));return;}
    inFlight.current=true;setBusy(true);
    try{setResult(await api.request('/x/v1/contributions',{method:'POST',body:payload}));onDone();}
    catch(error){setError(error instanceof ApiError?error:new ApiError('登记请求失败',0));}
    finally{inFlight.current=false;setBusy(false);}
  }
  return <Panel title="登记模拟设备"><form onSubmit={submit}><Field label="节点配置（JSON）"><textarea name="node" rows={7} required disabled={busy} placeholder="粘贴节点配置"/></Field><button className="button" disabled={busy||Boolean(error?.uncertain)}>{busy?'正在登记…':'登记设备'}</button></form>{error&&<ErrorNotice error={error}/>} {error?.uncertain&&<p role="status">请刷新设备列表确认登记结果。</p>}{result&&<Receipt result={result}/>}</Panel>;
}

export function RecallForm({api,nodes,onDone}:{api:ApiClient;nodes:JsonObject[];onDone:()=>void}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null),[result,setResult]=useState<ApiResult|null>(null);
  const [selected,setSelected]=useState<string[]>([]);
  const inFlight=useRef(false);
  const devices=[...new Map(nodes.filter(node=>node.contributor_owner_id===api.identity.owner).flatMap(node=>list(node.devices).map(device=>[text(device.physical_gpu_id??device.device_id,''),{...device,node_id:node.node_id}] as const))).entries()].filter(([id])=>id);
  const currentSelection=selected.filter(id=>devices.some(([deviceId])=>deviceId===id));
  async function submit(event:FormEvent<HTMLFormElement>){
    event.preventDefault();if(inFlight.current||error?.uncertain||!currentSelection.length)return;
    if(!window.confirm(`收回这 ${currentSelection.length} 张 GPU？正在使用它们的工作将被中断。`))return;
    const form=new FormData(event.currentTarget);inFlight.current=true;setBusy(true);setError(null);setResult(null);
    try{setResult(await api.request('/x/v1/contributions/recall',{method:'POST',body:{gpu_ids:currentSelection,notice_seconds:Number(form.get('notice')),duration_seconds:Number(form.get('duration'))}}));setSelected([]);onDone();}
    catch(error){setError(error instanceof ApiError?error:new ApiError('收回结果待确认',0));}
    finally{inFlight.current=false;setBusy(false);}
  }
  if(!devices.length)return null;
  return <div className="recall-form"><form onSubmit={submit}><fieldset disabled={busy||Boolean(error?.uncertain)}><legend>选择设备</legend><div className="device-choices">{devices.map(([id,device])=><label key={id}><input type="checkbox" checked={currentSelection.includes(id)} onChange={e=>setSelected(values=>e.target.checked?[...values,id]:values.filter(value=>value!==id))}/><span>{text(object(device).model)} · <code>{id}</code></span></label>)}</div><div className="form-grid"><Field label="提前通知（秒）"><input name="notice" type="number" min="1" max="300" defaultValue="30" required/></Field><Field label="自用时长（秒）"><input name="duration" type="number" min="1" max="3600" defaultValue="60" required/></Field></div></fieldset><button className="button primary" disabled={busy||!currentSelection.length||Boolean(error?.uncertain)}>{busy?'正在通知…':'发送收回通知'}</button></form>{error&&<ErrorNotice error={error}/>} {error?.uncertain&&<p role="status">请刷新设备状态确认收回结果。</p>}{result&&<Receipt result={result}/>}</div>;
}
