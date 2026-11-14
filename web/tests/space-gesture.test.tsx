/** Synthetic input/DOM evidence only. These tests do not certify two fingers,
 * browser history interception, touchscreens, or a physical trackpad. */
import assert from 'node:assert/strict';
import {after, afterEach, beforeEach, test} from 'node:test';
import {JSDOM} from 'jsdom';
import {SpaceGestureRecognizer} from '../src/components/spaceGestureMachine.ts';

const packet = (deltaX: number, time: number, extras = {}) => ({deltaX, deltaY: 0,
  deltaMode: 0, time, eligible: true, cancelable: true, modified: false,
  space: 'overview' as const, width: 1440, ...extras});

test('a deliberate horizontal sequence commits once and long reversing inertia cannot navigate back', () => {
  const machine = new SpaceGestureRecognizer();
  assert.equal(machine.feed(packet(20, 0)).navigate, undefined);
  assert.equal(machine.feed(packet(40, 20)).navigate, undefined);
  assert.equal(machine.feed(packet(40, 40)).navigate, 'workbench');
  // Navigation changes the source prop; the same recognizer keeps its inertia lock.
  machine.cancel(41);
  for (let time = 50; time < 2100; time += 100) {
    const tail = machine.feed(packet(-60, time, {space: 'workbench'}));
    assert.equal(tail.navigate, undefined); assert.equal(tail.consume, true);
  }
  assert.equal(machine.feed(packet(-50, 2400, {space: 'workbench'})).navigate, undefined);
  assert.equal(machine.feed(packet(-50, 2420, {space: 'workbench'})).navigate, 'overview');
});

test('a short or reversed preview returns to its source without a navigation', () => {
  const short = new SpaceGestureRecognizer();
  assert.equal(short.feed(packet(30, 0)).preview, -30);
  assert.equal(short.feed(packet(30, 20)).navigate, undefined);
  assert.deepEqual(short.quiet(201), {phase: 'cooldown', preview: 0, consume: false});
  const reverse = new SpaceGestureRecognizer();
  reverse.feed(packet(50, 0));
  const canceled = reverse.feed(packet(-40, 20));
  assert.equal(canceled.preview, 0); assert.equal(canceled.navigate, undefined);
  assert.equal(reverse.feed(packet(-180, 40)).navigate, undefined);
});

test('vertical-first and invalid beginnings remain native for the complete sequence', () => {
  for (const first of [packet(2, 0, {deltaY: 24}), packet(25, 0, {deltaMode: 1}),
    packet(25, 0, {deltaMode: 2}), packet(25, 0, {modified: true}),
    packet(25, 0, {cancelable: false}), packet(25, 0, {eligible: false})]) {
    const machine = new SpaceGestureRecognizer();
    assert.equal(machine.feed(first).consume, false);
    for (const time of [20, 40, 60]) {
      const result = machine.feed(packet(100, time));
      assert.equal(result.navigate, undefined); assert.equal(result.preview, 0);
      assert.equal(result.consume, false);
    }
  }
});

test('spaces do not wrap, and one high velocity event never commits by itself', () => {
  for (const [space, direction] of [['overview', -1], ['workbench', 1]] as const) {
    const machine = new SpaceGestureRecognizer();
    assert.equal(machine.feed(packet(direction * 50, 0, {space})).consume, false);
    assert.equal(machine.feed(packet(direction * 100, 20, {space})).navigate, undefined);
  }
  const machine = new SpaceGestureRecognizer();
  const spike = machine.feed(packet(1000, 0, {width: 390}));
  assert.equal(spike.navigate, undefined);
  assert.ok(Math.abs(spike.preview) <= 390 * 0.18);
  assert.equal(machine.feed(packet(0, 20)).navigate, undefined);
  assert.equal(machine.quiet(201).preview, 0);
});

test('cooldown requires both elapsed time and quiet, not a clock deadline alone', () => {
  const machine = new SpaceGestureRecognizer();
  machine.feed(packet(50, 0)); machine.feed(packet(50, 20));
  machine.quiet(201);
  // New input before the minimum cooldown extends quiet detection.
  assert.equal(machine.feed(packet(-70, 400, {space: 'workbench'})).navigate, undefined);
  assert.equal(machine.feed(packet(-70, 560, {space: 'workbench'})).navigate, undefined);
  assert.equal(machine.feed(packet(-70, 720, {space: 'workbench'})).navigate, undefined);
  assert.equal(machine.feed(packet(-50, 1000, {space: 'workbench'})).navigate, undefined);
  assert.equal(machine.feed(packet(-50, 1020, {space: 'workbench'})).navigate, 'overview');
});

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/', pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'navigator', 'Element', 'HTMLElement', 'Event', 'MouseEvent', 'WheelEvent', 'KeyboardEvent']) {
  Object.defineProperty(globalThis, name, {value: dom.window[name], configurable: true, writable: true});
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const React = await import('react');
const {act, StrictMode} = React;
const {createRoot} = await import('react-dom/client');
const {SpaceGesture} = await import('../src/components/SpaceGesture.tsx');

