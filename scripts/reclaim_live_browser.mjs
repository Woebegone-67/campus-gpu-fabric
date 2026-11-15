import {pathToFileURL} from 'node:url';
import fs from 'node:fs';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';
const {chromium}=await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE??path.resolve('runtime/starwave-browser/node_modules/playwright-core/index.mjs')).href);
const runtime=path.resolve('runtime/recall-live-20261115'),base='http://127.0.0.1:15275';
const out=path.resolve('evidence/reclaim-live-20261115');fs.mkdirSync(out,{recursive:true});
const passwords=Object.fromEntries([...fs.readFileSync(runtime+'/cpu/login-accounts.txt','utf8').matchAll(/^(\w+)（[^\n]+）\n密码：([^\n]+)/gm)].map(m=>[m[1],m[2]]));
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const errors=[],checks=[],rows=[];let frozenPid=null;
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function wait(fn,seconds=25){const until=Date.now()+seconds*1000;while(Date.now()<until){const value=await fn();if(value)return value;await pause(200);}throw Error('Observation timed out');}
async function login(name){const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));await page.goto(base);await page.locator('input[name=username]').fill(name);await page.locator('input[name=password]').fill(passwords[name]);await page.locator('.identity-manual button').click();await page.waitForFunction(n=>document.querySelector('.account-trigger')?.textContent?.includes(n),name);const session=await(await context.request.get(base+'/x/v1/session')).json();return {context,page,actor:session.actor_id};}
async function json(user,url,body){const response=await user.context.request[body===undefined?'get':'post'](base+url,{headers:{'X-Fabric-Request':'browser','X-Fabric-Actor':user.actor,Origin:base},...(body===undefined?{}:{data:body})});const value=await response.json();assert(response.ok(),url+' '+response.status()+' '+JSON.stringify(value));return value;}
try{
 const alice=await login('alice'),chen=await login('chen');
 const contributions=await json(chen,'/x/v1/contributions');const devices=contributions.items.flatMap(n=>n.devices).filter(d=>d.model==='A10').map(d=>d.physical_gpu_id);assert(devices.length);
 for(const frozen of [false,true]){
  const intent='reclaim-live-'+(frozen?'frozen':'normal')+'-'+Date.now();
  await json(alice,'/x/v1/work-intents',{intent_id:intent,owner_id:'alice',display_name:frozen?'停止超时验证':'收回策略验证',execution_mode:'cpu_reference',settlement_action:'release_on_terminal',specification:{allocation:{kind:'allocation.create',owner_id:'alice',ttl_seconds:300,preemption_mode:'checkpoint',checkpoint_timeout_seconds:8,max_restarts:2,request:{cpu_millicores:1000,memory_mib:4096,gpu:{count:1,model:'A10'}}},job:{image:'python:3.12-slim',command:['python','-V'],max_retries:0}}});
  const detail=await wait(async()=>{const d=await json(alice,'/x/v1/work-intents/'+intent);return d.jobs?.some(j=>j.state==='Running')&&d;});
  await alice.page.goto(base+'/#continuity?intent='+intent);await alice.page.getByRole('heading',{name:frozen?'停止超时验证':'收回策略验证',exact:true}).waitFor();await pause(1400);
  if(frozen){const journal=JSON.parse(fs.readFileSync(runtime+'/cpu/data/cpu-executor/journal.json','utf8'));const record=Object.values(journal.attempts).find(x=>x.attempt.intent_id===intent);assert(record?.pid>1);const command=execFileSync('/bin/ps',['-p',String(record.pid),'-o','command='],{encoding:'utf8'});assert(command.includes(runtime+'/cpu/')&&command.includes('cpu_worker.py'));process.kill(record.pid,'SIGSTOP');frozenPid=record.pid;}
  await chen.page.goto(base+'/#resources');const disclosure=chen.page.locator('details').filter({has:chen.page.locator('summary').getByText('收回设备',{exact:true})}).first();if(await disclosure.getAttribute('open')===null)await disclosure.locator('summary').click();
  for(const id of [...new Set(devices)])await chen.page.locator('.device-choices label').filter({hasText:id}).locator('input').check();
  await chen.page.locator('input[name=notice]').fill('12');await chen.page.locator('input[name=duration]').fill('3');await chen.page.locator('select[name=return_policy]').selectOption('preserve_work');chen.page.once('dialog',d=>d.accept());await chen.page.getByRole('button',{name:'收回设备',exact:true}).click();
  const notice=await wait(async()=>{const d=await json(alice,'/x/v1/recalls');return d.borrowed.find(x=>x.intent_id===intent)&&d;});
  const row=notice.borrowed.find(x=>x.intent_id===intent);assert(row.decision);assert(row.decision.stop_at<=row.checkpoint_due);assert(row.decision.stop_at<=row.deadline);assert(row.checkpoint_due<row.deadline);
  await alice.page.locator('section[aria-label="设备收回进度"]').waitFor();const before=await alice.page.locator('section[aria-label="设备收回进度"]').innerText();await pause(1400);const after=await alice.page.locator('section[aria-label="设备收回进度"]').innerText();assert.notEqual(before,after,'Countdown must update without manual refresh');await alice.page.screenshot({path:out+'/'+(frozen?'frozen':'normal')+'-countdown.png'});
  const returned=await wait(async()=>{const d=await json(chen,'/x/v1/recalls');return d.owned.find(x=>x.notice_id&&x.held_count===0&&x.issued_at>=notice.now-2)&&d;});
  const owned=returned.owned[0];assert(owned.held_count===0);assert(returned.now<=owned.deadline+.5,'Must stop before owner deadline in this bounded run');
  const terminal=await json(alice,'/x/v1/work-intents/'+intent);assert(terminal.allocations.every(a=>!a.placement));
  await pause(2200);await chen.page.screenshot({path:out+'/'+(frozen?'frozen':'normal')+'-returned.png'});
  if(frozen){assert(['failed','cancelled'].includes(terminal.intent.state));frozenPid=null;checks.push('Frozen CPU worker forcibly stopped without borrower action; no fake checkpoint success');}
  else {assert(terminal.allocations.some(a=>a.checkpoint?.verification_status==='verified'));checks.push('Adaptive delayed checkpoint saved actual CPU progress and returned resource');}
  checks.push('Live countdown and owner status automatically updated; save cutoff differs from recall deadline');rows.push({case:frozen?'frozen':'normal',intent,initial:row,returned:owned,confirmed_now:returned.now,work_state:terminal.intent.state,before,after});
  await wait(async()=>{const d=await json(chen,'/x/v1/recalls');return d.now>=owned.unavailable_until;},20);
 }
 assert.deepEqual(errors,[]);checks.push('No application errors in two independent account contexts');
 fs.writeFileSync(out+'/browser-receipt.json',JSON.stringify({observed_at:new Date().toISOString(),mode:'paced real CPU integration; not performance evidence',checks,rows,errors},null,2));console.log(JSON.stringify({checks,decisions:rows.map(r=>({case:r.case,decision:r.initial.decision,state:r.work_state,confirmed_before_deadline:r.confirmed_now<=r.returned.deadline}))},null,2));
}catch(e){fs.writeFileSync(out+'/failure.txt',String(e.stack));throw e;}finally{if(frozenPid){try{const c=execFileSync('/bin/ps',['-p',String(frozenPid),'-o','command='],{encoding:'utf8'});if(c.includes(runtime))process.kill(frozenPid,'SIGCONT');}catch{}}await browser.close();}
