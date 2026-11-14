import {useCallback, useEffect, useRef, useState} from 'react';
import {ApiClient, ApiError} from './api';

export type QueryState = {data: unknown; error: ApiError | null; loading: boolean; refresh: () => void};
export function useQuery(api: ApiClient, path: string | null, refreshToken = 0): QueryState {
  const [reload, setReload] = useState(0);
  const [state, setState] = useState<{key: string; data: unknown; error: ApiError | null; loading: boolean}>({key: '', data: null, error: null, loading: false});
  const generation = useRef(0);
  const key = `${api.identity.owner}\0${api.identity.token}\0${path}`;
  useEffect(() => {
    const current = ++generation.current;
    if (!path) { setState({key, data: null, error: null, loading: false}); return; }
    const controller = new AbortController();
    // Replace, rather than merge, snapshots. Reset/identity changes never keep old rows.
    setState({key, data: null, error: null, loading: true});
    api.request(path, {signal: controller.signal}).then(({data}) => {
      if (generation.current === current && !controller.signal.aborted) setState({key, data, error: null, loading: false});
    }).catch(error => {
      if (generation.current !== current || controller.signal.aborted) return;
      setState({key, data: null, error: error instanceof ApiError ? error : new ApiError('读取失败。', 0), loading: false});
    });
    return () => { controller.abort(); };
  }, [api, path, reload, refreshToken, key]);
  const refresh = useCallback(() => setReload(value => value + 1), []);
  return {...(state.key === key ? state : {data: null, error: null, loading: Boolean(path)}), refresh};
}
