/** Real Chromium interaction acceptance, with every API response intercepted.
 * No fixture request can mutate the running Fabric service.
 */
import assert from 'node:assert/strict';
import {mkdir, rename, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {installDualSpaceFixtures, ids} from './dual-space.fixtures.mjs';

const root=resolve(import.meta.dirname,'../..');
const baseUrl=process.env.INTERACTION_MOTION_URL||'http://127.0.0.1:15191';
const origin=new URL(baseUrl).origin;
assert.ok(['localhost','127.0.0.1'].includes(new URL(origin).hostname),'Local isolated target required');
const out=resolve(root,process.env.INTERACTION_MOTION_EVIDENCE||`evidence/cp4/interaction-motion/browser-${new Date().toISOString().replaceAll(':','-')}`);
await mkdir(out,{recursive:true});
const {chromium}=await import(pathToFileURL(resolve(root,'runtime/starwave-browser/node_modules/playwright-core/index.mjs')));
const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const result={started_at:new Date().toISOString(),browser:browser.version(),url:origin,
  status:'RUNNING',
  scope:'Real Chromium render/input behavior against synthetic intercepted API responses; no live backend writes.',
  checks:[],screenshots:[],requests:[],denied:[],errors:[],
  unverified:['Physical trackpad and touch hardware','Safari/WebKit','Actual operating-system clipboard permission UI','Real backend business transitions and authorization']};

async function persist(){
  result.updated_at=new Date().toISOString();
  const path=resolve(out,'interaction-motion-browser.json');
  await writeFile(`${path}.tmp`,JSON.stringify(result,null,2)+'\n');
  await rename(`${path}.tmp`,path);
}
await persist();

async function settle(page,ms=400){await page.waitForTimeout(ms);}
async function applyIdentity(page){
  if(!await page.getByLabel('Owner（管理员可留空）',{exact:true}).isVisible())await page.locator('.account-trigger').click();
  await page.getByLabel('Owner（管理员可留空）',{exact:true}).fill('alice');
  await page.getByLabel('Bearer token（启用认证时）',{exact:true}).fill('fixture-alice');
  await page.getByRole('button',{name:'应用身份',exact:true}).click();
  await page.locator('[data-space-scroll]:visible').waitFor();
  if(await page.getByLabel('Owner（管理员可留空）',{exact:true}).isVisible())await page.getByRole('button',{name:'关闭账户面板',exact:true}).click();
  await settle(page);
}
async function goto(page,hash='#overview'){
  await page.goto(`${origin}/${hash}`,{waitUntil:'networkidle'});
  await page.locator('[data-space]').waitFor();
  if(await page.locator('.account-gate').isVisible())await applyIdentity(page);
  await settle(page);
}
async function section(page,id){
  await page.locator(`a[href="#${id}"]:visible`).first().click();
  await page.waitForURL(url=>url.hash.split('?')[0]===`#${id}`);
  await settle(page);
}
async function openDetails(page,label){
  const summary=page.locator('summary:visible').filter({hasText:label}).first();
  if(!await summary.evaluate(el=>el.parentElement.open))await summary.click();
  await settle(page);
  return summary;
}
async function screenshot(page,name){
  const path=resolve(out,`${name}.png`);await page.screenshot({path});
  result.screenshots.push({name,path,viewport:page.viewportSize(),data:'synthetic browser fixtures'});
}
async function transformScale(locator){
  return locator.evaluate(el=>{const style=getComputedStyle(el),matrix=style.transform==='none'?null:new DOMMatrix(style.transform);return {scale:Number.parseFloat(style.scale)||matrix?.a||1,transform:style.transform,shadow:style.boxShadow,pressed:el.getAttribute('data-motion-pressed')};});
}
async function settledPress(locator){
  const timing=await locator.evaluate(async el=>{
    const started=performance.now();
    // Flush the input style, then follow the browser's animation clock. A wall
    // timeout alone can sample an old frame when rendering has been delayed.
    getComputedStyle(el).boxShadow;
    const animations=el.getAnimations().filter(animation=>['scale','box-shadow'].includes(animation.transitionProperty));
    const transitions=animations.map(animation=>({property:animation.transitionProperty,...animation.effect.getTiming()}));
    let timeout;
    try{
      await Promise.race([
        Promise.all(animations.map(animation=>animation.finished)).then(()=>new Promise(requestAnimationFrame)),
        new Promise((_,reject)=>{timeout=setTimeout(()=>reject(new Error('Press transitions did not settle within 2 seconds')),2000);}),
      ]);
    }finally{clearTimeout(timeout);}
    return {animation_wait_ms:performance.now()-started,transitions};
  });
  for(const transition of timing.transitions){
    const maximum=transition.property==='scale'?110:160;
    assert.ok(transition.duration<=maximum&&transition.delay===0,`${transition.property} retains its ${maximum} ms motion budget without delay`);
  }
  return {...await transformScale(locator),...timing};
}
async function check(name,run,options={}){
  if(process.env.INTERACTION_MOTION_FILTER&&!name.includes(process.env.INTERACTION_MOTION_FILTER))return;
  const context=await browser.newContext({viewport:{width:1440,height:900},deviceScaleFactor:1,...options});
  const fixture=await installDualSpaceFixtures(context,origin);
  const page=await context.newPage();page.setDefaultTimeout(10000);
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  try{
    const details=await run({context,fixture,page});
    assert.equal(fixture.requests.filter(request=>request.method!=='GET').length,0,'No business writes may occur');
    assert.deepEqual(fixture.denied,[],'No external requests');assert.deepEqual(errors,[],'No browser runtime errors');
    result.checks.push({name,status:'PASS',details});
  }catch(error){result.checks.push({name,status:'FAIL',error:error.stack});await screenshot(page,`failure-${name}`).catch(()=>{});}
  finally{
    fixture.releaseAll();result.requests.push(...fixture.requests);result.denied.push(...fixture.denied);result.errors.push(...errors);
    // Retain each observation even if browser cleanup stalls or the runner exits.
    await persist();
    await context.close();
  }
}

await check('button-press-release-and-keyboard',async({page})=>{
  await goto(page);
  const button=page.getByRole('button',{name:'显示设置',exact:true});
  const before=await transformScale(button),box=await button.boundingBox();
  await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();
  const pressed=await settledPress(button);
  assert.ok(pressed.scale>=.945&&pressed.scale<=.975,`Pressed scale should reach .96, got ${pressed.scale}`);
  const inset=pressed.shadow.match(/rgba\(0, 0, 0, [\d.]+\) [\d.-]+px ([\d.-]+)px ([\d.-]+)px [\d.-]+px inset/);
  assert.ok(inset&&Number(inset[1])>2.5&&Number(inset[2])>7.5,`Pointer press should retain deep inset depth even while hovered, got ${pressed.shadow}`);
  await screenshot(page,'1440-button-pressed');
  const samplesPromise=button.evaluate(async el=>{const samples=[];const start=performance.now();while(performance.now()-start<650){const style=getComputedStyle(el),matrix=style.transform==='none'?null:new DOMMatrix(style.transform);samples.push({at:performance.now()-start,scale:Number.parseFloat(style.scale)||matrix?.a||1});await new Promise(requestAnimationFrame);}return samples;});
  await page.mouse.up();const samples=await samplesPromise;
  assert.ok(samples.some(sample=>sample.scale>1.001),'Release should produce a slight overshoot');
  const after=await transformScale(button);assert.ok(Math.abs(after.scale-1)<.003,'Release returns to rest');
  if(await page.getByRole('button',{name:'关闭显示设置',exact:true}).isVisible())await page.getByRole('button',{name:'关闭显示设置',exact:true}).click();
  await button.focus();await page.keyboard.down('Space');
  const keyboardPressed=await settledPress(button);assert.ok(keyboardPressed.scale>=.945&&keyboardPressed.scale<=.975,'Keyboard Space has matching press feedback');
  await page.keyboard.up('Space');await settle(page,650);assert.equal(await button.getAttribute('aria-expanded'),'true','Native keyboard activation is preserved');
  await screenshot(page,'1440-button-and-popover');
  await page.getByRole('button',{name:'关闭显示设置',exact:true}).click();
  await section(page,'run');
  const search=page.getByRole('textbox',{name:'筛选当前页工作',exact:true});
  await search.focus();
  const refresh=page.locator('.panel').filter({has:search}).getByRole('button',{name:'刷新',exact:true});
  const refreshBox=await refresh.boundingBox();
  await page.mouse.move(refreshBox.x+refreshBox.width/2,refreshBox.y+refreshBox.height/2);await page.mouse.down();
  const fromInput=await settledPress(refresh);
  assert.equal(fromInput.pressed,'true','Blurring the previous input must not clear the newly pressed button');
  assert.ok(fromInput.scale>=.945&&fromInput.scale<=.975,'Pointer press after input focus keeps compression');
  await page.mouse.up();await settle(page,450);
  return {before,pressed,after,keyboardPressed,fromInput,release_peak:Math.max(...samples.map(sample=>sample.scale)),scope:'Verifies configured durations and rendered endpoints; not a frame-rate or input-latency performance certification'};
});

await check('navigation-slides-and-preserves-draft',async({page})=>{
  await goto(page);
  const nav=page.getByRole('navigation',{name:'空间',exact:true});
  const selection=nav.locator('.nav-selection');await selection.waitFor({state:'visible'});
  const before=await selection.boundingBox();
  const samplesPromise=selection.evaluate(async el=>{const samples=[];const start=performance.now();while(performance.now()-start<700){const b=el.getBoundingClientRect();samples.push({at:performance.now()-start,x:b.x,y:b.y});await new Promise(requestAnimationFrame);}return samples;});
  await nav.getByRole('link',{name:'工作台',exact:true}).click();
  const samples=await samplesPromise,after=await selection.boundingBox();
  assert.ok(Math.abs(after.y-before.y)>10,'Selection moves between navigation rows');
  assert.ok(samples.some(s=>s.y>Math.min(before.y,after.y)+2&&s.y<Math.max(before.y,after.y)-2),'Navigation includes intermediate positions');
  await openDetails(page,'新建工作');await page.locator('input[name=cpu]').fill('1234');
  await page.getByRole('textbox',{name:'筛选当前页工作',exact:true}).fill('校园');
  const switchIndicator=page.locator('.space-switch-selection');
  const switchBefore=await switchIndicator.boundingBox();
  const switchSamplesPromise=switchIndicator.evaluate(async el=>{const samples=[];const start=performance.now();while(performance.now()-start<650){const b=el.getBoundingClientRect();samples.push({at:performance.now()-start,x:b.x,y:b.y});await new Promise(requestAnimationFrame);}return samples;});
  await page.getByRole('button',{name:'总览',exact:true}).click();
  const switchSamples=await switchSamplesPromise,switchAfter=await switchIndicator.boundingBox();
  assert.ok(Math.abs(switchBefore.x-switchAfter.x)>10,'Segment selection moves horizontally');
  assert.ok(switchSamples.some(s=>s.x>Math.min(switchBefore.x,switchAfter.x)+2&&s.x<Math.max(switchBefore.x,switchAfter.x)-2),'Segment selection includes intermediate positions');
  await page.getByRole('button',{name:'工作台',exact:true}).click();await settle(page,650);
  assert.equal(await page.locator('input[name=cpu]').inputValue(),'1234');
  assert.equal(await page.getByRole('textbox',{name:'筛选当前页工作',exact:true}).inputValue(),'校园');
  return {nav_before:before,nav_after:after,nav_intermediate_samples:samples.filter(s=>s.y!==before.y&&s.y!==after.y).length,switch_before:switchBefore,switch_after:switchAfter,draft_preserved:true};
});

await check('query-refresh-preserves-results',async({page,fixture})=>{
  await goto(page,'#run');
  const search=page.getByRole('textbox',{name:'筛选当前页工作',exact:true});
  const panel=page.locator('.panel').filter({has:search});
  const card=panel.locator('tbody tr').first();await card.waitFor();
  await card.evaluate(el=>{window.__retainedQueryCard=el;});
  const gate=fixture.holdNext('/x/v1/work-intents');
  await panel.getByRole('button',{name:'刷新',exact:true}).click();await gate.started;
  await settle(page,150);
  assert.ok(await card.evaluate(el=>el===window.__retainedQueryCard&&el.isConnected),'Refresh retains the existing result node');
  assert.ok(await panel.getByText('正在更新，当前显示上次读取的结果。',{exact:true}).isVisible());
  assert.ok(await card.isVisible(),'Existing results remain visible while pending');
  await screenshot(page,'1440-query-pending-retains-result');
  const samplesPromise=panel.evaluate(async el=>{const samples=[];const start=performance.now();while(performance.now()-start<700){samples.push(...el.getAnimations({subtree:true}).map(a=>({at:performance.now()-start,target:a.effect?.target?.className,name:a.animationName||'',keyframes:a.effect?.getKeyframes()})));await new Promise(requestAnimationFrame);}return samples;});
  gate.release();const samples=await samplesPromise;await settle(page,100);
  assert.ok(await card.evaluate(el=>el===window.__retainedQueryCard&&el.isConnected),'Successful refresh also preserves result DOM identity');
  assert.ok(samples.some(s=>String(s.target).includes('query-result-motion')&&s.keyframes?.some(frame=>frame.opacity!==undefined||frame.transform!==undefined)),'Updated query results include a reveal animation');
  return {preserved_during_refresh:true,preserved_after_refresh:true,animation_targets:[...new Set(samples.map(sample=>sample.target))]};
});

await check('copy-full-id-success-and-failure',async({context,page,fixture})=>{
  await context.addInitScript(()=>{window.__clipboardWrites=[];window.__clipboardFails=false;Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{window.__clipboardWrites.push(value);if(window.__clipboardFails)throw new DOMException('Synthetic clipboard denial','NotAllowedError');}}});});
  const id='work-4c4b4f5e-b951-4b70-8aac-2dd706cd0f82';
  const work=structuredClone(fixture.works.get(ids.active));work.intent_id=id;work.allocation_ids=[`${id}-lease`];work.job_ids=[`${id}-job`];fixture.works.set(id,work);
  await goto(page,`#continuity?intent=${id}`);
  const copy=page.getByRole('button',{name:`复制工作 ID ${id}`,exact:true}).first();await copy.click();
  await page.getByText('已复制完整工作 ID',{exact:true}).first().waitFor();
  assert.equal((await page.evaluate(()=>window.__clipboardWrites)).at(-1),id,'Copy must use the complete ID');
  assert.ok(page.url().includes(`#continuity?intent=${id}`),'Copy does not navigate');
  await page.evaluate(()=>{window.__clipboardFails=true;});await copy.click();
  const fallback=page.getByRole('textbox',{name:'完整工作 ID，选中后手动复制',exact:true}).first();await fallback.waitFor();
  assert.equal(await fallback.inputValue(),id);await fallback.focus();
  assert.equal(await fallback.evaluate(el=>el.selectionEnd-el.selectionStart),id.length,'Fallback selects the full value');
  await screenshot(page,'1440-copy-failure-fallback');
  return {full_id:id,successful_copy:true,denied_clipboard_fallback:true};
});

