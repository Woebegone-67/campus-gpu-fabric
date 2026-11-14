import {useState, type FormEvent} from 'react';
import {ApiClient, ApiError, idOf, items, list, object, text, type ApiResult} from '../api';
import {useQuery} from '../hooks';
import {ErrorNotice, Field, Metadata, Panel, QueryFeedback, RawData, Receipt, Refresh, Status, Table} from '../components/common';

export function ResourcesPage({api}: {api: ApiClient}) {
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
    <Panel title="收回自有资源" description="召回能力将在 CP2 接入，当前暂不可提交。贡献 ID 须由后端提供，节点 ID 不自动视为贡献 ID。">
      <fieldset disabled className="form-grid"><Field label="贡献 ID"><input name="contribution" placeholder="填写后端提供的贡献 ID"/></Field><Field label="GPU ID（逗号分隔）"><input name="devices" placeholder="gpu-1, gpu-2"/></Field><Field label="通知期（秒）"><input name="notice" type="number" defaultValue="30"/></Field><Field label="占用时长（秒）"><input name="duration" type="number" defaultValue="300"/></Field></fieldset><button className="button primary" disabled>收回通知 · CP2 待接入</button>
    </Panel>
    <ContributionForm api={api} onDone={()=>setRefresh(n=>n+1)}/>
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
  return <Panel title="登记模拟供给" description="使用 NodeInventory 契约登记节点。当前版本保留完整配置输入，后端会核验归属及资源一致性。"><form onSubmit={submit}><Field label="节点定义（JSON）" help={`contributor_owner_id 应与当前身份 ${api.identity.owner} 一致。包含 node_id、state、capacity、available、devices、observed_at。`}><textarea name="node" rows={7} required placeholder="粘贴真实的模拟资源配置，不会自动生成库存。" disabled={busy}/></Field><button className="button" disabled={busy}>{busy?'正在登记…':'登记供给'}</button></form>{error&&<ErrorNotice error={error}/>} {result&&<Receipt result={result}/>}</Panel>;
}
