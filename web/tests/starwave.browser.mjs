/** Real Chromium acceptance of CP4 with strictly intercepted, synthetic APIs.
 * Install playwright-core in a separate tooling directory; no product dependency.
 * STARWAVE_URL=http://127.0.0.1:15186 node web/tests/starwave.browser.mjs
 * Optional STARWAVE_BASELINE=1 captures the current visual shell without Starwave checks.
 * STARWAVE_PREVIEW=1 performs production-bundle checks without dev-only debug hooks.
 * STARWAVE_EXTRA=1 uses an isolated headed Chrome for visibility, DPR and small-height checks.
 */
import assert from 'node:assert/strict';
import {mkdir,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {installFixtures} from './starwave.fixtures.mjs';

const root = resolve(import.meta.dirname, '../..');
const baseUrl = process.env.STARWAVE_URL || 'http://127.0.0.1:15186';
const origin = new URL(baseUrl).origin;
assert.ok(['127.0.0.1', 'localhost'].includes(new URL(origin).hostname), 'Only loopback browser previews are allowed');
const {chromium} = await import(pathToFileURL(process.env.PLAYWRIGHT_CORE || resolve(root, 'runtime/starwave-browser/node_modules/playwright-core/index.mjs')));
const baseline = process.env.STARWAVE_BASELINE === '1';
const preview = process.env.STARWAVE_PREVIEW === '1';
const extra = process.env.STARWAVE_EXTRA === '1';
const out = resolve(root, process.env.STARWAVE_EVIDENCE || `evidence/cp4/starwave/${baseline ? 'baseline' : preview ? 'production-preview' : extra ? 'supplemental' : 'browser'}-${new Date().toISOString().replaceAll(':','-')}`);
await mkdir(out,{recursive:true});
const browser = await chromium.launch({headless:!extra,
  ...(extra ? {ignoreDefaultArgs:['--disable-backgrounding-occluded-windows','--disable-renderer-backgrounding','--disable-background-timer-throttling']} : {}),
  executablePath: process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const context = await browser.newContext({viewport:{width:1440,height:900},deviceScaleFactor:1, reducedMotion:'no-preference'});
const fixture = await installFixtures(context, origin);
const page = await context.newPage();
page.setDefaultTimeout(10000);
const errors = [], consoleMessages = [], checks = [], screenshots = [];
page.on('pageerror', error => errors.push(error.message));
page.on('console', message => {if (['error','warning','warn'].includes(message.type())) consoleMessages.push({type:message.type(),text:message.text()});});
const report = {started_at:new Date().toISOString(),browser:browser.version(),platform:process.platform,
  scope:'Real browser rendering and interactions; every business response and mutation is synthetic and intercepted. No running Fabric API is used.',
  baseUrl,baseline,preview,extra,headed:extra,checks,screenshots,errors,consoleMessages,requests:fixture.requests,deniedExternalRequests:fixture.denied,
  notApplicable:['CP4 has no Portal / Popover / Tooltip component or sidebar-collapse implementation; native confirm and select controls are tested separately.'],
  unverified:['Native macOS reduced-motion toggle','Safari and Firefox','Physical phone / screen reader','Long-duration energy consumption','Production business execution','Browser BFCache restoration']};
async function check(name, run) {
  try { const details = await run(); checks.push({name,status:'PASS',...(details === undefined ? {} : {details})}); console.log(`PASS ${name}`); }
  catch(error) { checks.push({name,status:'FAIL',error:error.stack}); console.error(`FAIL ${name}: ${error.message}`); }
}
async function snap(name) {const path=resolve(out,`${name}.png`);await page.screenshot({path});screenshots.push({name,path,viewport:page.viewportSize(),data:'synthetic API fixtures'});return path;}
const state = () => page.evaluate(() => document.querySelector('main.sw-stage')?.__starwave?.getDebugState());
const clear = () => page.evaluate(() => document.querySelector('main.sw-stage')?.__starwave?.clearWaves());
const sleep = ms => page.waitForTimeout(ms);
const scroller = () => page.locator('.sw-content');
async function nav(id) {await page.locator(`nav a[href="#${id}"]`).click();await sleep(120);}
async function blank() {const b=await page.locator('main.sw-stage').boundingBox();return {x:b.x+12,y:b.y+Math.min(230,b.height/2)};}
async function clickBlank() {const p=await blank();await page.mouse.click(p.x,p.y);return p;}
async function stablePage() {await page.getByRole('status').filter({hasText:'服务在线'}).first().waitFor();await page.locator('.panel').first().waitFor();}

try {
  await page.goto(baseUrl,{waitUntil:'networkidle'});await stablePage();
  if (baseline) {
    await snap('before-run-1440x900');
    await nav('resources');await snap('before-resources-1440x900');
    await page.setViewportSize({width:390,height:844});await sleep(180);await snap('before-resources-390x844');
    await check('baseline navigation and no external requests',async()=>{assert.equal(fixture.denied.length,0);assert.equal(await page.locator('nav a').count(),6);});
  } else if (extra) {
    await page.waitForFunction(()=>document.querySelector('main.sw-stage')?.__starwave?.ready);
    await check('real hidden tab suspends animation and resumes without a time jump',async()=>{
      const cover=await context.newPage();
      try {
        await cover.goto('about:blank');await page.bringToFront();
        await page.waitForFunction(()=>document.visibilityState==='visible',{},{polling:100});await sleep(150);
        const before=await state();
        await cover.bringToFront();
        // RAF polling is itself suspended in a hidden tab, so inspect on a timer.
        try {await page.waitForFunction(()=>document.hidden,{},{timeout:5000,polling:100});}
        catch {
          const primary=await page.evaluate(()=>({hidden:document.hidden,visibility:document.visibilityState,focus:document.hasFocus()}));
          const second=await cover.evaluate(()=>({hidden:document.hidden,visibility:document.visibilityState,focus:document.hasFocus()}));
          throw new Error(`Native visibility transition NOT_VERIFIED in isolated headed browser. Primary: ${JSON.stringify(primary)}; cover: ${JSON.stringify(second)}. This does not establish an engine defect.`);
        }
        await sleep(150);const hiddenBefore=await state();
        const hiddenState=await page.evaluate(()=>({hidden:document.hidden,visibilityState:document.visibilityState}));
        await sleep(800);const hiddenAfter=await state();
        assert.equal(hiddenBefore.elapsedMs,hiddenAfter.elapsedMs);
        assert.equal(hiddenBefore.renderedFrames,hiddenAfter.renderedFrames);
        await page.bringToFront();await page.waitForFunction(()=>!document.hidden,{},{polling:100});await sleep(200);const resumed=await state();
        assert.ok(resumed.renderedFrames>hiddenAfter.renderedFrames);
        assert.ok(resumed.elapsedMs>hiddenAfter.elapsedMs);
        assert.ok(resumed.elapsedMs-hiddenAfter.elapsedMs<500);
        return {method:'Separate temporary Chrome profile; real tabs brought to front; document.hidden was not mocked',before,hiddenState,hiddenBefore,hiddenAfter,resumed};
      } finally {await cover.close();await page.bringToFront();}
    });
    await check('DPR 2 caps bitmap scaling at 1.75',async()=>{
      const highContext=await browser.newContext({viewport:{width:1440,height:900},deviceScaleFactor:2,reducedMotion:'no-preference'});
      const highFixture=await installFixtures(highContext,origin);
      try {
        const high=await highContext.newPage();const highErrors=[];high.on('pageerror',e=>highErrors.push(e.message));
        await high.goto(baseUrl,{waitUntil:'networkidle'});
        await high.waitForFunction(()=>document.querySelector('main.sw-stage')?.__starwave?.ready);
        const info=await high.evaluate(()=>{
          const host=document.querySelector('main.sw-stage'),canvas=host.querySelector('canvas'),debug=host.__starwave.getDebugState();
          return {reportedDpr:devicePixelRatio,cssWidth:debug.width,cssHeight:debug.height,bitmapWidth:canvas.width,bitmapHeight:canvas.height,
            pixelArea:canvas.width*canvas.height,rgbaBytes:canvas.width*canvas.height*4,canvasCount:document.querySelectorAll('.fabric-starwave-canvas').length};
        });
        assert.equal(info.reportedDpr,2);assert.equal(info.bitmapWidth,Math.round(info.cssWidth*1.75));
        assert.equal(info.bitmapHeight,Math.round(info.cssHeight*1.75));assert.equal(info.canvasCount,1);
        assert.ok(info.pixelArea<=Math.round(1440*1.75)*Math.round(900*1.75));
        const path=resolve(out,'dpr2-1440x900.png');await high.screenshot({path});
        screenshots.push({name:'dpr2-1440x900',path,viewport:{width:1440,height:900},deviceScaleFactor:2,data:'synthetic API fixtures'});
        assert.deepEqual(highErrors,[]);assert.deepEqual(highFixture.denied,[]);
        return {...info,scalingCap:1.75,uncaughtErrors:highErrors,interceptedRequests:highFixture.requests.length};
      } finally {await highContext.close();await page.bringToFront();}
    });
    for (const viewport of [{width:720,height:450},{width:360,height:225}]) await check(`${viewport.width} by ${viewport.height} CSS viewport keeps scrolling and controls reachable`,async()=>{
      await page.setViewportSize(viewport);await nav('run');await sleep(180);
      const layout=await page.evaluate(()=>({width:innerWidth,height:innerHeight,documentWidth:document.documentElement.scrollWidth,
        contentHeight:document.querySelector('.sw-content').clientHeight,scrollHeight:document.querySelector('.sw-content').scrollHeight,
        documentHeight:document.documentElement.scrollHeight,mainY:document.querySelector('main').getBoundingClientRect().y,
        mainHeight:document.querySelector('main').clientHeight,canvasHeight:document.querySelector('canvas').height}));
      assert.ok(layout.documentWidth<=viewport.width);assert.ok(layout.contentHeight>=160);assert.ok(layout.scrollHeight>layout.contentHeight);
      assert.ok(layout.mainHeight<=viewport.height);assert.ok(layout.canvasHeight<=viewport.height*1.75);
      await snap(`small-height-first-${viewport.width}x${viewport.height}`);
      await page.locator('input[name=command]').click();await page.locator('input[name=command]').fill('invalid json');
      const inputPosition=await page.locator('input[name=command]').evaluate(el=>{const b=el.getBoundingClientRect();return {top:b.top,bottom:b.bottom,viewportHeight:innerHeight};});
      const visible=inputPosition.top>=0&&inputPosition.bottom<=inputPosition.viewportHeight;
      assert.equal(visible,true,JSON.stringify(inputPosition));
      await page.getByRole('button',{name:'提交工作',exact:true}).click();
      await page.locator('.feedback.error').waitFor();await page.locator('.feedback.error').scrollIntoViewIfNeeded();
      assert.equal(fixture.requests.filter(r=>r.method!=='GET').length,0);
      await snap(`small-height-validation-${viewport.width}x${viewport.height}`);
      await page.locator('nav a[href="#resources"]').click();await page.locator('.stat-row').waitFor();
      return {method:'720×450 and 360×225 CSS viewports approximate 200% and 400% page-zoom layout from 1440×900; browser zoom itself was not changed',layout,
        commandInputReachable:visible,inputPosition,validationRejectedBeforeAPI:true};
    });
    await check('supplemental checks produce no page errors or external requests',async()=>{assert.deepEqual(errors,[]);assert.deepEqual(fixture.denied,[]);});
  } else if (preview) {
    await check('production canvas and no development hook',async()=>{
      await page.locator('.fabric-starwave-canvas').waitFor();
      assert.equal(await page.locator('.fabric-starwave-canvas').count(),1);
      assert.equal(await state(),undefined);
      assert.equal(await page.locator('.sidebar canvas').count(),0);
      assert.equal(await page.locator('.sidebar').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(0, 0, 0)');
      await snap('production-run-1440x900');
    });
    await check('production pause control and invalid input preserve business behavior',async()=>{
      await page.getByRole('button',{name:'暂停背景动效',exact:true}).click();
      await page.getByRole('button',{name:'恢复背景动效',exact:true}).waitFor();
      await page.getByRole('button',{name:'恢复背景动效',exact:true}).click();
      await page.locator('input[name=command]').fill('invalid json');
      await page.getByRole('button',{name:'提交工作',exact:true}).click();
      await page.locator('.feedback.error').waitFor();
      assert.equal(fixture.requests.filter(r=>r.method!=='GET').length,0);
    });
    await check('production six routes, errors, details and mobile screenshot',async()=>{
      for(const id of ['resources','continuity','decisions','events','acceptance','resources']) {
        await nav(id);assert.equal(await page.locator('.fabric-starwave-canvas').count(),1);
      }
      await page.getByText('Contributor or administrator authority is required.',{exact:true}).waitFor();
      await page.getByText('查看服务端原始记录',{exact:true}).first().click();
      assert.equal(await page.locator('details.raw').first().getAttribute('open'),'');
      await snap('production-resources-1440x900');
      await page.setViewportSize({width:390,height:844});await nav('run');await sleep(180);
      assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
      await snap('production-run-390x844');
    });
    await check('production reduced-motion control follows browser preference',async()=>{
      await page.emulateMedia({reducedMotion:'reduce'});
      const button=page.getByRole('button',{name:'背景静态 · 减少动态',exact:true});
      await button.waitFor();assert.equal(await button.isDisabled(),true);
      await page.emulateMedia({reducedMotion:'no-preference'});
      await page.getByRole('button',{name:'暂停背景动效',exact:true}).waitFor();
    });
    await check('production classic rollback and no external requests or exceptions',async()=>{
      await page.goto(`${origin}/?visual=classic#run`,{waitUntil:'networkidle'});await stablePage();
      assert.equal(await page.locator('.fabric-starwave-canvas').count(),0);
      assert.deepEqual(errors,[]);assert.deepEqual(fixture.denied,[]);
    });
  } else {
    await check('one bounded canvas outside pure black sidebar; denser CP4 glass over frozen preset',async()=>{
      await page.waitForFunction(()=>document.querySelector('main.sw-stage')?.__starwave?.ready);
      const detail=await page.evaluate(()=>{
        const stage=document.querySelector('main.sw-stage'),sidebar=document.querySelector('.sidebar'),canvas=stage.querySelector('canvas'),panel=document.querySelector('.panel'),input=document.querySelector('.field input');
        window.__acceptanceCanvas=canvas;
        const c=getComputedStyle(canvas),s=getComputedStyle(stage),p=getComputedStyle(panel),i=getComputedStyle(input);
        return {canvasCount:document.querySelectorAll('.fabric-starwave-canvas').length,sidebarCanvas:sidebar.querySelectorAll('canvas').length,
          sidebarBackground:getComputedStyle(sidebar).backgroundColor,stageBackground:s.backgroundColor,stageBackgroundImage:s.backgroundImage,
          stageHeight:stage.clientHeight,viewportHeight:innerHeight,scrollHeight:document.querySelector('.sw-content').scrollHeight,
          pixelWidth:canvas.width,pixelHeight:canvas.height,pointerEvents:c.pointerEvents,ariaHidden:canvas.getAttribute('aria-hidden'),
          fill:s.getPropertyValue('--sw-glass-alpha').trim(),panelFill:p.backgroundColor,panelOpacity:p.opacity,
          panelBlur:p.backdropFilter,inputBlur:i.backdropFilter,bodyWidth:document.documentElement.scrollWidth,viewportWidth:innerWidth};
      });
      assert.equal(detail.canvasCount,1);assert.equal(detail.sidebarCanvas,0);
      assert.equal(detail.sidebarBackground,'rgb(0, 0, 0)');assert.equal(detail.stageBackground,'rgb(0, 0, 0)');
      assert.equal(detail.stageBackgroundImage,'none');assert.ok(detail.stageHeight<=detail.viewportHeight+1);
      assert.equal(detail.pointerEvents,'none');assert.equal(detail.ariaHidden,'true');assert.equal(Number(detail.fill),.2);
      assert.equal(detail.panelOpacity,'1');assert.match(detail.panelFill,/0\.64\)/);assert.equal(detail.inputBlur,'none');
      assert.match(detail.panelBlur,/blur\(24px\)/);
      assert.ok(detail.bodyWidth<=detail.viewportWidth);return {...detail,state:await state()};
    });
    await snap('run-1440x900');
    await check('blank click ripple 0 → 1 → 0 with visible capture',async()=>{
      await clear();assert.equal((await state()).ripples,0);const point=await clickBlank();await sleep(160);
      assert.equal((await state()).ripples,1);await snap('ripple-1440x900');
      await page.waitForFunction(()=>document.querySelector('main.sw-stage').__starwave.getDebugState().ripples===0,{},{timeout:6000});
      return {point,sequence:[0,1,0],durationMeaning:'active animation time, not background or paused time'};
    });
    await check('four-wave limit and right-click exclusion',async()=>{
      await clear();const p=await blank();for(let i=0;i<7;i++)await page.mouse.click(p.x,p.y+i*10);
      assert.ok((await state()).ripples<=4);const count=(await state()).ripples;await clear();
      await page.mouse.click(p.x,p.y,{button:'right'});await page.keyboard.press('Escape');assert.equal((await state()).ripples,0);return {maxObserved:count};
    });
    await check('double click creates only its first ripple',async()=>{
      await clear();const p=await blank();await page.mouse.dblclick(p.x,p.y);
      assert.equal((await state()).ripples,1);await clear();
      return {expected:'first single click may produce one wave; second click is excluded'};
    });
    await check('native inputs, selects, identity buttons and labels do not create waves',async()=>{
      await clear();await page.getByLabel('任务镜像',{exact:true}).click();
      await page.getByLabel('任务镜像',{exact:true}).fill('python:3.12-slim');
      await page.locator('select[name=preemption]').selectOption('restartable');
      await page.getByRole('button',{name:/当前身份/}).click();
      await page.getByRole('button',{name:/当前身份/}).click();
      assert.equal((await state()).ripples,0);
    });
    await check('keyboard focus and text selection remain usable',async()=>{
      await page.getByLabel('任务镜像',{exact:true}).focus();await page.keyboard.press('Tab');
      const focus=await page.evaluate(()=>({tag:document.activeElement.tagName,outline:getComputedStyle(document.activeElement).outlineStyle,width:getComputedStyle(document.activeElement).outlineWidth}));
      assert.equal(focus.tag,'INPUT');assert.notEqual(focus.outline,'none');assert.notEqual(focus.width,'0px');
      await clear();const h=await page.locator('.intro h1').boundingBox();
      await page.mouse.move(h.x+2,h.y+h.height/2);await page.mouse.down();await page.mouse.move(h.x+130,h.y+h.height/2,{steps:12});await page.mouse.up();
      const selected=await page.evaluate(()=>getSelection()?.toString());assert.ok(selected?.length);assert.equal((await state()).ripples,0);
      await page.getByLabel('任务镜像',{exact:true}).click();return {focus,selected};
    });
    await check('native validation and mocked registration / cancellation / release journey',async()=>{
      await clear();const before=fixture.requests.filter(r=>r.method!=='GET').length;
      await page.locator('input[name=command]').fill('invalid json');
      await page.getByRole('button',{name:'提交工作',exact:true}).click();
      await page.locator('.feedback.error').waitFor();assert.equal(fixture.requests.filter(r=>r.method!=='GET').length,before);
      await page.locator('input[name=command]').fill('["python","-V"]');
      await page.getByRole('button',{name:'提交工作',exact:true}).click();
      await page.getByRole('button',{name:'查询这项工作',exact:true}).waitFor();
      const registration=fixture.requests.filter(r=>r.path==='/x/v1/work-intents'&&r.method==='POST').at(-1);
      assert.equal(registration.body.specification.allocation.preemption_mode,'restartable');
      assert.equal((await state()).ripples,0);
      await page.getByRole('button',{name:'查询这项工作',exact:true}).click();
      await page.getByRole('button',{name:'取消工作',exact:true}).waitFor();
      page.once('dialog',dialog=>dialog.dismiss());await page.getByRole('button',{name:'取消工作',exact:true}).click();
      assert.equal(fixture.requests.filter(r=>r.path.endsWith('/actions')).length,0,'dismissed confirmation sends nothing');
      page.once('dialog',dialog=>dialog.accept());await page.getByRole('button',{name:'取消工作',exact:true}).click();
      await page.waitForFunction(()=>[...document.querySelectorAll('.status')].some(e=>e.textContent==='已取消'));
      page.once('dialog',dialog=>dialog.accept());await page.getByRole('button',{name:'释放租约',exact:true}).click();
      await page.waitForFunction(()=>[...document.querySelectorAll('.status')].some(e=>e.textContent==='已释放'));
      assert.equal((await state()).ripples,0);await snap('mock-cancel-released-1440x900');
      return {kind:'SYNTHETIC_BROWSER_FIXTURE',id:registration.body.intent_id,mutations:fixture.requests.filter(r=>r.method!=='GET').map(r=>({path:r.path,method:r.method,action:r.body?.action}))};
    });
    await check('same canvas across all six routes and back / forward',async()=>{
      for(const id of ['run','resources','continuity','decisions','events','acceptance','resources']) {
        await nav(id);assert.equal(await page.locator('.fabric-starwave-canvas').count(),1);
        assert.equal(await page.evaluate(()=>document.querySelector('.fabric-starwave-canvas')===window.__acceptanceCanvas),true);
      }
      await page.goBack();await page.waitForURL('**/#acceptance');await page.goForward();await page.waitForURL('**/#resources');
      await page.locator('.stat-row').waitFor();await snap('resources-1440x900');
    });
    await check('permission error semantics and native details work under glass',async()=>{
      await page.getByText('Contributor or administrator authority is required.',{exact:true}).waitFor();
      const error=page.locator('.feedback.error').first();assert.equal(await error.getAttribute('role'),'alert');
      await page.getByText('查看服务端原始记录',{exact:true}).first().click();
      assert.equal(await page.locator('details.raw').first().getAttribute('open'),'');assert.equal((await state()).ripples,0);
    });
    await check('long inner scrolling keeps bitmap bounded and ripple aligned',async()=>{
      await nav('events');const before=await state();
      await page.mouse.move(1000,600);await page.mouse.wheel(0,2600);await sleep(200);
      const scroll=await scroller().evaluate(el=>({top:el.scrollTop,height:el.scrollHeight,client:el.clientHeight}));
      assert.ok(scroll.top>0);assert.ok(scroll.height>scroll.client*2);
      const after=await state();assert.equal(after.height,before.height);assert.equal(after.width,before.width);
      await clear();const point=await clickBlank();await sleep(180);assert.equal((await state()).ripples,1);await snap('long-page-scrolled-ripple-1440x900');
      await clear();return {scroll,before:{width:before.width,height:before.height},after:{width:after.width,height:after.height},point};
    });
    await check('loading, empty filtering and disabled pagination remain usable',async()=>{
      fixture.delayMs=650;
      try {
        await nav('run');await page.getByText('正在读取服务端状态…',{exact:true}).first().waitFor();
        assert.ok(await page.getByRole('button',{name:'正在刷新…',exact:true}).first().isDisabled());
        await page.getByRole('button',{name:'fixture-work-completed',exact:true}).waitFor();
      } finally {fixture.delayMs=0;}
      await page.getByText('当前授权范围内没有记录。新记录会在服务端接受后显示。',{exact:true}).first().waitFor();
      await clear();const previous=page.getByRole('button',{name:'上一页',exact:true});
      assert.equal(await previous.isDisabled(),true);await previous.scrollIntoViewIfNeeded();
      const b=await previous.boundingBox();await page.mouse.click(b.x+b.width/2,b.y+b.height/2);assert.equal((await state()).ripples,0);
      await page.getByLabel('筛选当前页工作',{exact:true}).fill('no-such-fixture-work');
      await page.getByText('当前页没有匹配项。',{exact:true}).waitFor();
      assert.equal((await state()).ripples,0);await snap('loading-empty-disabled-1440x900');
      await page.getByLabel('筛选当前页工作',{exact:true}).fill('');
    });
    await check('local pause freezes time and frames; resume preserves time continuity',async()=>{
      await nav('run');await page.getByRole('button',{name:'暂停背景动效',exact:true}).click();await sleep(120);
      const a=await state();await sleep(400);await clickBlank();const b=await state();
      assert.equal(a.elapsedMs,b.elapsedMs);assert.equal(a.renderedFrames,b.renderedFrames);assert.equal(b.ripples,0);
      await page.getByRole('button',{name:'恢复背景动效',exact:true}).click();await sleep(180);const c=await state();
      assert.ok(c.elapsedMs>b.elapsedMs);assert.ok(c.elapsedMs-b.elapsedMs<450);return {pausedBefore:a,pausedAfter:b,resumed:c};
    });
    await check('Chromium emulated reduced motion remains static after local resume',async()=>{
      await page.emulateMedia({reducedMotion:'reduce'});await sleep(150);
      const a=await state();assert.equal(a.systemReduced,true);
      await page.evaluate(()=>document.querySelector('main.sw-stage').__starwave.setPaused(false));
      await clickBlank();await sleep(250);const b=await state();
      assert.equal(a.elapsedMs,b.elapsedMs);assert.equal(a.renderedFrames,b.renderedFrames);assert.equal(b.ripples,0);
      const button=await page.getByRole('button',{name:/系统.*静态|减少动态|背景动效/}).filter({visible:true}).count();
      await page.emulateMedia({reducedMotion:'no-preference'});await sleep(150);
      assert.equal((await state()).systemReduced,false);return {method:'Playwright/Chromium media emulation, not native OS toggle',before:a,after:b,motionControls:button};
    });
    await check('responsive widths and mobile first / scrolled screenshots',async()=>{
      const measurements=[];
      for (const width of [1100,760,390]) {
        await page.setViewportSize({width,height:844});await nav('run');await sleep(180);
        const m=await page.evaluate(()=>({width:innerWidth,documentWidth:document.documentElement.scrollWidth,
          mainWidth:document.querySelector('main').clientWidth,canvasWidth:document.querySelector('canvas').width,
          canvasHeight:document.querySelector('canvas').height,navCount:document.querySelectorAll('nav a').length}));
        assert.ok(m.documentWidth<=m.width);assert.equal(m.navCount,6);measurements.push(m);
      }
      await snap('run-390x844');
      await page.getByLabel('任务镜像',{exact:true}).fill('python:3.12-slim');
      await page.mouse.move(300,650);await page.mouse.wheel(0,750);await sleep(200);await snap('run-scrolled-390x844');
      await nav('resources');await page.locator('.stat-row').waitFor();await snap('resources-390x844');
      return measurements;
    });
    await check('short desktop performance observation',async()=>{
      await page.setViewportSize({width:1440,height:900});await nav('run');
      const samples=[];const start=await state();
      for(let i=0;i<20;i++){await sleep(100);samples.push((await state()).lastFrameMs);}
      const end=await state();samples.sort((a,b)=>a-b);
      return {durationMs:end.elapsedMs-start.elapsedMs,renderedFrames:end.renderedFrames-start.renderedFrames,
        engineDrawMs:{min:samples[0],median:samples[10],p95:samples[18],max:samples[19]},
        caveat:'Short headless Chromium engine samples; excludes full-page composition and energy; not a 45 FPS guarantee.'};
    });
    await check('classic rollback removes canvas and preserves route and form',async()=>{
      await page.goto(`${origin}/?visual=classic#run`,{waitUntil:'networkidle'});await stablePage();
      assert.equal(await page.locator('.fabric-starwave-canvas').count(),0);assert.equal(await page.locator('.cp4-starwave').count(),0);
      await page.getByLabel('任务镜像',{exact:true}).fill('python:3.12-slim');await nav('resources');await page.locator('.stat-row').waitFor();
      await snap('rollback-classic-1440x900');
    });
    await check('Canvas 2D unavailable preserves black shell and business controls',async()=>{
      const fallbackContext=await browser.newContext({viewport:{width:1440,height:900}});await installFixtures(fallbackContext,origin);
      await fallbackContext.addInitScript(()=>{const original=HTMLCanvasElement.prototype.getContext;HTMLCanvasElement.prototype.getContext=function(type,...args){return type==='2d'?null:original.call(this,type,...args);};});
      const fallback=await fallbackContext.newPage();const fallbackErrors=[];fallback.on('pageerror',e=>fallbackErrors.push(e.message));
      await fallback.goto(baseUrl,{waitUntil:'networkidle'});await fallback.locator('.panel').first().waitFor();
      assert.equal(await fallback.locator('.fabric-starwave-canvas').count(),0);
      assert.equal(await fallback.locator('main').evaluate(el=>getComputedStyle(el).backgroundColor),'rgb(0, 0, 0)');
      await fallback.getByLabel('任务镜像',{exact:true}).fill('python:3.12-slim');
      await fallback.locator('nav a[href="#resources"]').click();await fallback.locator('.stat-row').waitFor();
      const path=resolve(out,'canvas-unavailable-1440x900.png');await fallback.screenshot({path});screenshots.push({name:'canvas-unavailable-1440x900',path,viewport:{width:1440,height:900},data:'synthetic API fixtures'});
      assert.deepEqual(fallbackErrors,[]);await fallbackContext.close();return {failure:'getContext(2d) returns null',uncaughtErrors:fallbackErrors};
    });
    await check('no unexpected page exceptions or external requests',async()=>{assert.deepEqual(errors,[]);assert.deepEqual(fixture.denied,[]);return {allBusinessRequestsIntercepted:true};});
  }
} catch(error) {checks.push({name:'harness setup',status:'FAIL',error:error.stack});}
finally {
  report.finished_at=new Date().toISOString();report.status=checks.some(c=>c.status==='FAIL')?'FAIL':'PASS';
  await writeFile(resolve(out,'browser-results.json'),JSON.stringify(report,null,2)+'\n');
  console.log(`Evidence: ${out}`);await context.close().catch(()=>{});await browser.close().catch(()=>{});
}
if(report.status==='FAIL')process.exitCode=1;
