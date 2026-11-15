import {pathToFileURL} from 'node:url';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
const {chromium}=await import(pathToFileURL(process.env.PLAYWRIGHT_MODULE??path.resolve('runtime/starwave-browser/node_modules/playwright-core/index.mjs')).href);
const runtime=path.resolve('runtime/recall-live-20261115'),base='http://127.0.0.1:15275';
const out=path.resolve('evidence/reclaim-live-20261115');fs.mkdirSync(out,{recursive:true});
const passwords=Object.fromEntries([...fs.readFileSync(runtime+'/cpu/login-accounts.txt','utf8').matchAll(/^(\w+)（[^\n]+）\n密码：([^\n]+)/gm)].map(m=>[m[1],m[2]]));
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const errors=[];
const pause=ms=>new Promise(r=>setTimeout(r,ms));
async function wait(fn,seconds=25){const until=Date.now()+seconds*1000;while(Date.now()<until){const value=await fn();if(value)return value;await pause(200);}throw Error('Observation timed out');}
async function login(name){const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));await page.goto(base);await page.locator('input[name=username]').fill(name);await page.locator('input[name=password]').fill(passwords[name]);await page.locator('.identity-manual button').click();await page.waitForFunction(n=>document.querySelector('.account-trigger')?.textContent?.includes(n),name);const session=await(await context.request.get(base+'/x/v1/session')).json();return {context,page,actor:session.actor_id};}
async function json(user,url,body){const response=await user.context.request[body===undefined?'get':'post'](base+url,{headers:{'X-Fabric-Request':'browser','X-Fabric-Actor':user.actor,Origin:base},...(body===undefined?{}:{data:body})});const value=await response.json();assert(response.ok(),url+' '+response.status()+' '+JSON.stringify(value));return value;}
try{
 const receipt=JSON.parse(fs.readFileSync(out+'/browser-receipt.json','utf8'));
 const alice=await login('alice'),chen=await login('chen'),admin=await login('admin');
 const intent=receipt.rows.find(r=>r.case==='normal').intent;
 const audit=await json(alice,'/x/v1/work-intents/'+intent+'/reclaim-decisions');
 assert(audit.items.length);assert(audit.items[0].checkpoint_verified_at);assert(audit.items[0].stop_confirmed_at);
 const hidden=await chen.context.request.get(base+'/x/v1/work-intents/'+intent+'/reclaim-decisions',{headers:{'X-Fabric-Actor':chen.actor}});assert.equal(hidden.status(),404);
 fs.writeFileSync(out+'/decision-audit.json',JSON.stringify(audit,null,2));
 await alice.page.goto(base+'/#continuity?intent='+intent);
 const resume=alice.page.getByRole('button',{name:'恢复这项工作',exact:true});await resume.waitFor({timeout:12000});
 if(await resume.isVisible()&&await resume.isEnabled()){
  await resume.click();const complete=await wait(async()=>{const d=await json(alice,'/x/v1/work-intents/'+intent);return d.intent.state==='completed'&&d;},70);
  assert(complete.intent.attempts.length>=2);assert(complete.allocations.every(a=>!a.placement));
  await pause(1500);await alice.page.screenshot({path:out+'/resumed-completed.png'});
  fs.writeFileSync(out+'/resumed-completed.json',JSON.stringify(complete,null,2));
  receipt.checks.push('Same work resumed actual checkpoint and completed; allocation released');
 }else throw Error('Normal verification work no longer resumable');
 await admin.page.goto(base+'/#acceptance');await admin.page.getByText('收回策略对照',{exact:true}).first().click();
 await admin.page.getByRole('button',{name:'下载对照报告'}).waitFor();assert(await admin.page.getByText('尽快归还',{exact:true}).count());
 const report=await json(admin,'/x/v1/reclaim-validation');assert.equal(report.planner_matches_current,true);assert(report.summary.length===10);
 const downloadPromise=admin.page.waitForEvent('download');await admin.page.getByRole('button',{name:'下载对照报告'}).click();const download=await downloadPromise;const file=await download.path();assert.equal(fs.readFileSync(file,'utf8'),fs.readFileSync('evidence/reclaim-aware-20261115/report.json','utf8'));
 await admin.page.screenshot({path:out+'/comparison-report.png'});
 assert.deepEqual(errors,[]);receipt.checks.push('Decision audit survives stop; another member cannot read it','Admin report current digest and browser download original bytes verified');fs.writeFileSync(out+'/browser-receipt.json',JSON.stringify(receipt,null,2));console.log(JSON.stringify({checks:receipt.checks},null,2));
}finally{await browser.close();}
