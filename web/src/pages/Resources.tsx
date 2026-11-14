import {useState, type FormEvent} from 'react';
import {ApiClient, ApiError, idOf, items, list, object, text, type ApiResult, type JsonObject} from '../api';
import {useQuery} from '../hooks';
import {useService} from '../service';
import {ErrorNotice, Field, Metadata, Panel, QueryFeedback, RawData, Receipt, Refresh, Status, Table} from '../components/common';

export function ResourcesPage({api}: {api: ApiClient}) {
  const service=useService();
  const [refresh, setRefresh]=useState(0);
  const capacity=useQuery(api,'/v1/capacity',refresh);
  const contributions=useQuery(api,'/x/v1/contributions?limit=50',refresh);
  const nodes=list(object(capacity.data).nodes);
  const totals=object(object(capacity.data).totals);
  return <>
    <Panel title="可见资源" description="资源可见范围由服务端授权决定；可用数量来自权威账本。" actions={<Refresh onClick={()=>setRefresh(n=>n+1)} loading={capacity.loading}/>}>
      <Metadata data={capacity.data}/><QueryFeedback query={capacity} empty={!nodes.length}>
        <div className="stat-row">{[['GPU','gpu_count'],['CPU · millicores','cpu_millicores'],['内存 · MiB','memory_mib']].map(([label,key])=><div key={key}><span>{label}</span><strong>{text(object(totals.available)[key])}<small> / {text(object(totals.capacity)[key])}</small></strong><small>可用 / 总量</small></div>)}</div>
        <Table rows={nodes} rowKey={idOf} columns={[
          {label:'节点',render:r=><code>{text(r.node_id ?? r.id)}</code>},{label:'状态',render:r=><Status value={r.state}/>},
          {label:'可用 GPU',render:r=>text(object(r.available).gpu_count)},
          {label:'设备 / 型号 / 显存',render:r=><div className="device-list">{list(r.devices).map(device=><div key={text(device.device_id)}><code>{text(device.device_id)}</code><span>{text(device.model)} · {text(device.memory_mib)} MiB · {text(device.profile,'整卡')}</span><Status value={device.state}/></div>)}</div>},
        ]}/><RawData data={capacity.data}/>
      </QueryFeedback>
    </Panel>
    <Panel title="我的贡献" description="普通贡献者只管理自己获授权的供给。登记的模拟节点不代表已接入真实 GPU。" actions={<Refresh onClick={contributions.refresh} loading={contributions.loading}/>}>
      <Metadata data={contributions.data}/><QueryFeedback query={contributions} empty={!items(contributions.data).length}><div className="record-list">{items(contributions.data).map(row=><div className="record" key={idOf(row)}><div className="record-title"><code>{idOf(row)}</code><Status value={row.state}/></div><p>贡献者：{text(row.contributor_owner_id)}</p><RawData data={row}/></div>)}</div></QueryFeedback>
    </Panel>
    <RecallForm api={api} nodes={items(contributions.data)} onDone={()=>setRefresh(n=>n+1)}/>
    {service.mode==='cpu_reference'?<Panel title="CPU 参考实例供给" description="此实例的库存由启动配置载入。"><p>在线登记供给需要准确的逻辑时刻；CPU 参考实例的时钟持续前进，因此这里不提供手工登记。请在逻辑模拟实例登记供给。</p><p className="helper">已有自有 GPU 的收回通知仍可在上方提交。</p></Panel> :service.mode==='logical_simulation'?<ContributionForm api={api} onDone={()=>setRefresh(n=>n+1)}/>:<Panel title="登记模拟供给"><p>正在确认服务执行模式，确认后再开放供给登记。</p></Panel>}
  </>;
}
function ContributionForm({api,onDone}:{api:ApiClient;onDone:()=>void}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null),[result,setResult]=useState<ApiResult|null>(null);
  async function submit(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();setError(null);setResult(null);
    let payload:unknown;
    try {payload=JSON.parse(String(new FormData(event.currentTarget).get('node')));if(!object(payload).node_id)throw new Error('需要包含 node_id 的节点定义。');}
    catch(error){setError(new ApiError(error instanceof Error?error.message:'JSON 无法解析',422));return;}
    setBusy(true);
    try {setResult(await api.request('/x/v1/contributions',{method:'POST',body:payload}));onDone();}
    catch(error){setError(error instanceof ApiError?error:new ApiError('登记请求失败',0));}
    finally{setBusy(false);}
  }
  return <Panel title="登记模拟供给" description="使用 NodeInventory 契约登记节点。当前版本保留完整配置输入，后端会核验归属及资源一致性。"><form onSubmit={submit}><Field label="节点定义（JSON）" help={`contributor_owner_id 应与当前身份 ${api.identity.owner} 一致。包含 node_id、state、capacity、available、devices；observed_at 使用当前逻辑时间。`}><textarea name="node" rows={7} required placeholder="粘贴真实的模拟资源配置，不会自动生成库存。" disabled={busy}/></Field><button className="button" disabled={busy||Boolean(error?.uncertain)}>{busy?'正在登记…':'登记供给'}</button></form>{error&&<ErrorNotice error={error}/>} {result&&<Receipt result={result}/>}</Panel>;
}

