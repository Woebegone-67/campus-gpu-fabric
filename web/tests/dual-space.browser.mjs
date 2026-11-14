/** Reproducible dual-space UI acceptance. All business requests use intercepted fixtures.
 * Run only against a prepared local dev build:
 *   DUAL_SPACE_URL=http://127.0.0.1:15186 node web/tests/dual-space.browser.mjs
 * Optional: DUAL_SPACE_EVIDENCE=relative/path; DUAL_SPACE_PREVIEW=1 omits dev-frame inspection.
 * Native trackpad/Safari/VoiceOver behavior is deliberately not inferred from this harness.
 */
import assert from 'node:assert/strict';
import {mkdir, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {installDualSpaceFixtures, ids} from './dual-space.fixtures.mjs';

const root = resolve(import.meta.dirname, '../..');
const baseUrl = process.env.DUAL_SPACE_URL || 'http://127.0.0.1:15186';
const origin = new URL(baseUrl).origin;
assert.ok(['localhost', '127.0.0.1'].includes(new URL(origin).hostname), 'Local isolated target required');
const preview = process.env.DUAL_SPACE_PREVIEW === '1';
const out = resolve(root, process.env.DUAL_SPACE_EVIDENCE || `evidence/cp4/dual-space/browser-${new Date().toISOString().replaceAll(':', '-')}`);
await mkdir(out, {recursive: true});
const {chromium} = await import(pathToFileURL(resolve(root, 'runtime/starwave-browser/node_modules/playwright-core/index.mjs')));
const browser = await chromium.launch({headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const result = {started_at: new Date().toISOString(), baseUrl, preview, checkFilter: process.env.DUAL_SPACE_CHECK || null, browser: browser.version(),
  scope: 'Real Chrome rendering and interaction with synthetic API fixtures only; no live Fabric backend or business writes.',
  checks: [], screenshots: [], errors: [], requests: [], denied: [],
  unverified: ['Native two-finger gestures and system history conflicts on physical Chrome/Safari trackpads.',
    'Native OS reduced-motion setting; this harness uses Chromium media emulation.',
    'VoiceOver, complete accessibility conformance, real API authorization, server business correctness, and file-download integrity.']};
const sleep = (page, ms = 180) => page.waitForTimeout(ms);
const pane = (page, space = 'workbench') => page.locator(`[data-space-scroll="${space}"]`);
const control = (page, label) => page.getByRole('button', {name: label, exact: true});
async function expectSpace(page, space, allowGate = false) {
  await page.waitForFunction(value => document.querySelector('[data-space]')?.getAttribute('data-space') === value, space);
  await (allowGate ? page.locator('.account-gate') : pane(page, space)).waitFor({state: 'visible'});
  await sleep(page, 450);
}
async function switchSpace(page, space) {
  await control(page, space === 'overview' ? '总览' : '工作台').click();
  await expectSpace(page, space);
}
async function section(page, id) {
  if (await page.locator('[data-space]').getAttribute('data-space') !== 'workbench') await switchSpace(page, 'workbench');
  await page.locator(`a[href="#${id}"]:visible`).first().click();
  await page.waitForURL(url => url.hash.split('?')[0] === `#${id}`);
  await sleep(page);
}
async function openDetails(page, summary) {
  const heading = page.locator('summary').filter({hasText: summary}).first();
  if (await heading.count() && !await heading.evaluate(el => el.parentElement.open)) await heading.click();
}
async function openNewWork(page) {
  if (!await page.locator('input[name=cpu]').isVisible()) await control(page, '新建工作').click();
  await page.locator('input[name=cpu]').waitFor({state: 'visible'});
}
async function openAdvanced(page) {
  await openNewWork(page); await openDetails(page, '高级设置');
  await page.locator('input[name=timeout]').waitFor({state: 'visible'});
}
async function openIdentity(page) {
  if (!await page.getByLabel('账户', {exact: true}).isVisible()) {
    await page.locator('button.account-trigger').click();
    await control(page, '切换账户').click();
  }
}
async function applyIdentity(page, owner, token = `fixture-${owner}`) {
  await openIdentity(page);
  await page.getByLabel('账户', {exact: true}).fill(owner);
  await page.getByLabel('访问凭证', {exact: true}).fill(token);
  await page.locator('.identity-manual').getByRole('button', {name: '连接账户', exact: true}).click();
  await sleep(page, 400);
  if (await page.getByLabel('账户', {exact: true}).isVisible()) {
    await page.getByRole('button', {name: '关闭账户菜单', exact: true}).click();
  }
}
async function snap(page, name) {
  const path = resolve(out, `${name}.png`);
  await page.screenshot({path});
  result.screenshots.push({name, path, viewport: page.viewportSize(), data: 'synthetic browser fixtures'});
}
async function frames(page) {
  return page.evaluate(() => {
    const host = [...document.querySelectorAll('.sw-stage')].find(el => el.__starwave);
    return host?.__starwave?.getDebugState();
  });
}
async function hoverGestureBlank(page) {
  const point = await page.locator('[data-space-gesture-region]').evaluate(element => {
    const r = element.getBoundingClientRect();
    for (let y = r.top + 5; y < r.bottom - 5; y += 8) {
      for (let x = r.right - 5; x > r.left + 5; x -= 12) {
        if (document.elementFromPoint(x, y) === element) return {x, y};
      }
    }
    return null;
  });
  assert.ok(point, 'Gesture page header needs a real empty hit area');
  await page.mouse.move(point.x, point.y);
  return point;
}
async function scrollPaneToTop(page, space) {
  const current = await pane(page, space).evaluate(el => el.scrollTop);
  if (current <= 0) return;
  const box = await pane(page, space).boundingBox();
  await page.mouse.move(box.x + box.width - 10, box.y + box.height / 2);
  await page.mouse.wheel(0, -current - 1);
  await page.waitForFunction(name => document.querySelector(`[data-space-scroll="${name}"]`)?.scrollTop === 0, space);
  await sleep(page, 450);
}
async function check(name, run, options = {}) {
  if (process.env.DUAL_SPACE_CHECK && !new RegExp(process.env.DUAL_SPACE_CHECK).test(name)) return;
  const context = await browser.newContext({viewport: {width: 1440, height: 900}, deviceScaleFactor: 1, ...options});
  const fixture = await installDualSpaceFixtures(context, origin), page = await context.newPage();
  page.setDefaultTimeout(10000);
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  try {
    const details = await run({page, context, fixture});
    assert.equal(fixture.requests.filter(request => request.method !== 'GET').length, 0, 'No business write is allowed in view/navigation acceptance');
    assert.deepEqual(fixture.denied, []); assert.deepEqual(errors, []);
    result.checks.push({name, status: 'PASS', details}); console.log(`PASS ${name}`);
  } catch (error) {
    result.checks.push({name, status: 'FAIL', error: error.stack}); console.error(`FAIL ${name}: ${error.message}`);
    try {await snap(page, `failure-${result.checks.length}`);} catch {}
  } finally {
    fixture.releaseAll(); result.requests.push(...fixture.requests); result.denied.push(...fixture.denied); result.errors.push(...errors);
    await context.close();
  }
}
async function goto(page, hash = '#overview') {
  await page.goto(`${origin}/${hash}`, {waitUntil: 'networkidle'});
  await page.locator('[data-space]').waitFor();
  if (await page.locator('.account-gate').isVisible()) await applyIdentity(page, 'alice');
}

try {
  await check('default overview loads scoped work and real session capabilities without mutations', async ({page, fixture}) => {
    await page.goto(baseUrl, {waitUntil: 'networkidle'});
    assert.equal(fixture.requests.filter(request => !['/healthz', '/v1/version'].includes(request.path.split('?')[0])).length, 0,
      'Fresh page must not assume an Alice identity');
    await applyIdentity(page, 'alice'); await expectSpace(page, 'overview');
    assert.equal(await control(page, '总览').isVisible(), true); assert.equal(await control(page, '工作台').isVisible(), true);
    assert.ok(fixture.requests.some(request => request.path === '/x/v1/session'));
    const text = await pane(page, 'overview').innerText();
    assert.ok(text.includes('工作')); assert.ok(!text.includes('Bob 独立工作'));
    assert.ok(!text.includes('全校算力运行')); assert.ok(!text.includes('实时 GPU 网络'));
    await snap(page, 'overview-1440x900');
    return {scope: 'Alice loaded objects; synthetic use_compute and contribute_resources projection', readRequests: fixture.requests.length};
  });
  await check('overview joins authorized lease records and prioritizes dismissible factual reminders', async ({page, fixture}) => {
    assert.ok([...fixture.works.values()].every(item => !Object.hasOwn(item, 'allocations')),
      'The actual work-list contract supplies allocation_ids, never embedded allocations');
    await goto(page); await expectSpace(page, 'overview');
    assert.ok(fixture.requests.some(request => request.path === '/v1/allocations?limit=50' && request.owner === 'alice'));
    const attention = page.locator('.overview-attention');
    const rowIds = async () => attention.locator('.attention-row').evaluateAll(rows => rows.map(row =>
      new URLSearchParams(row.querySelector('a[href*="intent="]').getAttribute('href').split('?')[1]).get('intent')));
    assert.deepEqual(await rowIds(), [ids.stopping, ids.notice, ids.attention]);
    for (let index = 0; index < 3; index++) await attention.locator('.attention-row').first().getByRole('button', {name: '本次收起', exact: true}).click();
    assert.deepEqual(await rowIds(), [ids.resumable, ids.retained]);
    assert.ok(!await attention.locator(`a[href*="intent=${ids.waiting}"]`).count(), 'Ordinary Pending is not a user-action warning');
    for (let index = 0; index < 2; index++) await attention.locator('.attention-row').first().getByRole('button', {name: '本次收起', exact: true}).click();
    assert.ok(/本次已收起\s*5\s*项提示/.test(await attention.innerText()));
    await attention.getByRole('button', {name: '展开已收起提示', exact: true}).click();
    assert.deepEqual(await rowIds(), [ids.stopping, ids.notice, ids.attention]);
    const recent = await page.locator('.overview-results .overview-result').evaluateAll(links => links.map(link =>
      new URLSearchParams(link.getAttribute('href').split('?')[1]).get('intent')));
    assert.deepEqual(recent, [ids.completed, ids.retained], 'Recent records use recorded completion time, not API list order');
    await snap(page, 'overview-linked-reminders-1440x900');
    return {source: '/v1/allocations?limit=50', firstThree: await rowIds(), recent, ordinaryWaitingExcluded: true};
  });
  await check('failed lease reads preserve work facts without inventing resource reminders', async ({page, fixture}) => {
    fixture.failures.set('/v1/allocations', {status: 503, body: {error: 'synthetic_lease_failure', detail: 'Synthetic lease projection unavailable'}});
    await goto(page); await expectSpace(page, 'overview');
    const attention = page.locator('.overview-attention');
    const rows = await attention.locator('.attention-row').evaluateAll(elements => elements.map(row =>
      new URLSearchParams(row.querySelector('a[href*="intent="]').getAttribute('href').split('?')[1]).get('intent')));
    assert.deepEqual(rows, [ids.attention], 'Only manual_action is a known work fact without lease data');
    const content = await pane(page, 'overview').innerText();
    assert.ok(/租约.*(未|失败|不可|无法)|Synthetic lease projection unavailable/.test(content));
    await snap(page, 'overview-lease-unavailable-1440x900');
    return {independentWorkReminderRetained: true, leaseDependentReminders: 0};
  });
  await check('overview opens the same work in workbench and browser history returns', async ({page}) => {
    await goto(page); await expectSpace(page, 'overview');
    const link = pane(page, 'overview').locator(`a[href*="intent=${ids.active}"]`).first();
    await link.click(); await expectSpace(page, 'workbench');
    await page.waitForURL(url => url.hash.includes(`intent=${ids.active}`));
    await page.getByLabel('工作 ID', {exact: true}).waitFor();
    assert.equal(await page.getByLabel('工作 ID', {exact: true}).inputValue(), ids.active);
    await snap(page, 'work-detail-1440x900');
    await page.goBack(); await expectSpace(page, 'overview');
    await page.goForward(); await expectSpace(page, 'workbench');
    assert.ok(page.url().includes(ids.active));
  });
  await check('same-session drafts, current-page filters and workbench scroll survive space switching', async ({page}) => {
    await goto(page, '#run'); await expectSpace(page, 'workbench');
    const search = page.getByRole('textbox', {name: '筛选当前页工作', exact: true});
    await search.fill('queued');
    const status = page.getByLabel('工作状态筛选', {exact: true});
    const waiting = await status.locator('option').allTextContents();
    const label = waiting.find(text => text.includes('等待'));
    assert.ok(label, 'A waiting status filter must be available'); await status.selectOption({label});
    const selected = await status.inputValue();
    await openAdvanced(page);
    await page.locator('input[name=image]').fill('draft-session-local-only');
    await page.locator('input[name=cpu]').fill('2500'); await page.locator('input[name=ttl]').fill('777');
    await page.locator('input[name=command]').fill('["python","draft.py"]');
    await page.locator('input[name=command]').click(); await page.keyboard.press('Tab');
    const box = await pane(page).boundingBox(); await page.mouse.move(box.x + box.width - 15, box.y + box.height / 2);
    await page.mouse.wheel(0, 380); await sleep(page);
    const before = await pane(page).evaluate(el => el.scrollTop); assert.ok(before > 0);
    await switchSpace(page, 'overview'); await switchSpace(page, 'workbench');
    const after = await pane(page).evaluate(el => el.scrollTop);
    assert.ok(Math.abs(after - before) <= 3, `scroll ${before} → ${after}`);
    assert.equal(await search.inputValue(), 'queued'); assert.equal(await status.inputValue(), selected);
    for (const [field, expected] of [['image', 'draft-session-local-only'], ['cpu', '2500'], ['ttl', '777'], ['command', '["python","draft.py"]']]) {
      assert.equal(await page.locator(`input[name=${field}]`).inputValue(), expected);
    }
    await snap(page, 'restored-workbench-draft-1440x900'); return {scrollBefore: before, scrollAfter: after, status: selected};
  });
  await check('workbench subpage and overview scroll are independent and recover on return', async ({page}) => {
    await goto(page); await expectSpace(page, 'overview');
    const b = await pane(page, 'overview').boundingBox(); await page.mouse.move(b.x + b.width - 15, b.y + b.height / 2);
    await page.mouse.wheel(0, 500); await sleep(page);
    const before = await pane(page, 'overview').evaluate(el => el.scrollTop);
    const beforeAnchor = await page.locator('.overview-work').boundingBox();
    const beforeHeight = await pane(page, 'overview').evaluate(el => ({clientHeight: el.clientHeight, scrollHeight: el.scrollHeight}));
    await switchSpace(page, 'workbench'); await section(page, 'resources');
    await switchSpace(page, 'overview'); const after = await pane(page, 'overview').evaluate(el => el.scrollTop);
    const afterAnchor = await page.locator('.overview-work').boundingBox();
    const afterHeight = await pane(page, 'overview').evaluate(el => ({clientHeight: el.clientHeight, scrollHeight: el.scrollHeight}));
    assert.ok(Math.abs(after - before) <= 3, `overview scroll ${before} → ${after}; anchor y ${beforeAnchor.y} → ${afterAnchor.y}; heights ${JSON.stringify(beforeHeight)} → ${JSON.stringify(afterHeight)}`); await switchSpace(page, 'workbench'); assert.equal(new URL(page.url()).hash, '#resources');
    return {overviewScrollBefore: before, overviewScrollAfter: after};
  });
  await check('old six workspace hashes and work deep link remain direct and read-only', async ({page}) => {
    for (const hash of ['#run', '#resources', '#continuity', '#decisions', '#events', '#acceptance', `#continuity?intent=${ids.completed}`]) {
      await goto(page, hash); await expectSpace(page, 'workbench');
      assert.equal(await page.locator('.fabric-starwave-canvas').count(), 0, 'Hidden art must be unmounted, not transparent');
      assert.ok(new URL(page.url()).hash.startsWith(hash.split('?')[0]));
    }
    assert.equal(await page.getByLabel('工作 ID', {exact: true}).inputValue(), ids.completed);
    const text = await pane(page).innerText(); assert.ok(text.includes('已完成')); assert.ok(text.includes('已释放'));
    assert.equal(await pane(page).getByRole('button', {name: /下载.*结果/}).count(), 0, 'Logical fixture has no result artifact');
  });
  await check('identity switch and clear remove old objects, drafts, filters and late responses', async ({page, fixture}) => {
    await goto(page, '#run'); await expectSpace(page, 'workbench'); await openAdvanced(page);
    await page.locator('input[name=image]').fill('alice-secret-draft-marker');
    await page.getByRole('textbox', {name: '筛选当前页工作', exact: true}).fill('alice-filter-marker');
    const gate = fixture.holdNext(`/x/v1/work-intents/${ids.active}`);
    await page.goto(`${origin}/#continuity?intent=${ids.active}`, {waitUntil: 'domcontentloaded'});
    await Promise.race([gate.started, new Promise((_, reject) => setTimeout(() => reject(Error('Expected held work read')), 5000))]);
    await applyIdentity(page, 'bob'); gate.release(); await sleep(page, 400);
    const visible = await page.locator('body').innerText();
    assert.ok(!visible.includes('校园实验室多模态识别验证任务'));
    await section(page, 'run'); await openAdvanced(page);
    assert.notEqual(await page.locator('input[name=image]').inputValue(), 'alice-secret-draft-marker');
    assert.notEqual(await page.getByRole('textbox', {name: '筛选当前页工作', exact: true}).inputValue(), 'alice-filter-marker');
    await openIdentity(page); await page.getByRole('button', {name: '清除身份', exact: true}).click(); await sleep(page, 300);
    const cleared = await page.locator('body').innerText(); assert.ok(!cleared.includes('Bob 独立工作'));
    const persisted = await page.evaluate(() => ({local: JSON.stringify({...localStorage}), session: JSON.stringify({...sessionStorage}), address: location.href}));
    for (const text of Object.values(persisted)) assert.ok(!/fixture-bob|alice-secret-draft-marker|Bearer/.test(text));
    return {identityScopedDrafts: true, staleAliceReadReleasedAfterBobApplied: true, persisted};
  });
  await check('unconfirmed session fails closed without protected overview or workbench reads', async ({page, fixture}) => {
    fixture.failures.set('/x/v1/session', {status: 401, body: {error: 'unauthorized', detail: 'Synthetic expired identity'}});
    await goto(page); await expectSpace(page, 'overview', true);
    await control(page, '工作台').click(); await expectSpace(page, 'workbench', true);
    assert.equal(fixture.requests.filter(request => !['/healthz', '/v1/version', '/x/v1/session'].includes(request.path.split('?')[0])).length, 0);
    await snap(page, 'session-rejected-1440x900');
  });
  await check('compute-only session does not request contributor or administrator data', async ({page, fixture}) => {
    fixture.sessionOverrides.set('alice', {actor_id: 'fixture-alice-compute-only', owner_id: 'alice',
      capabilities: ['use_compute'], is_administrator: false});
    await goto(page); await expectSpace(page, 'overview');
    await switchSpace(page, 'workbench'); await section(page, 'resources');
    await goto(page, '#acceptance'); await expectSpace(page, 'workbench');
    const forbidden = fixture.requests.filter(request => ['/x/v1/contributions', '/x/v1/admin/overview'].includes(request.path.split('?')[0]));
    assert.deepEqual(forbidden, [], 'Authoritative session, not identity label, controls privileged reads');
    return {capabilities: ['use_compute'], forbiddenReadCount: forbidden.length};
  });
  await check('new observed epoch clears previous drafts and discards a late old snapshot', async ({page, fixture}) => {
    await goto(page, '#run'); await expectSpace(page, 'workbench'); await openAdvanced(page);
    await page.locator('input[name=image]').fill('old-epoch-draft-marker');
    const search = page.getByRole('textbox', {name: '筛选当前页工作', exact: true});
    await search.fill('queued');
    const listing = page.locator('.panel').filter({has: search});
    const gate = fixture.holdNext('/x/v1/work-intents');
    await listing.getByRole('button', {name: '刷新', exact: true}).click();
    await Promise.race([gate.started, new Promise((_, reject) => setTimeout(() => reject(Error('Expected held old epoch read')), 5000))]);
    fixture.snapshot = {version: {epoch: 1, revision: 1}, now: 0};
    await switchSpace(page, 'overview'); await sleep(page, 250);
    gate.release(); await sleep(page, 250);
    await switchSpace(page, 'workbench'); await openAdvanced(page);
    assert.notEqual(await page.locator('input[name=image]').inputValue(), 'old-epoch-draft-marker');
    assert.notEqual(await search.inputValue(), 'queued');
    return {initialEpoch: 0, newEpoch: 1, oldSnapshotReleasedAfterNewEpoch: true};
  });
  await check('empty current-owner overview does not invent work or global success', async ({page, fixture}) => {
    fixture.emptyOwners.add('alice'); await goto(page); await expectSpace(page, 'overview');
    const text = await pane(page, 'overview').innerText();
    assert.ok(/暂无|还没有|没有工作/.test(text)); assert.ok(!text.includes('全部正常'));
    assert.equal(await pane(page, 'overview').locator('a[href*="intent=fixture-"]').count(), 0);
    await snap(page, 'overview-empty-1440x900');
  });
  await check('same-identity refresh failure retains previously loaded list with failure notice', async ({page, fixture}) => {
    await goto(page, '#run'); await expectSpace(page, 'workbench');
    await page.getByRole('textbox', {name: '筛选当前页工作', exact: true}).waitFor();
    const before = await pane(page).innerText(); assert.ok(before.includes('校园实验室多模态识别验证任务'));
    fixture.failures.set('/x/v1/work-intents', {status: 503});
    const listing = page.locator('.panel').filter({has: page.getByRole('textbox', {name: '筛选当前页工作', exact: true})});
    await listing.getByRole('button', {name: '刷新', exact: true}).click(); await sleep(page, 250);
    const after = await pane(page).innerText(); assert.ok(after.includes('校园实验室多模态识别验证任务'));
    assert.ok(/更新失败|上次读取|读取失败|Synthetic unavailable/.test(after));
    await snap(page, 'work-list-stale-1440x900');
  });
  await check('art exists only in overview and reduced motion stops continuous drawing', async ({page}) => {
    await goto(page); await expectSpace(page, 'overview');
    assert.equal(await page.locator('.fabric-starwave-canvas').count(), 1);
    await page.emulateMedia({reducedMotion: 'reduce'}); await sleep(page, 350);
    const before = await frames(page); await sleep(page, 400); const after = await frames(page);
    if (!preview) {assert.ok(before && after, 'Dev frame instrumentation required'); assert.equal(before.renderedFrames, after.renderedFrames);}
    await page.locator('.account-trigger').click();
    await openDetails(page, '显示偏好');
    await page.getByRole('checkbox', {name: '页头横向手势', exact: true}).check();
    await page.getByRole('button', {name: '关闭账户菜单', exact: true}).click();
    await hoverGestureBlank(page); await page.mouse.wheel(30, 0); await sleep(page, 60);
    const reducedTransform = await page.locator('.space-body').evaluate(el => getComputedStyle(el).transform);
    assert.ok(['none', 'matrix(1, 0, 0, 1, 0, 0)'].includes(reducedTransform), `Reduced motion still translates content: ${reducedTransform}`);
    await switchSpace(page, 'workbench'); assert.equal(await page.locator('.fabric-starwave-canvas').count(), 0);
    await switchSpace(page, 'overview'); assert.equal(await page.locator('.fabric-starwave-canvas').count(), 1);
    await snap(page, 'overview-reduced-motion-1440x900');
    return {method: 'Chromium media emulation', before, after, reducedTransform, workbenchCanvasCount: 0};
  });
  await check('keyboard switching leaves focus in a visible active surface', async ({page}) => {
    await goto(page); await expectSpace(page, 'overview');
    await control(page, '工作台').focus(); await page.keyboard.press('Enter'); await expectSpace(page, 'workbench');
    const focus = await page.evaluate(() => {
      const e = document.activeElement, rect = e.getBoundingClientRect();
      return {tag: e.tagName, text: e.textContent?.slice(0, 100), hiddenAncestor: Boolean(e.closest('[hidden],[inert]')), visible: rect.width > 0 && rect.height > 0};
    });
    assert.ok(focus.visible); assert.equal(focus.hiddenAncestor, false); assert.notEqual(focus.tag, 'BODY');
    await control(page, '总览').focus(); await page.keyboard.press('Enter'); await expectSpace(page, 'overview'); return focus;
  });
  await check('optional header gesture is off by default and respects inertia and controls', async ({page}) => {
    await goto(page); await expectSpace(page, 'overview');
    const region = page.locator('[data-space-gesture-region]');
    assert.equal(await region.getAttribute('data-space-gesture-enabled'), 'false');
    await hoverGestureBlank(page); await page.mouse.wheel(60, 0); await page.mouse.wheel(60, 0); await sleep(page, 220);
    assert.equal(await page.locator('[data-space]').getAttribute('data-space'), 'overview');
    await page.locator('.account-trigger').click();
    await openDetails(page, '显示偏好');
    await page.getByRole('checkbox', {name: '页头横向手势', exact: true}).check();
    await page.getByRole('button', {name: '关闭账户菜单', exact: true}).click();
    assert.equal(await region.getAttribute('data-space-gesture-enabled'), 'true');
    const point = await hoverGestureBlank(page);
    await page.mouse.wheel(55, 0); await page.mouse.wheel(55, 0);
    await page.waitForFunction(() => document.querySelector('[data-space]')?.getAttribute('data-space') === 'workbench');
    await page.mouse.wheel(-65, 0); await page.mouse.wheel(-65, 0); await sleep(page, 220);
    assert.equal(await page.locator('[data-space]').getAttribute('data-space'), 'workbench', 'Immediate reversal is inertia, not a second navigation');
    await sleep(page, 600);
    await control(page, '总览').hover(); await page.mouse.wheel(-65, 0); await page.mouse.wheel(-65, 0); await sleep(page, 220);
    assert.equal(await page.locator('[data-space]').getAttribute('data-space'), 'workbench', 'Buttons must not swallow navigation wheel gestures');
    await sleep(page, 250); await hoverGestureBlank(page); await page.mouse.wheel(-55, 0); await page.mouse.wheel(-55, 0);
    await expectSpace(page, 'overview');
    return {input: 'Chromium mouse.wheel, not hardware trackpad', overviewToWorkbench: 'positive deltaX', emptyHitPoint: point};
  });
  await check('desktop and narrow layouts preserve visible switcher and reachable forms', async ({page, fixture}) => {
    const measurements = [], nameMeasurements = [];
    const longName = 'W'.repeat(80);
    for (const id of [ids.active, ids.stopping, ids.completed]) fixture.works.get(id).display_name = longName;
    async function measureName(selector, label, space) {
      const element = page.locator(selector).filter({hasText: longName}).first();
      await element.waitFor();
      const measurement = await element.evaluate(el => {
        const panel = el.closest('.panel'), bounds = panel.getBoundingClientRect();
        const range = document.createRange(); range.selectNodeContents(el);
        const rects = [...range.getClientRects()];
        const scroll = el.closest('[data-space-scroll]');
        return {text: el.textContent, panelWidth: bounds.width, elementWidth: el.clientWidth,
          elementOverflow: Math.max(0, el.scrollWidth - el.clientWidth),
          paneOverflow: Math.max(0, scroll.scrollWidth - scroll.clientWidth),
          textRight: Math.max(...rects.map(rect => rect.right)), panelRight: bounds.right,
          lineRects: rects.length};
      });
      nameMeasurements.push({viewport: page.viewportSize(), label, space, ...measurement});
    }
    for (const size of [{width: 1440, height: 900}, {width: 1280, height: 800}, {width: 1024, height: 768}, {width: 390, height: 844}, {width: 320, height: 700}]) {
      await page.setViewportSize(size); await goto(page); await expectSpace(page, 'overview');
      await scrollPaneToTop(page, 'overview');
      await measureName('.attention-row strong', 'attention', 'overview');
      await measureName('.overview-work-row strong', 'current', 'overview');
      await measureName('.overview-result>strong', 'result', 'overview');
      assert.equal(await control(page, '工作台').isVisible(), true);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      if ([1440, 390].includes(size.width)) await snap(page, `overview-${size.width}x${size.height}`);
      await switchSpace(page, 'workbench'); await section(page, 'run');
      await scrollPaneToTop(page, 'workbench');
      await measureName('.work-list-identity .link-button', 'list', 'workbench');
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      if ([1440, 390].includes(size.width)) await snap(page, `work-list-${size.width}x${size.height}`);
      await openNewWork(page); await page.locator('input[name=cpu]').click(); await page.locator('input[name=cpu]').fill('1500');
      await sleep(page, 300);
      const input = await page.locator('input[name=cpu]').boundingBox();
      assert.ok(input.width > 0 && input.x >= 0 && input.x + input.width <= size.width + 1);
      assert.ok(input.y >= 0 && input.y + input.height <= size.height + 1, 'Focused real form input must remain vertically visible');
      if ([1440, 390].includes(size.width)) await snap(page, `work-form-${size.width}x${size.height}`);
      measurements.push({...size, horizontalOverflow: await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)});
      await goto(page, `#continuity?intent=${ids.active}`); await expectSpace(page, 'workbench');
      await measureName('.work-detail .panel-heading h2', 'detail', 'workbench');
      await page.locator('.work-detail .panel-heading h2').filter({hasText: longName}).scrollIntoViewIfNeeded();
      if ([1440, 390, 320].includes(size.width)) await snap(page, `work-detail-long-name-${size.width}x${size.height}`);
    }
    await writeFile(resolve(out, 'long-name-measurements.json'), JSON.stringify({name: longName, measurements: nameMeasurements}, null, 2) + '\n');
    const overflow = nameMeasurements.filter(item => item.elementOverflow > 1 || item.paneOverflow > 1 || (item.label !== 'list' && item.textRight > item.panelRight + 1));
    assert.deepEqual(overflow, [], 'Long names must wrap inside their content area without horizontal pane overflow; a narrow table keeps its own scroll container');
    return {layouts: measurements, longNames: nameMeasurements};
  });
} finally {
  await browser.close(); result.browserClosed = !browser.isConnected();
  result.finished_at = new Date().toISOString(); result.status = result.checks.some(check => check.status === 'FAIL') ? 'FAIL' : 'PASS';
  await writeFile(resolve(out, 'browser-results.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(`Evidence: ${out}`); if (result.status === 'FAIL') process.exitCode = 1;
}
