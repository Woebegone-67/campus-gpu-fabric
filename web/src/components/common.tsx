import {useRef,useState, type ReactNode} from 'react';
import {ApiError, ApiClient, object, text, versionOf, type ApiResult, type JsonObject} from '../api';
import type {QueryState} from '../hooks';
import {MotionReveal} from '../motion';

const labels: Record<string, string> = {
  Pending: '等待分配', Allocated: '已获资源', Running: '运行中', PreemptRequested: '收到收回通知',
  Preempted: '已中断', Released: '已释放', Expired: '已过期', Evicted: '已驱逐', Rejected: '已拒绝',
  Succeeded: '已完成', Failed: '失败', Cancelled: '已取消', Checkpointing: '保存中',
  stopping: '正在停止', quarantined: '隔离中', interrupted: '已中断', pending: '待处理', waiting: '等待中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消',
  manual_action: '需要处理', committed: '已提交', rejected: '已拒绝', noop: '无新增效果', success: '已完成', failure: '失败', stopped: '已停止', created: '已登记', starting: '正在启动',
  ready: '收到 ready 报告', verified: '已核验', unverified: '未核验', applied: '已接续', not_attempted: '未接续',
  MET: '已通过', NOT_MET: '未通过', NOT_RUN: '未运行', available: '可用', occupied: '占用', unavailable: '不可用',
  Ready:'在线', NotReady:'离线', Down:'离线', Draining:'收回中',
  legal: '可行', infeasible: '不可行', unexamined: '未检查',
};
export function Status({value}: {value: unknown}) {
  const key = text(value, 'unknown');
  return <span className={`status status-${key.toLowerCase()}`}>{labels[key] || key}</span>;
}
export function ErrorNotice({error}: {error: ApiError}) {
  const code=text(object(error.data).error,text(object(object(error.data).error).code,''));
  const messages:Record<string,string>={restart_budget_exhausted:'可恢复次数已用完。',lease_expired:'租约已到期。',lease_unavailable:'当前资源暂不可用。',stale_version:'状态已更新，请刷新后再操作。',epoch_changed:'实例已更新，请重新打开这项工作。'};
  const message=messages[code]??({401:'请重新登录。',403:'当前账户不能执行此操作。',404:'找不到这条记录。',501:'此操作暂不可用。',503:'服务暂时不可用，请稍后重试。'} as Record<number,string>)[error.status]??error.message;
  return <div className={`feedback ${error.status===503?'warning':'error'}`} role="alert"><strong>{error.uncertain?'暂未确认结果':'操作未完成'}</strong><p>{message}</p><details className="raw"><summary>错误详情</summary><p>{error.message}</p><small>{error.status?`HTTP ${error.status}`:'连接中断'}{error.requestId?` · ${error.requestId}`:''}</small></details></div>;
}
export function QueryFeedback({query, empty, children}: {query: QueryState; empty?: boolean; children: ReactNode}) {
  if (query.loading && query.data === null) return <div className="empty" role="status">加载中…</div>;
  if (query.error && query.data === null) return <ErrorNotice error={query.error}/>;
  if (query.data === null) return null;
  return <>{query.loading&&<p className="helper query-refreshing" role="status">更新中…</p>}{query.error&&<div className="feedback warning" role="status"><strong>未能更新</strong><p>显示 {query.receivedAt?new Date(query.receivedAt).toLocaleTimeString():'上次'} 的记录。</p><details><summary>错误详情</summary><ErrorNotice error={query.error}/></details></div>}<MotionReveal trigger={query.data}>{empty?<div className="empty">暂无记录</div>:children}</MotionReveal></>;
}
export function LookupStatus({query}: {query: QueryState}) {
  return query.loading||query.receivedAt?<p className="helper lookup-status" role="status" aria-live="polite">{query.loading?'更新中…':`更新于 ${new Date(query.receivedAt!).toLocaleTimeString()}`}</p>:null;
}
export function Metadata({data}: {data: unknown}) {
  const version = versionOf(data);
  const now = object(data).now;
  if (!version && now === undefined) return null;
  return <div className="metadata">{version && <span>版本 {version.epoch}:{version.revision}</span>}{now !== undefined && <span>逻辑时间 {text(now)} s</span>}</div>;
}
export function RawData({data, label = '技术详情'}: {data: unknown; label?: string}) {
  return <details className="raw"><summary>{label}</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>;
}
export function Panel({title, description, actions, children}: {title: string; description?: string; actions?: ReactNode; children: ReactNode}) {
  return <section className="panel"><div className="panel-heading"><div><h2>{title}</h2>{description && <p>{description}</p>}</div>{actions}</div>{children}</section>;
}
export function Refresh({onClick, loading = false}: {onClick: () => void; loading?: boolean}) { return <button type="button" className="button quiet" disabled={loading} onClick={onClick}>{loading?'正在刷新…':'刷新'}</button>; }
export function ActionButton({api, path, method = 'POST', body, children, confirmation, onDone, onResult, request, disabled = false}: {api: ApiClient; path: string; method?: string; body?: unknown; children: ReactNode; confirmation?: string; onDone?: () => void; onResult?: (result:ApiResult) => void; request?: () => Promise<ApiResult>; disabled?: boolean}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<ApiResult | null>(null);
  const inFlight=useRef(false);
  async function run() {
    if(disabled||inFlight.current||error?.uncertain)return;
    if (confirmation && !window.confirm(confirmation)) return;
    inFlight.current=true;setBusy(true); setError(null); setResult(null);
    try { const response=await (request ? request() : api.request(path, {method, body}));setResult(response);onResult?.(response);onDone?.(); }
    catch (error) { setError(error instanceof ApiError ? error : new ApiError('操作失败', 0)); }
    finally { inFlight.current=false;setBusy(false); }
  }
  return <div className="action-unit"><button className="button" disabled={disabled || busy || Boolean(error?.uncertain)} onClick={run}>{busy ? '处理中…' : children}</button>{error && <ErrorNotice error={error}/>} {error?.uncertain&&<p className="helper">请先刷新确认结果。</p>}{result && <Receipt result={result}/>}</div>;
}
export function Receipt({result}: {result: ApiResult}) {
  const receipt = object(object(result.data).receipt ?? object(result.data).commit_receipt ?? result.data);
  const rejected=receipt.outcome==='rejected',error=object(receipt.error);
  return <div className={`feedback ${rejected?'error':'success'} receipt-compact`} role={rejected?'alert':'status'}><strong>{rejected?'操作未完成':receipt.outcome==='noop'?'无需更改':'已提交'}</strong>{rejected&&<p>{text(error.detail,'请查看错误详情。')}</p>}<details className="raw"><summary>操作记录</summary><small>HTTP {result.status}{result.requestId?` · ${result.requestId}`:''}</small><pre>{JSON.stringify(result.data,null,2)}</pre></details></div>;
}
export function Field({label, children, help}: {label: string; children: ReactNode; help?: string}) { return <label className="field"><span>{label}</span>{children}{help && <small>{help}</small>}</label>; }
export function Table({columns, rows, rowKey}: {columns: {label: string; render: (row: JsonObject) => ReactNode}[]; rows: JsonObject[]; rowKey: (row: JsonObject, index: number) => string}) {
  return <div className="table-wrap"><table><thead><tr>{columns.map(col => <th key={col.label}>{col.label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={rowKey(row,index)}>{columns.map(col => <td key={col.label}>{col.render(row)}</td>)}</tr>)}</tbody></table></div>;
}
export function downloadJson(data: unknown, name: string) {
  downloadJsonText(JSON.stringify(data, null, 2), name);
}
export function downloadJsonText(raw: string, name: string) {
  // Preserve server integers and artifact hashes; a JS number roundtrip can lose bits.
  const url = URL.createObjectURL(new Blob([raw], {type: 'application/json'}));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = name; anchor.click(); URL.revokeObjectURL(url);
}
