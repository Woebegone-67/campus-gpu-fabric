/** Read-only screenshots and overflow checks against the final local app. */
import {readFile,mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import assert from 'node:assert/strict';
const root=resolve(import.meta.dirname,'../..');
const out=resolve(root,process.env.VISUAL_EVIDENCE||'evidence/release-20261115/visual');
await mkdir(out,{recursive:true});
const identity=JSON.parse(await readFile(resolve(root,'runtime/release-preview/cpu/client-identities.json'),'utf8'));
const {chromium}=await import(pathToFileURL(resolve(root,'runtime/starwave-browser/node_modules/playwright-core/index.mjs')));
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const errors=[],checks=[];
try{
for(const width of [1440,768,390]){
 const page=await browser.newPage({viewport:{width,height:width===390?844:1000},reducedMotion:'reduce'});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:15253');
 await page.locator('input[name=account]').fill(identity.alice['X-Owner']);
 await page.locator('input[name=credential]').fill(identity.alice.Authorization.slice(7));
 await page.locator('.identity-manual button').click();
 await page.locator('[data-space-scroll=overview]').waitFor({state:'visible'});
 for(const section of ['overview','run','resources','events','decisions']){
  if(section==='overview')await page.getByRole('button',{name:'总览',exact:true}).click();
  else {await page.getByRole('button',{name:'工作台',exact:true}).click();await page.locator(`nav a[href="#${section}"]`).click();}
  await page.waitForTimeout(600);
  await page.waitForLoadState('networkidle');
  await page.waitForFunction(() => ![...document.querySelectorAll('[role=status],button')].some(el => el.getClientRects().length && /^(加载中|正在刷新|更新中)(…|\.\.\.)$/.test(el.textContent.trim())), null, {timeout: 25000});
  await page.waitForTimeout(200);
  const overflow=await page.evaluate(()=>({viewport:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth,areas:[...document.querySelectorAll('.space-scroll')].filter(el=>el.clientWidth).map(el=>({width:el.clientWidth,scroll:el.scrollWidth}))}));
  assert.ok(overflow.document<=width+1&&overflow.body<=width+1,JSON.stringify(overflow));
  assert.ok(overflow.areas.every(a=>a.scroll<=a.width+1),JSON.stringify(overflow));
  await page.screenshot({path:resolve(out,`${width}-${section}.png`)});
  checks.push({width,section,overflow});
 }
 await page.close();
}
assert.deepEqual(errors,[]);
await writeFile(resolve(out,'receipt.json'),JSON.stringify({status:'PASS',checks,errors,browser:browser.version(),scope:'Real API; read-only page navigation, 3 widths × 5 pages; screenshots require visual inspection.'},null,2));
console.log(JSON.stringify({checks:checks.length,status:'PASS'}));
}finally{await browser.close();}