export function RecallForm({api,nodes,onDone}:{api:ApiClient;nodes:JsonObject[];onDone:()=>void}) {
  const [busy,setBusy]=useState(false),[error,setError]=useState<ApiError|null>(null),[result,setResult]=useState<ApiResult|null>(null);
  const [selected,setSelected]=useState<string[]>([]);
  const devices=[...new Map(nodes.filter(node=>node.contributor_owner_id===api.identity.owner).flatMap(node=>list(node.devices).map(device=>[text(device.physical_gpu_id??device.device_id,''),{...device,node_id:node.node_id}] as const))).entries()].filter(([id])=>id);
  async function submit(event:FormEvent<HTMLFormElement>){
    event.preventDefault();setBusy(true);setError(null);setResult(null);
    const form=new FormData(event.currentTarget);
    try{setResult(await api.request('/x/v1/contributions/recall',{method:'POST',body:{gpu_ids:selected,notice_seconds:Number(form.get('notice')),duration_seconds:Number(form.get('duration'))}}));onDone();}
    catch(error){setError(error instanceof ApiError?error:new ApiError('收回请求结果不明',0));}
    finally{setBusy(false);}
  }
  return <Panel title="收回自有资源" description="选择自己的物理 GPU，提前通知正在使用它的工作；同一物理 GPU 的切片一并受影响。权限与占用状态由服务端核验。"><form onSubmit={submit}><fieldset disabled={busy||Boolean(error?.uncertain)}><legend>当前身份的物理 GPU</legend>{devices.length?<div className="device-choices">{devices.map(([id,device])=><label key={id}><input type="checkbox" checked={selected.includes(id)} onChange={e=>setSelected(values=>e.target.checked?[...values,id]:values.filter(value=>value!==id))}/><span><code>{id}</code> · {text(device.node_id)} · {text(object(device).model)}</span></label>)}</div>:<p className="empty">当前页没有属于此身份的 GPU。先登记供给，或刷新我的贡献。</p>}<div className="form-grid"><Field label="收回通知期（秒）"><input name="notice" type="number" min="1" max="300" defaultValue="30" required/></Field><Field label="收回占用时长（秒）"><input name="duration" type="number" min="1" max="3600" defaultValue="60" required/></Field></div></fieldset><button className="button primary" disabled={busy||!selected.length||Boolean(error?.uncertain)}>{busy?'正在通知…':'发送收回通知'}</button></form>{error&&<ErrorNotice error={error}/>} {error?.uncertain&&<p className="helper">结果尚不确定，请先检查关联事件和资源状态，避免重复提交收回通知。</p>}{result&&<Receipt result={result}/>}<p className="helper">收回后资源能否再次出租，以通知期限、占用期限及服务端账本为准。无需填写贡献 ID。</p></Panel>;
}
