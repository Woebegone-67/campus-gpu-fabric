import {useEffect,useState,type ChangeEvent} from 'react';
import {object,type Identity} from '../api';
import {Field} from './common';

export function IdentityPicker({identity,onApply,busy}:{identity:Identity;onApply:(identity:Identity)=>void;busy:boolean}) {
  const [owner,setOwner]=useState(identity.owner),[token,setToken]=useState(identity.token);
  const [profiles,setProfiles]=useState<Record<string,Identity>>({});
  const [error,setError]=useState(''),[notice,setNotice]=useState('');
  useEffect(()=>{
    setOwner(identity.owner);setToken(identity.token);setNotice('');
    if(!identity.owner&&!identity.token)setProfiles({});
  },[identity.owner,identity.token]);
  async function importFile(event:ChangeEvent<HTMLInputElement>){
    const input=event.currentTarget,file=input.files?.[0];if(!file)return;
    setError('');setNotice('');
    try {
      if(file.size>65536)throw new Error('身份文件超过 64 KiB。');
      const root=object(JSON.parse(await file.text()));
      const parsed:Record<string,Identity>={};
      for(const [name,value] of Object.entries(root)) {
        const record=object(value),authorization=record.Authorization;
        if(typeof authorization!=='string'||!authorization.startsWith('Bearer '))continue;
        const owner=record['X-Owner'];
        parsed[name]={owner:typeof owner==='string'?owner:'',token:authorization.slice(7),profile:name};
      }
      if(!Object.keys(parsed).length)throw new Error('文件中没有可用的账户。');
      setProfiles(parsed);setNotice(`已读取 ${Object.keys(parsed).length} 个账户，请选择。`);
    } catch(error){setError(error instanceof Error?error.message:'无法读取身份文件。');}
    finally{input.value='';}
  }
  function choose(name:string){const profile=profiles[name];if(profile){setOwner(profile.owner);setToken(profile.token);setError('');setNotice('');onApply(profile);}}
  function applyManual() {
    const next={owner:owner.trim(),token:token.trim()};
    if(!next.owner&&!next.token){setError('请输入账户或访问凭证。');return;}
    const match=Object.values(profiles).find(profile=>profile.owner===next.owner&&profile.token===next.token);
    const profile=(identity.owner===next.owner&&identity.token===next.token?identity.profile:undefined)??match?.profile;
    setError('');setNotice('');onApply(profile?{...next,profile}:next);
  }
  return <section className="identity-panel">
    <form className="identity-manual" onSubmit={event=>{event.preventDefault();applyManual();}}>
      <Field label="账户"><input name="account" autoComplete="username" value={owner} onChange={event=>setOwner(event.target.value)} disabled={busy} placeholder="管理员可留空"/></Field>
      <Field label="访问凭证"><input name="credential" type="password" autoComplete="off" value={token} onChange={event=>setToken(event.target.value)} disabled={busy}/></Field>
      <button className="button primary" disabled={busy}>连接账户</button>
    </form>
    <details className="identity-import"><summary>从身份文件连接</summary>
      <Field label="导入本地身份文件"><input type="file" accept="application/json,.json" disabled={busy} onChange={importFile}/></Field>
      {Object.keys(profiles).length>0&&<Field label="选择本地身份"><select value={identity.profile||''} disabled={busy} onChange={event=>choose(event.target.value)}><option value="" disabled>选择账户</option>{Object.keys(profiles).map(name=><option key={name} value={name}>{name}</option>)}</select></Field>}
      {notice&&<p className="helper" role="status">{notice}</p>}
    </details>
    {error&&<p role="alert" className="identity-error">{error}</p>}
    <p className="identity-session-note">刷新页面后需重新连接。</p>
  </section>;
}
