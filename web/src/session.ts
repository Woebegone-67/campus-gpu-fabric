import {createContext, useContext} from 'react';
import {object} from './api';
import type {AccountSession} from './generated/account-session';
export type {AccountSession} from './generated/account-session';

/** Workspace capabilities come from the authenticated server session. */
export function readAccountSession(data:unknown):AccountSession|null {
  const value=object(data);
  if(typeof value.actor_id!=='string'||!value.actor_id||!(value.owner_id===null||typeof value.owner_id==='string')||typeof value.is_administrator!=='boolean'||!Array.isArray(value.capabilities)||value.capabilities.some(x=>x!=='use_compute'&&x!=='contribute_resources'))return null;
  return {actor_id:value.actor_id,owner_id:value.owner_id,capabilities:value.capabilities as AccountSession['capabilities'],is_administrator:value.is_administrator,...(typeof value.username==='string'?{username:value.username}:{})};
}
export const AccountContext=createContext<AccountSession|null>(null);
export const useAccount=()=>useContext(AccountContext);
