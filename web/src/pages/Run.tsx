import {CopyWorkId} from '../components/CopyWorkId';
import {useRef, useState, type FormEvent} from 'react';
import type {WorkIntentRegistration} from '../generated/contracts';
import {ApiClient, ApiError, encodeId, idOf, items, list, object, text, type ApiResult} from '../api';
import {useQuery} from '../hooks';
import {modeLabel,useService} from '../service';
import {workName} from '../workName';
import {StopStatus} from '../components/CP2';
import {Field, Metadata, Panel, QueryFeedback, RawData, Receipt, Refresh, Status, Table, ErrorNotice, ActionButton} from '../components/common';

const terminalStates = new Set(['completed', 'failed', 'cancelled']);

export function CreateWork({api, onCreated, onOpen}: {api: ApiClient; onCreated: () => void; onOpen: (id: string) => void}) {
  const service=useService();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<ApiResult | null>(null);
  const [intentId, setIntentId] = useState('');
  const [advanced, setAdvanced] = useState(false);
  const [commandError, setCommandError] = useState('');
  const [nameError, setNameError] = useState('');
  const registration = useRef<WorkIntentRegistration | null>(null);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setError(null); setResult(null); setCommandError(''); setNameError('');
    if(!service.mode){setError(new ApiError('尚未确认服务执行模式，请重新检查连接。',409));return;}
    if (!registration.current) {
      const form = new FormData(event.currentTarget);
      const displayName = String(form.get('display_name') ?? '').trim();
      if (!displayName || Array.from(displayName).length > 80) {
        setNameError(displayName ? '工作名称不能超过 80 个字符。' : '请输入工作名称。');
        (event.currentTarget.elements.namedItem('display_name') as HTMLInputElement | null)?.focus();
        return;
      }
      try {
        const command: unknown = JSON.parse(String(form.get('command')));
        if (!Array.isArray(command) || !command.length || command.some(x => typeof x !== 'string')) throw new Error('command 必须是非空的字符串 JSON 数组。');
        const id = `work-${crypto.randomUUID()}`;
        registration.current = {
          intent_id: id, display_name: displayName, owner_id: api.identity.owner, execution_mode: service.mode,
          settlement_action: String(form.get('settlement')) as WorkIntentRegistration['settlement_action'],
          specification: {
            allocation: {kind: 'allocation.create', owner_id: api.identity.owner, ttl_seconds: Number(form.get('ttl')), priority: Number(form.get('priority')), preemption_mode: String(form.get('preemption')) as 'checkpoint' | 'restartable' | 'best_effort', checkpoint_timeout_seconds:Number(form.get('checkpoint_timeout')),max_restarts:Number(form.get('max_restarts')),
              request: {cpu_millicores: Number(form.get('cpu')), memory_mib: Number(form.get('memory')), gpu: {count: Number(form.get('gpu')), model: String(form.get('model')) || 'any', memory_mib: Number(form.get('vram')), profile: String(form.get('profile')) || null}}},
            job: {image: String(form.get('image')).trim(), command: command as string[], max_retries:Number(form.get('max_retries')), ...(form.get('timeout')?{timeout_seconds:Number(form.get('timeout'))}:{})},
          },
        };
        setIntentId(id);
      } catch (error) { const message=error instanceof Error ? error.message : '输入无法解析'; setCommandError(message); setAdvanced(true); setError(new ApiError(message, 422)); return; }
    }
    setBusy(true);
    try {
      const response = await api.registerWork(registration.current);
      setResult(response); onCreated();
    } catch (error) { setError(error instanceof ApiError ? error : new ApiError('登记失败', 0)); }
    finally { setBusy(false); }
  }
  return <Panel title="提交一项工作" description="设置所需资源，以及中断和结束后如何处理。">
    <div className="work-mode-note"><strong>{modeLabel(service.mode)}</strong><p className="helper simulation-note">{service.mode==='cpu_reference'?'运行服务端固定 CPU 参考程序，不执行填写的镜像与命令；GPU 库存仍为模拟。程序随真实时间运行。':service.mode==='logical_simulation'?<>任务采用 10 秒固定逻辑工期，现实等待不会推进时间；请管理员在<a href="#acceptance">系统验收</a>推进模拟时钟。不实际执行镜像命令，GPU 库存为模拟。</>:'确认服务模式后才能提交。'}</p></div>
    <form onSubmit={submit} onInvalidCapture={event=>{const disclosure=(event.target as HTMLElement).closest('details.work-disclosure');if(disclosure&&event.currentTarget.contains(disclosure)){disclosure.setAttribute('open','');setAdvanced(true);}}}>
      <fieldset disabled={busy || Boolean(registration.current)} className="work-form-fields">
        <legend>工作信息</legend>
        <Field label="工作名称"><input name="display_name" required autoComplete="off" aria-invalid={Boolean(nameError)} aria-describedby={nameError?'work-name-error':undefined} onChange={()=>setNameError('')}/>{nameError&&<small id="work-name-error" role="alert">{nameError}</small>}</Field>
        <h3 className="work-form-heading">资源需求</h3>
        <div className="form-grid">
        <Field label="CPU（millicores）"><input name="cpu" type="number" min="0" defaultValue="1000" required/></Field>
        <Field label="内存（MiB）"><input name="memory" type="number" min="0" defaultValue="4096" required/></Field>
        <Field label="GPU 数量"><input name="gpu" type="number" min="0" max="16" defaultValue="1" required/></Field>
        <Field label="GPU 型号"><input name="model" defaultValue="any" required/></Field>
        <Field label="每卡最低显存（MiB）"><input name="vram" type="number" min="0" defaultValue="0" required/></Field>
        <Field label="租约时长（秒）"><input name="ttl" type="number" min="30" max="3600" defaultValue="300" required/></Field>
        </div>
        <h3 className="work-form-heading">中断与结束</h3>
        <div className="form-grid work-form-pair">
        <Field label="中断处理"><select name="preemption" defaultValue="best_effort"><option value="checkpoint" disabled={service.capabilities.checkpoint_recovery!==true}>保存点恢复</option><option value="restartable">允许重启</option><option value="best_effort">尽力运行</option></select></Field>
        <Field label="租约结算"><select name="settlement" defaultValue="keep_lease"><option value="keep_lease">保留租约，手动归还</option><option value="release_on_success">成功后申请自动归还</option><option value="release_on_terminal">结束后申请自动归还</option></select></Field>
        </div>
        <p className="helper">保留租约时，工作结束后仍需手动释放资源。</p>
        <details className="work-disclosure" open={advanced} onToggle={event=>setAdvanced(event.currentTarget.open)}>
          <summary>高级设置</summary>
          <div className="form-grid">
        <Field label="MIG-like profile（可选）"><input name="profile" placeholder="例如 mig-2g.20gb"/></Field>
        <Field label="优先级"><input name="priority" type="number" min="0" max="100" defaultValue="0" required/></Field>
        <Field label="保存报告窗口（秒）"><input name="checkpoint_timeout" type="number" min="1" max="300" defaultValue="15" required/></Field>
        <Field label="收回恢复次数上限"><input name="max_restarts" type="number" min="0" max="20" defaultValue="2" required/></Field>
        <Field label="任务失败重试次数上限"><input name="max_retries" type="number" min="0" max="10" defaultValue="0" required/></Field>
        <Field label="任务超时（秒，可选）"><input name="timeout" type="number" min="1" max="86400" placeholder="不填使用服务默认"/></Field>
          </div>
          <h3 className="work-form-heading">登记信息</h3>
          <p className="helper">镜像与命令作为工作元数据登记。当前服务不会执行这些内容。</p>
          <div className="form-grid work-form-pair">
            <Field label="任务镜像"><input name="image" required defaultValue="python:3.12-slim"/></Field>
            <Field label="启动命令（JSON 字符串数组）"><input name="command" required defaultValue='["python", "-V"]' aria-invalid={Boolean(commandError)} aria-describedby={commandError?'work-command-error':undefined}/>{commandError&&<small id="work-command-error" role="alert">{commandError}</small>}</Field>
          </div>
        </details>
      </fieldset>
      <div className="actions"><button className="button primary" disabled={busy || Boolean(result) || !api.identity.owner || !service.mode || Boolean(error?.uncertain)}>{busy ? '正在登记…' : intentId ? '重试同一工作登记' : '提交工作'}</button>{intentId && <button type="button" className="button" disabled={busy} onClick={() => onOpen(intentId)}>查询这项工作</button>}{intentId && <button type="button" className="button quiet" disabled={busy} onClick={() => {
        if (window.confirm('新建另一项工作不会取消之前的请求。若结果不明，请先查询原工作。确定准备另一项工作？')) { registration.current = null; setIntentId(''); setError(null); setResult(null); setCommandError(''); setNameError(''); }
      }}>准备另一项工作</button>}</div>
    </form>
    {intentId && <p className="helper"><strong>{workName(registration.current, intentId)}</strong> <CopyWorkId id={intentId} scope={api} compact/></p>}
    {error && <ErrorNotice error={error}/>} {error?.uncertain&&<p className="helper">请先查询这项工作确认登记结果，已阻止重复提交。</p>}{result && <Receipt result={result}/>}
  </Panel>;
}

