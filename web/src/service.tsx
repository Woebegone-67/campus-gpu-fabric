import {createContext,useContext} from 'react';
import {object,text} from './api';

export type ServiceInfo={mode:'logical_simulation'|'cpu_reference'|null;checkpoint:string;capabilities:Record<string,unknown>};
export const unknownService:ServiceInfo={mode:null,checkpoint:'',capabilities:{}};
export const ServiceContext=createContext<ServiceInfo>(unknownService);
export function serviceInfo(data:unknown):ServiceInfo {
  const value=object(data),mode=value.execution_mode;
  return {mode:mode==='logical_simulation'||mode==='cpu_reference'?mode:null,checkpoint:text(value.checkpoint,''),capabilities:object(value.capabilities)};
}
export function useService(){return useContext(ServiceContext);}
export function modeLabel(mode:unknown){return mode==='cpu_reference'?'真实 CPU 参考执行':mode==='logical_simulation'?'逻辑模拟':'执行模式尚未确认';}
