import {useCallback,useEffect,useMemo,useRef,useState} from 'react';
import {ApiClient,ApiError} from './api';
import {readAccountSession,type AccountSession} from './session';

export function useBrowserSession(){
  const api=useMemo(()=>new ApiClient({owner:'',token:''}),[]);
  const [state,setState]=useState<{account:AccountSession|null;loading:boolean;error:string}>({account:null,loading:true,error:''});
  const generation=useRef(0),pending=useRef<AbortController|null>(null),changing=useRef(false);
  const refresh=useCallback(async()=>{
    if(changing.current)return;
    const current=++generation.current;
    pending.current?.abort();const controller=new AbortController();pending.current=controller;
    setState(previous=>({...previous,loading:true,error:''}));
    try{
      const result=await api.request('/x/v1/session',{signal:controller.signal});
      if(current!==generation.current||controller.signal.aborted)return;
      const account=readAccountSession(result.data);
      if(!account)throw new Error('账户信息暂不可用，请重试。');
      setState({account,loading:false,error:''});
    }catch(error){
      if(current!==generation.current||controller.signal.aborted)return;
      const rejected=error instanceof ApiError&&(error.status===401||error.status===403);
      setState(previous=>({account:rejected?null:previous.account,loading:false,error:rejected?'':'账户暂时无法验证，请重试。'}));
    }
  },[api]);
  const invalidate=useCallback(()=>{setState({account:null,loading:true,error:''});void refresh();},[refresh]);
  useEffect(()=>{
    void refresh();
    const focus=()=>{if(document.visibilityState!=='hidden')void refresh();};
    window.addEventListener('focus',focus);document.addEventListener('visibilitychange',focus);
    return()=>{++generation.current;pending.current?.abort();window.removeEventListener('focus',focus);document.removeEventListener('visibilitychange',focus);};
  },[refresh]);
  const login=useCallback(async(username:string,password:string)=>{
    if(changing.current)return;
    changing.current=true;const current=++generation.current;pending.current?.abort();
    setState({account:null,loading:true,error:''});
    try{
      const result=await api.request('/x/v1/auth/login',{method:'POST',body:{username,password}});
      if(current!==generation.current)return;
      const account=readAccountSession(result.data);
      if(!account)throw new Error('账户信息暂不可用。');
      setState({account,loading:false,error:''});
    }catch(error){
      if(current!==generation.current)return;
      const message=error instanceof ApiError&&error.status===401?'账户或密码不正确。':error instanceof ApiError&&error.status===429?'尝试次数过多，请稍后再试。':'登录未完成，请重试。';
      setState({account:null,loading:false,error:message});
    }finally{changing.current=false;}
  },[api]);
  const logout=useCallback(async()=>{
    if(changing.current)return;
    changing.current=true;const current=++generation.current;pending.current?.abort();
    setState(previous=>({...previous,loading:true,error:''}));
    try{
      await api.request('/x/v1/auth/logout',{method:'POST'});
      if(current!==generation.current)return;
      setState({account:null,loading:false,error:''});
    }catch{if(current===generation.current)setState(previous=>({...previous,loading:false,error:'退出登录未完成，请重试。'}));}
    finally{changing.current=false;}
  },[api]);
  return {...state,refresh,login,logout,invalidate};
}
