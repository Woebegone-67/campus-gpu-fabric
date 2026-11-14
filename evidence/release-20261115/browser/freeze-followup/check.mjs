import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';
const root=process.cwd(), checkout=resolve('runtime/final-clean-checkout');
const out=resolve('evidence/release-20261115/browser/freeze-followup');
await mkdir(out,{recursive:true});
const identity=JSON.parse(await readFile(resolve(checkout,'runtime/final-clean/cpu/client-identities.json'),'utf8'));
const prior=JSON.parse(await readFile('evidence/release-20261115/browser/clean-live/receipt.json','utf8'));
const receipt={status:'RUNNING',commit:execFileSync('git',['rev-parse','HEAD'],{cwd:checkout,encoding:'utf8'}).trim(),sourceClean:execFileSync('git',['status','--porcelain'],{cwd:checkout,encoding:'utf8'}).trim()==='',checks:[],errors:[],scope:'Same clean checkout after Events-only correction, owned service restart, read-only final browser checks.'};
assert.equal(receipt.sourceClean,true);
const detailResponse=await fetch(`http://127.0.0.1:15263/x/v1/work-intents/${prior.recovery_work}`,{headers:identity.alice});
assert.equal(detailResponse.status,200);const detail=await detailResponse.json();
assert.equal(detail.item.state,'completed');assert.equal(detail.item.display_name,'联合验收 · 中断后继续');
assert.ok(detail.allocations.every(a=>a.state==='Released'));
receipt.checks.push({name:'completed named work and released resource persist after owned service restart',status:'PASS',intent:prior.recovery_work});
const {chromium}=await import(pathToFileURL(resolve('runtime/starwave-browser/node_modules/playwright-core/index.mjs')));
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
try {
 for(const width of [1440,768,390]){
  const page=await browser.newPage({viewport:{width,height:1000},reducedMotion:'reduce'});
  page.on('pageerror',e=>receipt.errors.push(e.message));
  await page.goto('http://127.0.0.1:15263');
  await page.locator('input[name=account]').fill(identity.alice['X-Owner']);
  await page.locator('input[name=credential]').fill(identity.alice.Authorization.slice(7));
  await page.locator('.identity-manual button').click();
  await page.locator('[data-space-scroll=overview]').waitFor({state:'visible'});
  await page.getByRole('button',{name:'工作台',exact:true}).click();
  await page.locator('nav a[href="#events"]').click();
  await page.locator('.timeline').waitFor({state:'visible'});
  await page.waitForLoadState('networkidle');
  const content=await page.locator('.timeline').innerText();
  assert.ok(content.includes('工作 · work-'));assert.ok(!content.includes('未命名工作'));
  const overflow=await page.evaluate(()=>({viewport:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth}));
  assert.ok(overflow.document<=width+1&&overflow.body<=width+1);
  await page.screenshot({path:resolve(out,`events-${width}.png`)});
  receipt.checks.push({name:'neutral work label, visible timeline and no horizontal overflow',width,status:'PASS'});
  await page.close();
 }
 assert.deepEqual(receipt.errors,[]);receipt.status='PASS';
} catch(error) {receipt.status='FAIL';receipt.failure=String(error);throw error;}
finally {await browser.close();await writeFile(resolve(out,'receipt.json'),JSON.stringify(receipt,null,2));}
console.log(JSON.stringify({status:receipt.status,checks:receipt.checks.length,commit:receipt.commit}));
