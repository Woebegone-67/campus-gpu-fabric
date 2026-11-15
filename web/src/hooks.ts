import {createContext, useContext, useCallback, useEffect, useRef, useState, type FormEvent} from 'react';
import {ApiClient, ApiError, encodeId, versionOf} from './api';

export type QueryState = {data: unknown; error: ApiError | null; loading: boolean; receivedAt: number | null; refresh: () => void};
export const QueryActivityContext = createContext(true);
export const QueryEpochContext = createContext<(epoch:number)=>boolean>(()=>true);
export function useQuery(api: ApiClient, path: string | null, refreshToken = 0): QueryState {
  const active = useContext(QueryActivityContext);
  const acceptEpoch = useContext(QueryEpochContext);
  const [reload, setReload] = useState(0);
  const [state, setState] = useState<{key: string; data: unknown; error: ApiError | null; loading: boolean; receivedAt: number | null}>({key: '', data: null, error: null, loading: false, receivedAt: null});
  const generation = useRef(0);
  const key = `${api.identity.owner}\0${api.identity.token}\0${api.identity.actorId||''}\0${path}`;
  useEffect(() => {
    const current = ++generation.current;
    if (!active) return;
    if (!path) { setState({key, data: null, error: null, loading: false, receivedAt: null}); return; }
    const controller = new AbortController();
    // Same-query refresh retains an explicitly dated snapshot; identities and paths never share it.
    setState(previous => previous.key === key ? {...previous, error: null, loading: true} : {key, data: null, error: null, loading: true, receivedAt: null});
    api.request(path, {signal: controller.signal}).then(({data}) => {
      if (generation.current !== current || controller.signal.aborted) return;
      const version=versionOf(data);
      if(version&&!acceptEpoch(version.epoch)) {setState({key,data:null,error:new ApiError('实例代次已更新，请重新读取当前记录。',409,null,{error:'epoch_changed'}),loading:false,receivedAt:null});return;}
      setState({key, data, error: null, loading: false, receivedAt: Date.now()});
    }).catch(error => {
      if (generation.current !== current || controller.signal.aborted) return;
      const failure = error instanceof ApiError ? error : new ApiError('读取失败。', 0);
      const revoked = failure.status >= 400 && failure.status < 500;
      setState(previous => !revoked && previous.key === key ? {...previous, error: failure, loading: false} : {key, data: null, error: failure, loading: false, receivedAt: null});
    });
    return () => { controller.abort(); };
  }, [api, path, reload, refreshToken, key, active, acceptEpoch]);
  const refresh = useCallback(() => setReload(value => value + 1), []);
  return {...(state.key === key ? state : {data: null, error: null, loading: Boolean(path), receivedAt: null}), refresh};
}

export function useLookupQuery(api: ApiClient, basePath: string, initialId = '', onSelect?: (id: string) => void) {
  const [input, setInput] = useState(initialId);
  const [id, setId] = useState(initialId);
  const query = useQuery(api, id ? `${basePath}/${encodeId(id)}` : null);
  function submit(event: FormEvent) {
    event.preventDefault();
    const next = input.trim();
    if (!next) return;
    if (next === id) query.refresh();
    else if (onSelect) onSelect(next);
    else setId(next);
  }
  function open(next:string) {setInput(next);if(next===id)query.refresh();else setId(next);}
  return {input, setInput, id, query, submit, open};
}
