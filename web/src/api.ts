export type JsonObject = Record<string, unknown>;
export type Identity = { owner: string; token: string; profile?: string };
export type ApiResult = { data: unknown; status: number; requestId: string | null };

export function object(value: unknown): JsonObject {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {};
}
export function text(value: unknown, fallback = '—'): string {
  return typeof value === 'string' || typeof value === 'number' ? String(value) : fallback;
}
export function list(value: unknown): JsonObject[] {
  return Array.isArray(value) ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item)) as JsonObject[] : [];
}
export function items(data: unknown): JsonObject[] {
  if (Array.isArray(data)) return list(data);
  return list(object(data).items);
}
export function detail(data: unknown): JsonObject {
  const root = object(data);
  return root.item !== undefined ? object(root.item) : root;
}
export function idOf(row: JsonObject): string {
  return text(row.intent_id ?? row.job_id ?? row.allocation_id ?? row.contribution_id ?? row.node_id ?? row.id, '');
}
export function versionOf(data: unknown): {epoch: number; revision: number} | null {
  const version = object(object(data).version);
  return Number.isSafeInteger(version.epoch) && Number.isSafeInteger(version.revision)
    ? {epoch: version.epoch as number, revision: version.revision as number} : null;
}
export class ApiError extends Error {
  constructor(message: string, public status: number, public requestId: string | null = null, public data: unknown = null) {
    super(message);
    this.name = 'ApiError';
  }
  get uncertain() { return this.status === 0 || this.status >= 500; }
}
export class ApiClient {
  constructor(public identity: Identity, private onMutating: (amount: number) => void = () => {}) {}
  async workAction(intentId: string, action: string, version: {epoch:number;revision:number}, stageId?:string): Promise<ApiResult> {
    const path=`/x/v1/work-intents/${encodeId(intentId)}`;
    let expected=version;
    // Keep identity selection locked across the refresh between rejected commands.
    this.onMutating(1);
    try {
      for(let attempt=0;;attempt++) {
        try {
          return await this.request(`${path}/actions`,{method:'POST',body:{action,expected_version:expected,...(stageId?{stage_id:stageId}:{})}});
        } catch(error) {
          // A known version rejection did not commit. Unknown/network failures are
          // never automatically replayed, particularly cancel/release/resume.
          if(!(error instanceof ApiError)||error.status!==409||apiErrorCode(error)!=='stale_version'||attempt>=2)throw error;
          const fresh=await this.request(path);
          const next=versionOf(fresh.data);
          if(!next||next.epoch!==version.epoch)throw new ApiError('实例代次已改变，请重新查询工作后再操作。',409,null,{error:'epoch_changed'});
          expected=next;
        }
      }
    } finally {this.onMutating(-1);}
  }
  async request(path: string, options: {method?: string; body?: unknown; signal?: AbortSignal; idempotencyKey?: string} = {}): Promise<ApiResult> {
    if (!path.startsWith('/v1/') && !path.startsWith('/x/v1/') && path !== '/healthz') throw new Error('不支持的产品接口');
    const method = options.method || 'GET';
    const mutation = method !== 'GET';
    const headers: Record<string, string> = {Accept: 'application/json'};
    if (this.identity.owner) headers['X-Owner'] = this.identity.owner;
    if (this.identity.token) headers.Authorization = `Bearer ${this.identity.token}`;
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
    if (options.idempotencyKey) headers['Idempotency-Key'] = options.idempotencyKey;
    const controller = new AbortController();
    const abort = () => controller.abort(options.signal?.reason);
    options.signal?.addEventListener('abort', abort, {once: true});
    if (options.signal?.aborted) abort();
    let timedOut = false;
    const timeout = window.setTimeout(() => { timedOut = true; controller.abort(); }, 20000);
    if (mutation) this.onMutating(1);
    try {
      const response = await fetch(path, {method, headers, body: options.body !== undefined ? JSON.stringify(options.body) : undefined, signal: controller.signal, cache: 'no-store'});
      const raw = await response.text();
      let data: unknown = null;
      try { data = raw ? JSON.parse(raw) : null; } catch {
        throw new ApiError('服务返回了非 JSON 响应，请检查 API 代理与服务地址。', response.status >= 400 ? response.status : 502);
      }
      const record = object(data);
      const requestId = response.headers.get('X-Request-ID') || (typeof record.request_id === 'string' ? record.request_id : null);
      if (!response.ok) throw new ApiError(text(record.detail, text(record.error, `请求失败：HTTP ${response.status}`)), response.status, requestId, data);
      const receipt = object(record.receipt ?? record.commit_receipt ?? record);
      if (receipt.outcome === 'rejected') {
        throw new ApiError(text(object(receipt.error).detail, '服务端拒绝了这项操作。'), 409, requestId, data);
      }
      return {data, status: response.status, requestId};
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (options.signal?.aborted) throw error;
      throw new ApiError(timedOut ? '请求超过 20 秒，尚不能确认服务端结果。请先查询状态。' : '无法连接服务，尚不能确认请求结果。', 0);
    } finally {
      window.clearTimeout(timeout);
      options.signal?.removeEventListener('abort', abort);
      if (mutation) this.onMutating(-1);
    }
  }
}
export function apiErrorCode(error:ApiError):string {
  const record=object(error.data),receipt=object(record.receipt??record.commit_receipt??record);
  return text(record.error,text(object(receipt.error).code,''));
}
export const encodeId = (value: string) => encodeURIComponent(value);