await check('copy-pending-keyboard-focus-and-reentry',async({context,page})=>{
  await context.addInitScript(()=>{
    window.__clipboardWrites=[];window.__clipboardPending=[];
    Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:value=>{
      window.__clipboardWrites.push(value);
      return new Promise((resolve,reject)=>window.__clipboardPending.push({resolve,reject}));
    }}});
  });
  await goto(page,`#continuity?intent=${ids.active}`);
  const copy=page.getByRole('button',{name:`复制工作 ID ${ids.active}`,exact:true}).first();
  await copy.focus();await page.keyboard.press('Enter');
  await page.waitForFunction(()=>document.querySelector('.work-copy-actions .copy-work-id-button')?.getAttribute('aria-busy')==='true');
  await settle(page,150);
  assert.equal(await copy.evaluate(el=>document.activeElement===el),true,'Pending clipboard work must retain keyboard focus');
  assert.equal(await copy.evaluate(el=>el.disabled),false,'Pending copy stays in the keyboard focus order');
  assert.equal(await copy.getAttribute('aria-disabled'),'true');
  await page.keyboard.press('Enter');await page.keyboard.press('Space');
  assert.deepEqual(await page.evaluate(()=>window.__clipboardWrites),[ids.active],'Repeated keyboard activation cannot enter another clipboard write');
  await page.evaluate(()=>window.__clipboardPending[0].resolve());
  await page.waitForFunction(()=>document.querySelector('.work-copy-actions .copy-work-id-button')?.getAttribute('aria-busy')==='false');
  assert.equal(await copy.evaluate(el=>document.activeElement===el),true,'Successful copy leaves focus on its control');

  await page.keyboard.press('Enter');
  await page.waitForFunction(()=>window.__clipboardPending.length===2);
  await page.keyboard.press('Tab');
  await page.evaluate(()=>{window.__copyMovedFocus=document.activeElement;});
  assert.equal(await copy.evaluate(el=>document.activeElement===el),false,'The user can leave a pending copy');
  await page.evaluate(()=>window.__clipboardPending[1].reject(new DOMException('Synthetic clipboard denial','NotAllowedError')));
  await page.getByRole('textbox',{name:'完整工作 ID，选中后手动复制',exact:true}).first().waitFor();
  assert.equal(await page.evaluate(()=>document.activeElement===window.__copyMovedFocus),true,'A late failure must not steal focus back');
  assert.deepEqual(await page.evaluate(()=>window.__clipboardWrites),[ids.active,ids.active]);
  return {pending_focus_retained:true,repeated_activation_writes_once:true,success_focus_retained:true,late_failure_does_not_steal_focus:true,clipboard:'Deferred synthetic clipboard promises; operating-system permission UI not exercised'};
});

