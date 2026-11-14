/** Real local CPU integration. Requires a NEW isolated cp2_joint_session runtime.
 * JOINT_URL, JOINT_IDENTITIES, JOINT_EVIDENCE are required; no API interception.
 * Business mutations use the UI, except the explicitly recorded Bob denial probe.
 * Does not certify CP4 performance, human acceptance, or native trackpad behavior.
 */
import assert from 'node:assert/strict';
import {readFile, mkdir, writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {execFileSync} from 'node:child_process';

const root = resolve(import.meta.dirname, '../..');
const base = process.env.JOINT_URL;
assert.ok(base && ['127.0.0.1', 'localhost'].includes(new URL(base).hostname));
assert.ok(process.env.JOINT_IDENTITIES && process.env.JOINT_EVIDENCE);
const identitiesPath = resolve(root, process.env.JOINT_IDENTITIES);
const identities = JSON.parse(await readFile(identitiesPath, 'utf8'));
const previous = process.env.JOINT_RESUME_RECEIPT ? JSON.parse(await readFile(resolve(root, process.env.JOINT_RESUME_RECEIPT), 'utf8')) : null;
const out = resolve(root, process.env.JOINT_EVIDENCE);
await mkdir(out); // Refuse to overwrite earlier evidence.
const result = {started_at: new Date().toISOString(), base,
  commit: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: root, encoding: 'utf8'}).trim(),
  dirty: execFileSync('git', ['status', '--porcelain', '--untracked-files=no'], {cwd: root, encoding: 'utf8'}).trim(),
  scope: 'Live local CPU reference execution, real Chrome UI and downloads; GPU inventory is simulated.',
  resumed_from: process.env.JOINT_RESUME_RECEIPT || null, checks: [], responses: [], errors: [], snapshots: [], unverified: [
    'Independent human deployment and A/B cross-operation', 'Physical trackpad, Safari, VoiceOver',
    'Formal CP4 load and performance targets', 'Configured CP3 full-report rendering']};
