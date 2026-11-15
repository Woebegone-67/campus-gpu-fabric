import {useRef,useState} from 'react';
import {Field} from './common';

export function IdentityPicker({onLogin,busy,error}:{onLogin:(username:string,password:string)=>Promise<void>;busy:boolean;error?:string}) {
  const [username,setUsername]=useState(''),[password,setPassword]=useState('');
  const submitting=useRef(false);
  async function submit(){
    if(busy||submitting.current||!username.trim()||!password)return;
    submitting.current=true;
    try{await onLogin(username.trim(),password);}finally{submitting.current=false;setPassword('');}
  }
  return <section className="identity-panel">
    <form className="identity-manual" onSubmit={event=>{event.preventDefault();void submit();}}>
      <Field label="账户"><input name="username" autoComplete="username" value={username} onChange={event=>setUsername(event.target.value)} disabled={busy} required maxLength={128}/></Field>
      <Field label="密码"><input name="password" type="password" autoComplete="current-password" value={password} onChange={event=>setPassword(event.target.value)} disabled={busy} required maxLength={256}/></Field>
      <button className="button primary" disabled={busy}>{busy?'正在登录…':'登录'}</button>
    </form>
    {error&&<p role="alert" className="identity-error">{error}</p>}
  </section>;
}