await check('disclosures-button-affordance-keyboard',async({page})=>{
  await goto(page,'#resources');
  const summary=page.locator('summary').filter({hasText:'高级 · 登记模拟供给'}).first();
  await summary.scrollIntoViewIfNeeded();await summary.focus();
  const before=await summary.evaluate(el=>{const style=getComputedStyle(el),after=getComputedStyle(el,'::after');return {height:el.getBoundingClientRect().height,border:style.borderTopWidth,bg:style.backgroundColor,cursor:style.cursor,after:after.content,open:el.parentElement.open};});
  assert.ok(before.height>=40,'Disclosure has a usable button target');assert.ok(Number.parseFloat(before.border)>0,'Disclosure has visible boundary');assert.equal(before.cursor,'pointer');
  await page.keyboard.press('Enter');await settle(page,500);assert.equal(await summary.evaluate(el=>el.parentElement.open),true,'Native Enter opens disclosure');
  await page.keyboard.press('Space');await settle(page,500);assert.equal(await summary.evaluate(el=>el.parentElement.open),false,'Native Space closes disclosure');
  await page.locator('summary').filter({hasText:'查看可见资源池'}).first().click();await settle(page,500);
  await screenshot(page,'1440-disclosure-buttons');
  return {computed:before,native_enter_and_space:true};
});

