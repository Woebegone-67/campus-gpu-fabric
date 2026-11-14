/** Read-only browser check against a local, explicitly pinned historical CP3 archive.
 * JOINT_URL, JOINT_IDENTITIES, JOINT_EVIDENCE required. Does not run experiments.
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
const identityPath = resolve(root, process.env.JOINT_IDENTITIES);
const identities = JSON.parse(await readFile(identityPath, 'utf8'));
const out = resolve(root, process.env.JOINT_EVIDENCE); await mkdir(out);
const {chromium} = await import(pathToFileURL(resolve(root, 'runtime/starwave-browser/node_modules/playwright-core/index.mjs')));
const browser = await chromium.launch({headless: true, executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
const page = await browser.newPage({viewport: {width: 1440, height: 1000}, acceptDownloads: true});
page.setDefaultTimeout(15000);
const record = {base, started_at: new Date().toISOString(), browser: browser.version(),
  commit: execFileSync('git', ['rev-parse', 'HEAD'], {cwd: root, encoding: 'utf8'}).trim(),
  scope: 'Read existing pinned historical report and browser download. No new strategy run or current-build effect claim.',
  requests: [], page_errors: []};
page.on('request', r => {if (/^\/(v1|x\/v1)\//.test(new URL(r.url()).pathname)) record.requests.push({method: r.method(), path: new URL(r.url()).pathname});});
page.on('pageerror', e => record.page_errors.push(e.message));
try {
  await page.goto(base);
  await page.locator('button.account-trigger').click();
  await page.getByLabel('导入本地身份文件', {exact: true}).setInputFiles(identityPath);
  const account = page.waitForResponse(r => new URL(r.url()).pathname === '/x/v1/session' && r.status() === 200);
  await page.locator('.identity-panel select').selectOption('admin'); await account;
  await page.getByRole('button', {name: '关闭账户面板', exact: true}).click();
  await page.getByRole('button', {name: '工作台', exact: true}).click();
  const reportResponse = page.waitForResponse(r => new URL(r.url()).pathname === '/x/v1/policy-validation');
  await page.getByRole('link', {name: '实验与报告', exact: true}).click();
  const response = await reportResponse; assert.equal(response.status(), 200);
  const report = await response.json();
  assert.equal(report.historical, true);
  assert.equal(report.current_build_acceptance_eligible, false);
  const panel = page.locator('.panel').filter({has: page.getByRole('heading', {name: 'CP3 策略配对验证', exact: true})});
  await panel.getByText(/历史实验报告/).waitFor();
  const text = await panel.innerText();
  assert.ok(text.includes('不能作为当前构建验收证据'));
  assert.ok(text.includes(Number(report.analysis.comparisons.B2.aggregate.macro_area_ratio).toFixed(4)));
  const artifactId = report.artifact_ids.find(id => id.split(':').at(-1) === 'analysis');
  assert.ok(artifactId);
  const pendingDownload = page.waitForEvent('download');
  await panel.getByRole('button', {name: '下载配对分析报告', exact: true}).click();
  const download = await pendingDownload; const path = resolve(out, download.suggestedFilename()); await download.saveAs(path);
  const originalResponse = await fetch(`${base}/x/v1/policy-validation/artifacts/${encodeURIComponent(artifactId)}`, {headers: identities.admin});
  assert.equal(originalResponse.status, 200);
  const raw = Buffer.from(await originalResponse.arrayBuffer()), downloaded = await readFile(path);
  assert.deepEqual(downloaded, raw);
  assert.ok(record.requests.every(r => r.method === 'GET'));
  assert.deepEqual(record.page_errors, []);
  await panel.scrollIntoViewIfNeeded(); await page.screenshot({path: resolve(out, 'historical-report.png')});
  record.status = 'PASS'; record.report = {historical: report.historical, eligible: report.current_build_acceptance_eligible,
    macro_area_ratio: report.analysis.comparisons.B2.aggregate.macro_area_ratio, manifest_sha256: report.manifest_sha256,
    runs: report.runs.length, artifact_id: artifactId, bytes: raw.length, download_sha256: createHash('sha256').update(raw).digest('hex')};
  console.log('PASS historical report, source boundary and exact browser download');
} catch (e) {
  record.status = 'FAIL'; record.failure = e.stack; console.error(e.message); process.exitCode = 1;
  await page.screenshot({path: resolve(out, 'failure.png')}).catch(() => {});
} finally {
  record.finished_at = new Date().toISOString(); await writeFile(resolve(out, 'receipt.json'), JSON.stringify(record, null, 2)); await browser.close();
}
