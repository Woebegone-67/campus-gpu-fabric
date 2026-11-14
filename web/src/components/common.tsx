import {useState, type ReactNode} from 'react';
import {ApiError, ApiClient, object, text, versionOf, type ApiResult, type JsonObject} from '../api';
import type {QueryState} from '../hooks';

const labels: Record<string, string> = {
  Pending: '等待分配', Allocated: '已获资源', Running: '运行中', PreemptRequested: '收到收回通知',
  Preempted: '已中断', Released: '已释放', Expired: '已过期', Evicted: '已驱逐', Rejected: '已拒绝',
  Succeeded: '已完成', Failed: '失败', Cancelled: '已取消', Checkpointing: '保存中',
  stopping: '正在停止', quarantined: '隔离中', interrupted: '已中断', pending: '待处理', waiting: '等待中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消',
  manual_action: '需要处理', committed: '已提交', rejected: '已拒绝', noop: '无新增效果',
  ready: '收到 ready 报告', verified: '已核验', unverified: '未核验', applied: '已接续', not_attempted: '未接续',
  MET: '已通过', NOT_MET: '未通过', NOT_RUN: '未运行', available: '可用', occupied: '占用', unavailable: '不可用',
  legal: '可行', infeasible: '不可行', unexamined: '未检查',
};
export function Status({value}: {value: unknown}) {
  const key = text(value, 'unknown');
  return <span className={`status status-${key.toLowerCase()}`}>{labels[key] || key}</span>;
}
export function ErrorNotice({error}: {error: ApiError}) {
  const pending = error.status === 501;
  const unavailable = error.status === 503;
  const title = pending ? '此能力等待后端接入' : unavailable ? '服务暂时不可用' : error.status === 403 || error.status === 401 ? '当前身份无权访问' : '请求未完成';
  return <div className={`feedback ${pending || unavailable ? 'warning' : 'error'}`} role="alert"><strong>{title}</strong><p>{error.message}</p><small>{error.status ? `HTTP ${error.status}` : '连接或响应中断'}{error.requestId ? ` · 请求 ${error.requestId}` : ''}</small></div>;
}
export function QueryFeedback({query, empty, children}: {query: QueryState; empty?: boolean; children: ReactNode}) {
  if (query.loading) return <div className="empty" role="status">正在读取服务端状态…</div>;
  if (query.error) return <ErrorNotice error={query.error}/>;
  if (query.data === null) return null;
  if (empty) return <div className="empty">当前授权范围内没有记录。新记录会在服务端接受后显示。</div>;
  return <>{children}</>;
}
export function LookupStatus({query}: {query: QueryState}) {
  return <p className="helper lookup-status" role="status" aria-live="polite">{query.loading ? '正在查询…' : query.receivedAt ? `已读取服务端状态 · 本机 ${new Date(query.receivedAt).toLocaleTimeString()}` : ''}</p>;
}
export function Metadata({data}: {data: unknown}) {
  const version = versionOf(data);
  const now = object(data).now;
  if (!version && now === undefined) return null;
  return <div className="metadata">{version && <span>版本 {version.epoch}:{version.revision}</span>}{now !== undefined && <span>逻辑时间 {text(now)} s</span>}</div>;
}
export function RawData({data, label = '查看服务端原始记录'}: {data: unknown; label?: string}) {
  return <details className="raw"><summary>{label}</summary><pre>{JSON.stringify(data, null, 2)}</pre></details>;
}
export function Panel({title, description, actions, children}: {title: string; description?: string; actions?: ReactNode; children: ReactNode}) {
  return <section className="panel"><div className="panel-heading"><div><h2>{title}</h2>{description && <p>{description}</p>}</div>{actions}</div>{children}</section>;
}
export function Refresh({onClick, loading = false}: {onClick: () => void; loading?: boolean}) { return <button type="button" className="button quiet" disabled={loading} onClick={onClick}>{loading?'正在刷新…':'刷新'}</button>; }
export function ActionButton({api, path, method = 'POST', body, children, confirmation, onDone, request, disabled = false}: {api: ApiClient; path: string; method?: string; body?: unknown; children: ReactNode; confirmation?: string; onDone?: () => void; request?: () => Promise<ApiResult>; disabled?: boolean}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [result, setResult] = useState<ApiResult | null>(null);
  async function run() {
    if (confirmation && !window.confirm(confirmation)) return;
    setBusy(true); setError(null); setResult(null);
    try { setResult(await (request ? request() : api.request(path, {method, body})));  onDone?.(); }
    catch (error) { setError(error instanceof ApiError ? error : new ApiError('操作失败', 0)); }
    finally { setBusy(false); }
  }
  return <div className="action-unit"><button className="button" disabled={disabled || busy || Boolean(error?.uncertain)} onClick={run}>{busy ? '处理中…' : children}</button>{error && <ErrorNotice error={error}/>} {error?.uncertain&&<p className="helper">结果尚不确定，已阻止重复提交。请先刷新当前状态，再决定下一步。</p>}{result && <Receipt result={result}/>}</div>;
}
export function Receipt({result}: {result: ApiResult}) {
  const receipt = object(object(result.data).receipt ?? object(result.data).commit_receipt ?? result.data);
  return <div className="feedback success" role="status"><strong>{result.status === 202 ? '服务已接受请求，最终状态以查询结果为准' : '已收到服务端回执'}</strong>{receipt.outcome !== undefined && <p><Status value={receipt.outcome}/></p>}<small>HTTP {result.status}{result.requestId ? ` · ${result.requestId}` : ''}</small><RawData data={result.data} label="查看操作回执"/></div>;
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
