import {useCallback, useEffect, useRef, useState, type FormEvent} from 'react';
import {ApiClient, ApiError, encodeId} from './api';

export type QueryState = {data: unknown; error: ApiError | null; loading: boolean; receivedAt: number | null; refresh: () => void};
export function useQuery(api: ApiClient, path: string | null, refreshToken = 0): QueryState {
  const [reload, setReload] = useState(0);
  const [state, setState] = useState<{key: string; data: unknown; error: ApiError | null; loading: boolean; receivedAt: number | null}>({key: '', data: null, error: null, loading: false, receivedAt: null});
  const generation = useRef(0);
  const key = `${api.identity.owner}\0${api.identity.token}\0${path}`;
  useEffect(() => {
    const current = ++generation.current;
    if (!path) { setState({key, data: null, error: null, loading: false, receivedAt: null}); return; }
    const controller = new AbortController();
    // Replace, rather than merge, snapshots. Reset/identity changes never keep old rows.
    setState({key, data: null, error: null, loading: true, receivedAt: null});
    api.request(path, {signal: controller.signal}).then(({data}) => {
      if (generation.current === current && !controller.signal.aborted) setState({key, data, error: null, loading: false, receivedAt: Date.now()});
    }).catch(error => {
      if (generation.current !== current || controller.signal.aborted) return;
      setState({key, data: null, error: error instanceof ApiError ? error : new ApiError('读取失败。', 0), loading: false, receivedAt: null});
    });
    return () => { controller.abort(); };
  }, [api, path, reload, refreshToken, key]);
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
  return {input, setInput, id, query, submit};
}