await check('reduced-motion',async({page})=>{
  await goto(page);
  const button=page.getByRole('button',{name:'显示设置',exact:true});const box=await button.boundingBox();
  await page.mouse.move(box.x+box.width/2,box.y+box.height/2);await page.mouse.down();await settle(page,160);
  const pressed=await transformScale(button);assert.ok(Math.abs(pressed.scale-1)<.001,'Reduced motion disables compression');
  await page.mouse.up();await settle(page,40);
  assert.equal(await button.evaluate(el=>el.getAnimations().filter(a=>a.playState==='running').length),0,'Reduced motion disables JS release animations');
  const CSS=await button.evaluate(el=>({transition:getComputedStyle(el).transitionDuration,animation:getComputedStyle(el).animationName}));
  assert.equal(CSS.transition,'0s');assert.equal(CSS.animation,'none');
  await page.getByRole('button',{name:'关闭显示设置',exact:true}).click();
  await page.getByRole('button',{name:'工作台',exact:true}).click();await settle(page,40);
  assert.equal(await page.locator('.space-switch-selection').evaluate(el=>el.getAnimations().filter(a=>a.playState==='running').length),0);
  return {pressed,css:CSS};
},{reducedMotion:'reduce'});

for(const width of [1440,390])await check(`responsive-${width}`,async({page})=>{
  await goto(page,'#resources');
  await openDetails(page,'查看可见资源池');
  await page.locator('summary').filter({hasText:'高级 · 登记模拟供给'}).first().scrollIntoViewIfNeeded();await settle(page);
  const widths=await page.evaluate(()=>({window:innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth,main:document.querySelector('main')?.getBoundingClientRect().width,canvas:document.querySelectorAll('canvas').length}));
  assert.ok(widths.document<=width+1&&widths.body<=width+1,`No horizontal viewport overflow: ${JSON.stringify(widths)}`);
  await screenshot(page,`${width}-resources-disclosures`);
  await page.getByRole('button',{name:'总览',exact:true}).click();await settle(page,600);await screenshot(page,`${width}-overview`);
  return widths;
},{viewport:{width,height:width===1440?900:844},isMobile:width===390,hasTouch:width===390});

result.finished_at=new Date().toISOString();result.status=result.checks.some(check=>check.status==='FAIL')?'FAIL':'PASS';
await persist();
console.log(JSON.stringify({status:result.status,evidence:out,checks:result.checks},null,2));
if(result.status==='FAIL')process.exitCode=1;
await browser.close();