let root, container, navigations, previews, networkCalls, renderKey;
const originalFetch = globalThis.fetch;
const originalAdd = dom.window.EventTarget.prototype.addEventListener;
const originalRemove = dom.window.EventTarget.prototype.removeEventListener;
const wheelListeners = new Map();
function watch(target, type) {
  return type === 'wheel' && (target === window || target instanceof Element && target.matches('.space-gesture-region'));
}
dom.window.EventTarget.prototype.addEventListener = function(type, listener, options) {
  if (watch(this, type)) {
    const set = wheelListeners.get(this) || new Set(); set.add(listener); wheelListeners.set(this, set);
    if (this === window) assert.equal(options.passive, true, 'global observation never cancels native input');
  }
  return originalAdd.call(this, type, listener, options);
};
dom.window.EventTarget.prototype.removeEventListener = function(type, listener, options) {
  if (watch(this, type)) {
    const set = wheelListeners.get(this); set?.delete(listener); if (!set?.size) wheelListeners.delete(this);
  }
  return originalRemove.call(this, type, listener, options);
};

async function render(props = {}, children = <span data-blank="true">Empty header area</span>) {
  await act(async () => root.render(<StrictMode><SpaceGesture key={renderKey} activeSpace="overview"
    onNavigate={target => navigations.push(target)} onPreview={offset => previews.push(offset)}
    {...props}>{children}</SpaceGesture></StrictMode>));
  const region = container.querySelector('[data-space-gesture-region]');
  if (region) region.getBoundingClientRect = () => ({width: 800, height: 60, top: 0, left: 0, right: 800, bottom: 60, x: 0, y: 0, toJSON() {}});
  return region;
}
async function wheel(target, extra = {}) {
  const event = new WheelEvent('wheel', {deltaX: 50, deltaY: 0, deltaMode: 0,
    bubbles: true, cancelable: true, composed: true, ...extra});
  await act(async () => target.dispatchEvent(event));
  return event;
}
beforeEach(() => {
  document.body.innerHTML = '<div id="test-root"></div>';
  container = document.getElementById('test-root'); root = createRoot(container);
  navigations = []; previews = []; networkCalls = 0; renderKey = 0;
  globalThis.fetch = async () => {networkCalls++; throw new Error('gesture must not issue a request');};
});
afterEach(async () => {
  await act(async () => root.unmount());
  document.getSelection()?.removeAllRanges();
  globalThis.fetch = originalFetch;
  assert.equal(networkCalls, 0); assert.equal(wheelListeners.size, 0, 'both local and passive observers cleaned up');
});
after(() => {
  dom.window.EventTarget.prototype.addEventListener = originalAdd;
  dom.window.EventTarget.prototype.removeEventListener = originalRemove;
  dom.window.close();
});

test('the optional gesture is off by default and never changes the URL itself', async () => {
  const region = await render();
  assert.equal((await wheel(region)).defaultPrevented, false);
  await wheel(region); assert.deepEqual(navigations, []); assert.equal(wheelListeners.size, 0);
  await render({enabled: true});
  assert.equal((await wheel(region)).defaultPrevented, true);
  await wheel(region);
  assert.deepEqual(navigations, ['workbench']);
  assert.equal(locationHash(), ''); assert.equal(window.history.length, 1);
});
function locationHash() {return window.location.hash;}

test('form controls, semantic content and nested scrollers retain native wheel at their boundary', async () => {
  const contents = [<input/>, <button>Action</button>, <a href="#work">Task</a>,
    <label>Field label</label>, <pre>Log</pre>, <table><tbody><tr><td>Cell</td></tr></tbody></table>,
    <div contentEditable suppressContentEditableWarning>Draft</div>,
    <div style={{overflowX: 'auto'}}>No overflow yet</div>, <div data-timeline="true">Timeline</div>,
    <div data-space-gesture-ignore="true">Custom chart</div>, <span>Selectable heading text</span>,
    <div tabIndex={0}>Custom control</div>];
  for (const child of contents) {
    renderKey++;
    const region = await render({enabled: true}, child);
    const target = region.firstElementChild;
    assert.equal((await wheel(target)).defaultPrevented, false);
    await wheel(region); await wheel(region);
    assert.deepEqual(navigations, [], 'moving to a blank area does not steal the same input sequence');
  }
});

