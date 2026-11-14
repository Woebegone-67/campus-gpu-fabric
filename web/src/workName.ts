import {idOf, object} from './api';

export function shortWorkId(id: string): string {
  return id.length > 22 ? `${id.slice(0, 13)}…${id.slice(-6)}` : id;
}

/** Names are presentation metadata; identifiers remain the navigation and copy keys. */
export function workName(value: unknown, fallbackId = ''): string {
  const work = object(value);
  const name = typeof work.display_name === 'string' ? work.display_name.trim() : '';
  if (name) return name;
  const id = idOf(work) || fallbackId;
  return id ? `未命名工作 · ${shortWorkId(id)}` : '未命名工作';
}
