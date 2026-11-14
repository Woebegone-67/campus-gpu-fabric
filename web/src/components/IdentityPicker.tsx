import {useState,type ChangeEvent} from 'react';
import {object,type Identity} from '../api';
import {Field} from './common';

export function IdentityPicker({identity, onApply, busy}:{identity:Identity;onApply:(identity:Identity)=>void;busy:boolean}) {
  const [owner,setOwner]=useState(identity.owner),[token,setToken]=useState(identity.token);
  const [profiles,setProfiles]=useState<Record<string,Identity>>({});
  const [error,setError]=useState('');
  async function importFile(event:ChangeEvent<HTMLInputElement>){
    const file=event.target.files?.[0];if(!file)return;
    setError('');
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
      if(!Object.keys(parsed).length)throw new Error('未找到有效的本地身份记录。');
      setProfiles(parsed);
    } catch(error){setError(error instanceof Error?error.message:'无法读取身份文件。');}
    finally{event.target.value='';}
  }
  function choose(name:string){const profile=profiles[name];if(profile){setOwner(profile.owner);setToken(profile.token);onApply(profile);}}
  return <section className="identity-panel"><div className="inline-form"><Field label="导入本地身份文件"><input type="file" accept="application/json,.json" disabled={busy} onChange={importFile}/></Field>{Object.keys(profiles).length>0&&<Field label="选择本地身份"><select value={identity.profile||''} disabled={busy} onChange={e=>choose(e.target.value)}><option value="" disabled>选择身份</option>{Object.keys(profiles).map(name=><option key={name} value={name}>{name}</option>)}</select></Field>}<button className="button quiet" disabled={busy} onClick={()=>{setProfiles({});setToken('');setOwner('');onApply({owner:'',token:'',profile:''});}}>清除身份</button></div>
    <form className="inline-form identity-manual" onSubmit={event=>{event.preventDefault();onApply({owner:owner.trim(),token:token.trim()});}}><Field label="Owner（管理员可留空）"><input value={owner} onChange={e=>setOwner(e.target.value)} disabled={busy}/></Field><Field label="Bearer token（启用认证时）"><input type="password" autoComplete="off" value={token} onChange={e=>setToken(e.target.value)} disabled={busy} placeholder="仅保留在本页内存"/></Field><button className="button" disabled={busy}>应用身份</button></form>
    {error&&<p role="alert" className="identity-error">{error}</p>}<p className="helper">文件仅在浏览器内存解析，不上传、不写入浏览器存储。身份名称不授予权限；每个请求由后端核验。刷新页面后需重新导入。</p>
  </section>;
}