test('focused editing, selected text, overlays and composition suspend space switching', async () => {
  for (const condition of ['focus', 'selection', 'overlay', 'composition']) {
    renderKey++;
    const region = await render({enabled: true}, <><input/><span>Selectable text</span></>);
    let overlay;
    if (condition === 'focus') region.querySelector('input').focus();
    if (condition === 'selection') {
      const range = document.createRange(); range.selectNodeContents(region.querySelector('span'));
      document.getSelection().addRange(range);
    }
    if (condition === 'overlay') {
      overlay = document.createElement('div'); overlay.setAttribute('aria-modal', 'true'); document.body.append(overlay);
    }
    if (condition === 'composition') document.dispatchEvent(new Event('compositionstart', {bubbles: true}));
    assert.equal((await wheel(region)).defaultPrevented, false);
    await wheel(region); assert.deepEqual(navigations, []);
    region.querySelector('input').blur(); document.getSelection().removeAllRanges(); overlay?.remove();
  }
});

test('outside, vertical, modifier and noncancelable input cannot upgrade into an app gesture', async () => {
  for (const first of ['outside', 'vertical', 'modifier', 'line', 'uncancelable']) {
    renderKey++; const region = await render({enabled: true});
    const event = await wheel(first === 'outside' ? document.body : region,
      first === 'vertical' ? {deltaX: 1, deltaY: 50} :
      first === 'modifier' ? {ctrlKey: true} : first === 'line' ? {deltaMode: 1} :
      first === 'uncancelable' ? {cancelable: false} : {});
    assert.equal(event.defaultPrevented, false);
    assert.equal((await wheel(region)).defaultPrevented, false);
    await wheel(region); assert.deepEqual(navigations, []);
  }
});

test('Escape, context changes, native scrolling, blur and resize cancel an unfinished preview', async () => {
  for (const reason of ['escape', 'scope', 'blocked', 'scroll', 'blur', 'resize']) {
    renderKey++; const region = await render({enabled: true, scopeKey: 'identity-1'});
    await wheel(region, {deltaX: 35}); assert.ok(previews.at(-1) < 0);
    if (reason === 'scope') await render({enabled: true, scopeKey: 'identity-2'});
    else if (reason === 'blocked') await render({enabled: true, blocked: true, scopeKey: 'identity-1'});
    else await act(async () => {
      if (reason === 'escape') document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true, cancelable: true}));
      else window.dispatchEvent(new Event(reason));
    });
    assert.equal(previews.at(-1), 0); assert.deepEqual(navigations, []);
    await wheel(region, {deltaX: 100}); assert.deepEqual(navigations, []);
  }
});

test('space rerenders retain inertia suppression and disabling removes interception', async () => {
  const region = await render({enabled: true});
  await wheel(region); await wheel(region);
  assert.deepEqual(navigations, ['workbench']);
  await render({enabled: true, activeSpace: 'workbench'});
  for (let i = 0; i < 4; i++) await wheel(region, {deltaX: -100});
  assert.deepEqual(navigations, ['workbench']);
  await render({enabled: false, activeSpace: 'workbench'});
  assert.equal((await wheel(region, {deltaX: -100})).defaultPrevented, false);
  assert.equal(wheelListeners.size, 0); assert.equal(previews.at(-1), 0);
});

test('a quiet short preview resets without navigation and unmount does not leave timers active', async () => {
  const region = await render({enabled: true});
  await wheel(region, {deltaX: 30});
  await act(async () => {await new Promise(resolve => setTimeout(resolve, 200));});
  assert.equal(previews.at(-1), 0); assert.deepEqual(navigations, []);
  await act(async () => root.render(null));
  const observed = previews.length;
  await act(async () => {await new Promise(resolve => setTimeout(resolve, 200));});
  assert.equal(previews.length, observed); assert.equal(wheelListeners.size, 0);
});

test('loss of cancellation support during tracking cancels the preview and relinquishes input', async () => {
  const region = await render({enabled: true});
  await wheel(region, {deltaX: 35});
  assert.equal(previews.at(-1), -35);
  const event = await wheel(region, {deltaX: 100, cancelable: false});
  assert.equal(event.defaultPrevented, false);
  assert.equal(previews.at(-1), 0); assert.deepEqual(navigations, []);
  assert.equal((await wheel(region, {deltaX: 100})).defaultPrevented, false);
  assert.deepEqual(navigations, []);
});

test('an embedded host that ignores cancellation never receives a navigation or preview', async () => {
  const region = await render({enabled: true});
  for (let i = 0; i < 3; i++) {
    const event = new WheelEvent('wheel', {deltaX: 100, bubbles: true, composed: true, cancelable: true});
    event.preventDefault = () => {};
    await act(async () => region.dispatchEvent(event));
  }
  assert.deepEqual(navigations, []); assert.deepEqual(previews, []);
});