export function RunPage({api, onOpen}: {api: ApiClient; onOpen: (id: string) => void}) {
  const service=useService();
  const createPanel=useRef<HTMLDetailsElement>(null);
  const openCreate=()=>{if(!createPanel.current)return;createPanel.current.open=true;requestAnimationFrame(()=>{createPanel.current?.scrollIntoView({block:'start',behavior:'auto'});(createPanel.current?.querySelector('input') as HTMLInputElement|null)?.focus({preventScroll:true});});};
  const [refresh, setRefresh] = useState(0);
  const [cursors, setCursors] = useState<string[]>(['']);
  const cursor = cursors[cursors.length - 1];
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('unfinished');
  const query = useQuery(api, `/x/v1/work-intents?limit=25${cursor ? `&after=${encodeURIComponent(cursor)}` : ""}`, refresh);
  const allocations = useQuery(api, '/v1/allocations?limit=50', refresh);
  const jobs = useQuery(api, '/v1/jobs?limit=50', refresh);
  const loaded = items(query.data);
  const rows = loaded.filter(row => (status==='all'||(status==='unfinished'?!terminalStates.has(text(row.state)):text(row.state)===status))&&`${workName(row)} ${idOf(row)} ${text(row.state)} ${text(object(object(row.specification).job).image)}`.toLowerCase().includes(search.toLowerCase()));
  const reload = () => { setRefresh(n => n + 1); };
  return <div className="work-experience">
    <Panel title="我的工作" description="查看进行中的工作，或打开详情处理结果和资源归还。" actions={<div className="panel-actions"><Refresh loading={query.loading} onClick={reload}/><button className="button primary" type="button" disabled={!api.identity.owner} onClick={openCreate}>新建工作</button></div>}>
      <div className="work-list-filters">
        <input className="search" aria-label="筛选当前页工作" placeholder="查找当前页的名称、ID 或镜像" value={search} onChange={e => setSearch(e.target.value)}/>
        <select className="search work-status-filter" aria-label="工作状态筛选" value={status} onChange={e=>setStatus(e.target.value)}><option value="unfinished">未完成</option><option value="all">全部状态</option><option value="running">运行中</option><option value="waiting">等待中</option><option value="pending">待处理</option><option value="manual_action">需要处理</option><option value="completed">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option></select>
      </div>
      <p className="helper work-page-scope">仅筛选当前已加载页{query.data?`：显示 ${rows.length} / ${loaded.length} 项`:''}。切换分页可查看其他工作。</p>
      <QueryFeedback query={query}>
        {rows.length ? <Table rows={rows} rowKey={idOf} columns={[
          {label:'工作',render:r=><div className="work-list-identity"><div className="work-id-with-copy"><button className="link-button" onClick={()=>onOpen(idOf(r))} title={idOf(r)}>{workName(r)}</button><CopyWorkId id={idOf(r)} scope={api} compact/></div><small>{text(object(object(r.specification).job).image,'未提供镜像登记信息')}</small></div>},
          {label:'状态',render:r=><Status value={r.state}/>},
          {label:'执行模式',render:r=>modeLabel(r.execution_mode)},
          {label:'操作',render:r=><button className="button quiet" aria-label={`查看工作 ${idOf(r)}`} onClick={()=>onOpen(idOf(r))}>查看详情</button>},
        ]}/> : <div className="empty">{loaded.length?'当前页没有符合筛选条件的工作。':'当前页暂无工作，可以在下方新建工作。'}{loaded.length>0&&<button className="button quiet" onClick={()=>{setStatus('all');setSearch('');}}>清除当前页筛选</button>}</div>}
        <div className="actions"><button className="button quiet" disabled={cursors.length === 1} onClick={()=>setCursors(values=>values.slice(0,-1))}>上一页</button><span className="helper">第 {cursors.length} 页</span><button className="button quiet" disabled={typeof object(query.data).next_cursor !== 'string'} onClick={()=>setCursors(values=>[...values,object(query.data).next_cursor as string])}>下一页</button></div>
        <details className="work-disclosure work-technical"><summary>列表技术信息</summary><Metadata data={query.data}/></details>
      </QueryFeedback>
    </Panel>
    <details ref={createPanel} className="work-create work-disclosure"><summary>新建工作</summary><CreateWork api={api} onCreated={()=>{setCursors(['']);reload();}} onOpen={onOpen}/></details>
    <details className="work-disclosure work-supporting-records"><summary>租约与执行记录</summary>
    <Panel title="资源租约" description="公共租约默认保留；工作意图按结算选择处理，以租约状态为准。" actions={<Refresh loading={allocations.loading} onClick={allocations.refresh}/>}>
      <Metadata data={allocations.data}/><QueryFeedback query={allocations} empty={!items(allocations.data).length}><div className="record-list">{items(allocations.data).map(row=><div className="record" key={idOf(row)}><div className="record-title"><code>{idOf(row)}</code><Status value={row.state}/></div><StopStatus allocation={row}/><div className="actions">
        <ActionButton api={api} path={`/v1/allocations/${encodeId(idOf(row))}/renew`} body={{ttl_seconds:300}} onDone={reload} disabled={Boolean(row.pending_terminal)||!['Allocated','Running'].includes(text(row.state))}>续租 300 秒</ActionButton>
        <ActionButton api={api} path={`/v1/allocations/${encodeId(idOf(row))}/resume`} body={{}} onDone={reload} disabled={service.capabilities.checkpoint_recovery!==true||!list(object(allocations.data).legal_actions).some(a=>a.operation==='allocation.resume'&&object(a.entity).entity_id===idOf(row))}>恢复租约</ActionButton>
        <ActionButton api={api} path={`/v1/allocations/${encodeId(idOf(row))}`} method="DELETE" confirmation="确定释放此租约？这会归还资源，并可能中止其上的工作。" onDone={reload} disabled={Boolean(row.pending_terminal)||['Released','Expired','Rejected','Evicted'].includes(text(row.state))}>释放租约</ActionButton>
      </div><RawData data={row}/></div>)}</div></QueryFeedback>
    </Panel>
    <Panel title="执行任务" description="一个工作可以经历多次执行。取消任务后仍需核对租约去向。" actions={<Refresh loading={jobs.loading} onClick={jobs.refresh}/>}>
      <Metadata data={jobs.data}/><QueryFeedback query={jobs} empty={!items(jobs.data).length}><Table rows={items(jobs.data)} rowKey={idOf} columns={[
        {label:'任务 ID',render:r=><code>{text(r.job_id ?? r.id)}</code>},{label:'租约',render:r=><code>{text(r.allocation_id)}</code>},{label:'状态',render:r=><Status value={r.state}/>},{label:'当前代次 / 失败重试',render:r=>`#${text(object(r.attempt).generation)} / ${text(r.retry_count)}`},
        {label:'操作',render:r=><ActionButton api={api} method="DELETE" path={`/v1/jobs/${encodeId(text(r.job_id ?? r.id,''))}`} confirmation="确定取消这次任务执行？公共租约默认保留；关联工作意图会按结算选择处理，请核对最终租约状态。" onDone={reload} disabled={['Succeeded','Failed','Cancelled'].includes(text(r.state))}>取消任务</ActionButton>},
      ]}/></QueryFeedback>
    </Panel>
    </details>
  </div>;
}