const {chromium} = await import(pathToFileURL(resolve(root, 'runtime/starwave-browser/node_modules/playwright-core/index.mjs')));
const browser = await chromium.launch({headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
result.browser = browser.version();
const control = (page, name) => page.getByRole('button', {name, exact: true});
const pane = page => page.locator('[data-space-scroll="workbench"]');
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
async function save() {await writeFile(resolve(out, 'receipt.json'), JSON.stringify(result, null, 2));}
async function pass(name, details = {}) {result.checks.push({name, status: 'PASS', details}); console.log(`PASS ${name}`); await save();}
async function read(actor, path) {
  const response = await fetch(base + path, {headers: identities[actor]});
  const raw = Buffer.from(await response.arrayBuffer());
  return {status: response.status, raw, data: JSON.parse(raw.toString())};
}
async function until(actor, id, predicate, timeout = 100000) {
  const deadline = Date.now() + timeout;
  let detail;
  while (Date.now() < deadline) {
    const response = await read(actor, `/x/v1/work-intents/${id}`);
    assert.equal(response.status, 200); detail = response.data;
    if (predicate(detail)) return detail;
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  throw new Error(`State deadline: ${JSON.stringify({id, state: detail?.item?.state, allocations: detail?.allocations?.map(a => a.state)})}`);
}
async function snapshot(page, name, detail) {
  await page.screenshot({path: resolve(out, `${name}.png`)});
  if (detail) await writeFile(resolve(out, `${name}.json`), JSON.stringify(detail, null, 2));
  result.snapshots.push(name); await save();
}
async function login(page, actor, fromFile = false) {
  await page.goto(base);
  const session = page.waitForResponse(r => new URL(r.url()).pathname === '/x/v1/session' && r.status() === 200);
  if (fromFile) {
    await openDetails(page, '从身份文件连接');
    await page.getByLabel('导入本地身份文件', {exact: true}).setInputFiles(identitiesPath);
    await page.locator('.identity-panel select').selectOption(actor);
  } else {
    await page.getByLabel('账户', {exact: true}).fill(identities[actor]['X-Owner'] || '');
    await page.getByLabel('访问凭证', {exact: true}).fill(identities[actor].Authorization.replace(/^Bearer /, ''));
    await page.locator('.identity-manual').getByRole('button', {name: '连接账户', exact: true}).click();
  }
  const account = await (await session).json();
  await page.locator('[data-space-scroll="overview"]').waitFor({state: 'visible'});
  return account;
}
async function newPage(actor) {
  const context = await browser.newContext({viewport: {width: 1440, height: 1000}, acceptDownloads: true});
  const page = await context.newPage(); page.setDefaultTimeout(12000);
  page.on('dialog', dialog => dialog.accept());
  page.on('pageerror', error => result.errors.push(error.message));
  page.on('response', response => {
    const path = new URL(response.url()).pathname;
    if (/^\/(v1|x\/v1)\//.test(path)) result.responses.push({actor, method: response.request().method(), path, status: response.status()});
  });
  const account = await login(page, actor);
  return {page, account};
}
async function section(page, id) {
  await control(page, '工作台').click();
  await page.locator(`a[href="#${id}"]:visible`).first().click();
  await page.waitForURL(url => url.hash === `#${id}`);
}
async function openDetails(page, text) {
  const summary = page.locator('summary').filter({hasText: text}).first();
  if (!await summary.evaluate(el => el.parentElement.open)) await summary.click();
}
async function createWork(page, {checkpoint = false, timeout, model = 'A10', name = '联合验收工作'} = {}) {
  await section(page, 'run');
  if (await control(page, '准备另一项工作').isVisible()) await control(page, '准备另一项工作').click();
  await control(page, '新建工作').click();
  await page.getByLabel('工作名称', {exact: true}).fill(name);
  await page.locator('input[name=model]').fill(model);
  await page.locator('select[name=preemption]').selectOption(checkpoint ? 'checkpoint' : 'best_effort');
  await page.locator('select[name=settlement]').selectOption('release_on_terminal');
  if (timeout) {await openDetails(page, '高级设置'); await page.locator('input[name=timeout]').fill(String(timeout));}
  const submitted = page.waitForResponse(r => new URL(r.url()).pathname === '/x/v1/work-intents' && r.request().method() === 'POST' && r.status() === 202);
  await control(page, '提交工作').click();
  const response = await submitted;
  const registration = response.request().postDataJSON();
  assert.equal(registration.display_name, name.trim());
  await page.waitForURL(url => url.hash.includes(registration.intent_id));
  return registration.intent_id;
}
async function refresh(page, id) {
  const loaded = page.waitForResponse(r => new URL(r.url()).pathname === `/x/v1/work-intents/${id}` && r.request().method() === 'GET');
  await control(page, '刷新').first().click(); await loaded;
  await page.waitForTimeout(100);
}
try {
  const version = await read('alice', '/v1/version');
  assert.equal(version.data.execution_mode, 'cpu_reference');
  const initial = await read('admin', '/x/v1/work-intents?limit=25');
  if (!previous) assert.equal(initial.data.items.length, 0, 'Use an empty isolated instance, never a human trial instance');
  else {assert.equal(previous.base, base); assert.ok(previous.recovery_work); assert.ok(previous.checks.some(check => check.name === 'real CPU recall, verified checkpoint, pending resume and applied generation 2' && check.status === 'PASS'));}
  const {page: alice, account} = await newPage('alice');
  const {page: supplier, account: supplierAccount} = await newPage('supplier');
  await pass('primary account connection accepted by real session endpoint', {account, supplierAccount});
  await snapshot(alice, 'overview-empty');
  const workName = '联合验收 · 中断后继续';
  let id = previous?.recovery_work;
  if (!previous) {
  id = await createWork(alice, {checkpoint: true, name: workName}); result.recovery_work = id;
  const first = await until('alice', id, d => d.item.state === 'running' && d.item.attempts[0]?.started_at != null);
  assert.equal(first.item.display_name, workName);
  await alice.getByRole('heading', {name: workName, exact: true}).waitFor();
  await refresh(alice, id); await snapshot(alice, 'running', first);
  await section(supplier, 'resources');
  await openDetails(supplier, '收回设备');
  for (const gpu of ['sim-a10-gpu0', 'sim-a10-gpu1']) await supplier.getByRole('checkbox', {name: new RegExp(gpu)}).check();
  await supplier.locator('input[name=notice]').fill('3');
  await supplier.locator('input[name=duration]').fill('15');
  const recalled = supplier.waitForResponse(r => new URL(r.url()).pathname === '/x/v1/contributions/recall' && r.request().method() === 'POST');
  await control(supplier, '发送收回通知').click(); assert.equal((await recalled).status(), 201);
  const stopped = await until('alice', id, d => d.allocations[0].state === 'Preempted');
  assert.equal(stopped.item.attempts[0].checkpoint.verification_status, 'verified');
  await refresh(alice, id); await snapshot(alice, 'checkpoint-verified', stopped);
  await control(alice, '恢复这项工作').click();
  const pending = await until('alice', id, d => d.allocations[0].state === 'Pending', 10000);
  assert.equal(pending.item.attempts.length, 1);
  await refresh(alice, id); await snapshot(alice, 'resume-waiting', pending);
  const resumed = await until('alice', id, d => d.item.attempts.length === 2 && d.item.attempts[1].checkpoint?.continuation_status === 'applied');
  assert.deepEqual(resumed.item.job_ids, first.item.job_ids);
  assert.deepEqual(resumed.item.allocation_ids, first.item.allocation_ids);
  assert.equal(resumed.allocations[0].expires_at, first.allocations[0].expires_at);
  assert.equal(resumed.item.display_name, workName);
  await refresh(alice, id); await snapshot(alice, 'resumed', resumed);
  await pass('real CPU recall, verified checkpoint, pending resume and applied generation 2', {id});
  } else {
    result.recovery_work = id;
    await alice.evaluate(value => {location.hash = value;}, `#continuity?intent=${id}`);
    await alice.getByRole('heading', {name: workName, exact: true}).waitFor();
  }
  const completed = await until('alice', id, d => d.item.state === 'completed' && d.allocations[0].state === 'Released');
  await refresh(alice, id);
  await control(alice, '查看结果 1').click();
  await alice.getByLabel('查看结果 1', {exact: true}).waitFor();
  const downloadPromise = alice.waitForEvent('download');
  await control(alice, '下载结果 1').click(); const download = await downloadPromise;
  const downloadedPath = resolve(out, download.suggestedFilename()); await download.saveAs(downloadedPath);
  const downloaded = await readFile(downloadedPath);
  const uri = completed.jobs[0].result_uri;
  const original = await read('alice', `/x/v1/artifacts?uri=${encodeURIComponent(uri)}`);
  assert.equal(original.status, 200); assert.deepEqual(downloaded, original.raw);
  // Python's integer JSON decoder avoids rounding the result uint64 in JavaScript.
  const oracle = execFileSync('uv', ['run', 'python', '-c',
    'import json,runpy,sys; oracle=runpy.run_path("tests/acceptance/cp3/cp3_cpu_equivalence.py")["oracle"]; x=json.load(open(sys.argv[1])); assert x["state"]==oracle(17,1000000); print("independent 3x3 matrix oracle matched")', downloadedPath], {cwd: root, encoding: 'utf8'}).trim();
  await snapshot(alice, 'completed-released', completed);
  await pass('browser result preview/download preserves bytes and independent integer result', {id, sha256: digest(downloaded), bytes: downloaded.length, oracle});
  await pane(alice).getByRole('link', {name: /^分配说明/}).first().click();
  await alice.waitForURL(url => url.hash.startsWith('#decisions?'));
  await pane(alice).locator('.event-context a').filter({hasText: workName}).waitFor();
  assert.ok(!(await pane(alice).innerText()).includes('请求未完成'));
  await snapshot(alice, 'related-decision');
  await login(alice, 'alice');
  await alice.goto(`${base}/#continuity?intent=${id}`);
  // A full navigation discards credentials by design. Reimport without losing the deep link.
  await login(alice, 'alice', true);
  await alice.evaluate(value => {location.hash = value;}, `#continuity?intent=${id}`);
  await control(alice, '下载结果 1').waitFor();
  await alice.getByRole('heading', {name: workName, exact: true}).waitFor();
  await pass('named work survives recovery, decision navigation and reimport after refresh', {id, display_name: workName});
  const {page: bob} = await newPage('bob');
  const forbidden = bob.waitForResponse(r => new URL(r.url()).pathname === `/x/v1/work-intents/${id}`);
  await bob.evaluate(value => {location.hash = value;}, `#continuity?intent=${id}`);
  assert.equal((await forbidden).status(), 404);
  assert.equal((await read('bob', `/x/v1/artifacts?uri=${encodeURIComponent(uri)}`)).status, 404);
  const recallDenied = await fetch(base + '/x/v1/contributions/recall', {method: 'POST', headers: {...identities.bob, 'Content-Type': 'application/json'}, body: JSON.stringify({gpu_ids: ['sim-a10-gpu0'], notice_seconds: 3, duration_seconds: 15})});
  assert.equal(recallDenied.status, 403);
  await snapshot(bob, 'bob-foreign-work');
  await pass('Bob denied foreign work in UI, foreign download and recall over HTTP', {work_status: 404, artifact_status: 404, recall_status: 403});
  const failedId = process.env.JOINT_FAILED_WORK || await createWork(alice, {timeout: 2, name: '联合验收 · 超时失败'});
  result.failed_work = failedId;
  if (process.env.JOINT_FAILED_WORK) {
    assert.ok(previous, 'Existing failed work requires explicit prior-session evidence');
    await alice.evaluate(value => {location.hash = value;}, `#continuity?intent=${failedId}`);
    await alice.getByRole('heading', {name: '联合验收 · 超时失败', exact: true}).waitFor();
  }
  const failed = await until('alice', failedId, d => d.item.state === 'failed' && d.allocations[0].state === 'Released');
  assert.equal(failed.item.display_name, '联合验收 · 超时失败');
  assert.equal(failed.item.specification.job.timeout_seconds, 2);
  await refresh(alice, failedId); assert.ok((await pane(alice).innerText()).includes('运行超时'));
  assert.equal(await control(alice, '下载结果 1').count(), 0);
  await snapshot(alice, 'w2-timeout-failed-released', failed);
  await pass('W2 actual CPU timeout fails and releases without a success artifact', {id: failedId, result: failed.item.result});
  const supplierId = await createWork(supplier, {timeout: 2, name: '贡献者也能提交工作'});
  await until('supplier', supplierId, d => d.item.state === 'failed' && d.allocations[0].state === 'Released');
  await pass('same supplier account both recalls owned supply and submits compute', {id: supplierId});
  const {page: admin} = await newPage('admin'); await section(admin, 'acceptance');
  const overviewPanel = admin.locator('.panel').filter({has: admin.getByRole('heading', {name: '系统状态', exact: true})});
  for (let i = 0; i < 10; i++) {
    const response = admin.waitForResponse(r => new URL(r.url()).pathname === '/x/v1/admin/overview');
    await overviewPanel.getByRole('button', {name: '刷新', exact: true}).click(); assert.equal((await response).status(), 200);
  }
  await pass('admin overview ten UI refreshes without 409');
  assert.deepEqual(result.errors, []);
  await pass('no browser page errors during live flow');
  result.status = 'PASS';
} catch (error) {
  result.status = 'FAIL'; result.failure = error.stack; console.error(error.message); process.exitCode = 1;
  let index = 0;
  for (const context of browser.contexts()) for (const page of context.pages()) {
    try {await snapshot(page, `failure-${++index}`);} catch { /* Preserve the original failure. */ }
  }
} finally {
  result.finished_at = new Date().toISOString(); await save(); await browser.close();
}
