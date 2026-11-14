/** Lifecycle integration against the real frozen engine, with controlled DOM,
 * Canvas2D, animation frames and media queries. This is not visual/browser evidence.
 * No API client, live service or business mutation is involved. */
import assert from 'node:assert/strict';
import {after, afterEach, beforeEach, test} from 'node:test';
import {JSDOM} from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/', pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'navigator', 'HTMLElement', 'HTMLCanvasElement', 'Event', 'MouseEvent']) {
  Object.defineProperty(globalThis, name, {value: dom.window[name], configurable: true, writable: true});
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.getComputedStyle = dom.window.getComputedStyle.bind(dom.window);
const React = await import('react');
const {act, StrictMode} = React;
const {createRoot} = await import('react-dom/client');
const {StarwaveSurface, StarwaveMotionControl} = await import('../src/visual/starwave/StarwaveSurface.tsx');
const {mountStarwave} = await import('../src/visual/starwave/create-starwave.mjs');

let root, container, noCanvas, reduced, legacyMedia, hidden, frameId, drawCount, observersCreated;
const frames = new Map();
const mediaListeners = new Set();
const observed = new Set();
const engineListeners = new Map();
const originalAdd = dom.window.EventTarget.prototype.addEventListener;
const originalRemove = dom.window.EventTarget.prototype.removeEventListener;
const originalCanvas = dom.window.HTMLCanvasElement.prototype.getContext;
const originalBounds = dom.window.HTMLElement.prototype.getBoundingClientRect;
const originalWarn = console.warn;

function engineEvent(target, type) {
  return (target === document && type === 'visibilitychange') ||
    (target instanceof dom.window.HTMLElement && target.tagName === 'MAIN' &&
      ['click', 'pointerdown', 'pointermove', 'pointercancel'].includes(type));
}
dom.window.EventTarget.prototype.addEventListener = function(type, listener, options) {
  if (engineEvent(this, type)) {
    const byType = engineListeners.get(this) || new Map();
    const listeners = byType.get(type) || new Set();
    listeners.add(listener); byType.set(type, listeners); engineListeners.set(this, byType);
  }
  return originalAdd.call(this, type, listener, options);
};
dom.window.EventTarget.prototype.removeEventListener = function(type, listener, options) {
  if (engineEvent(this, type)) {
    const byType = engineListeners.get(this);
    const listeners = byType?.get(type);
    listeners?.delete(listener);
    if (!listeners?.size) byType?.delete(type);
    if (!byType?.size) engineListeners.delete(this);
  }
  return originalRemove.call(this, type, listener, options);
};
dom.window.HTMLElement.prototype.getBoundingClientRect = function() {
  return {x: 0, y: 0, left: 0, top: 0, width: 640, height: 360, right: 640, bottom: 360, toJSON() {return this;}};
};
dom.window.HTMLCanvasElement.prototype.getContext = function(type) {
  if (noCanvas || type !== '2d') return null;
  return new Proxy({
    createRadialGradient: () => ({addColorStop() {}}),
    clearRect() {drawCount++;},
  }, {get: (target, key) => key in target ? target[key] : () => {}});
};
globalThis.requestAnimationFrame = callback => {const id = ++frameId; frames.set(id, callback); return id;};
globalThis.cancelAnimationFrame = id => frames.delete(id);
globalThis.ResizeObserver = class {
  constructor() {observersCreated++;}
  observe() {observed.add(this);}
  disconnect() {observed.delete(this);}
};
Object.defineProperty(document, 'hidden', {get: () => hidden, configurable: true});
Object.defineProperty(dom.window, 'devicePixelRatio', {value: 2, configurable: true});
globalThis.matchMedia = () => ({
  get matches() {return reduced;},
  media: '(prefers-reduced-motion: reduce)',
  ...(legacyMedia ? {
    addListener: listener => mediaListeners.add(listener),
    removeListener: listener => mediaListeners.delete(listener),
  } : {
    addEventListener: (type, listener) => {if (type === 'change') mediaListeners.add(listener);},
    removeEventListener: (type, listener) => {if (type === 'change') mediaListeners.delete(listener);},
  }),
});

function tick(time) {
  const pending = [...frames]; frames.clear();
  for (const [, callback] of pending) callback(time);
}
function changeMotion(value) {
  reduced = value;
  for (const listener of [...mediaListeners]) listener({matches: value});
}
function assertReleased() {
  assert.equal(document.querySelectorAll('.fabric-starwave-canvas').length, 0, 'decorative canvas is removed');
  assert.equal(frames.size, 0, 'no animation callback remains scheduled');
  assert.equal(observed.size, 0, 'resize observers are disconnected');
  assert.equal(engineListeners.size, 0, 'host and document listeners are removed');
  assert.equal(mediaListeners.size, 0, 'engine and wrapper media listeners are removed');
}
async function render(children) {await act(async () => root.render(children));}
function surface(enabled = true, label = 'Business content') {
  return <StrictMode><StarwaveSurface enabled={enabled}>
    <StarwaveMotionControl/><label>{label}<input defaultValue="preserved input"/></label>
  </StarwaveSurface></StrictMode>;
}

beforeEach(() => {
  noCanvas = false; reduced = false; legacyMedia = false; hidden = false;
  frameId = 0; drawCount = 0; observersCreated = 0;
  frames.clear(); observed.clear(); engineListeners.clear(); mediaListeners.clear();
  document.body.innerHTML = '<div id="test-root"></div>';
  container = document.getElementById('test-root'); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount());
  console.warn = originalWarn;
  assertReleased();
});
after(() => {
  dom.window.EventTarget.prototype.addEventListener = originalAdd;
  dom.window.EventTarget.prototype.removeEventListener = originalRemove;
  dom.window.HTMLCanvasElement.prototype.getContext = originalCanvas;
  dom.window.HTMLElement.prototype.getBoundingClientRect = originalBounds;
  dom.window.close();
});

test('StrictMode replay, rerender and unmount/remount retain one active scene without leaked resources', async () => {
  await render(surface());
  const canvas = container.querySelector('.fabric-starwave-canvas');
  assert.ok(canvas);
  assert.equal(canvas.getAttribute('aria-hidden'), 'true');
  assert.equal(canvas.style.pointerEvents, 'none');
  assert.equal(container.querySelectorAll('canvas').length, 1);
  assert.ok(observersCreated >= 2, 'StrictMode setup/cleanup replay actually occurred');
  assert.equal(observed.size, 1); assert.equal(frames.size, 1);
  const input = container.querySelector('input'); input.value = 'user-edited content';
  await render(surface(true, 'Updated business label'));
  assert.equal(container.querySelector('canvas'), canvas, 'business rerender preserves visual instance');
  assert.equal(container.querySelector('input').value, 'user-edited content');
  await render(null); assertReleased();
  await render(surface());
  assert.notEqual(container.querySelector('canvas'), canvas, 'remount constructs a fresh scene');
  assert.equal(container.querySelectorAll('canvas').length, 1);
  assert.equal(frames.size, 1); assert.equal(observed.size, 1);
});

test('disabling the visual shell releases the scene and leaves business controls operable', async () => {
  await render(surface());
  const retainedInput = container.querySelector('input');
  retainedInput.value = 'draft before switching space';
  await render(surface(false));
  assertReleased();
  assert.ok(container.querySelector('.sw-stage-static'));
  assert.ok(container.querySelector('.sw-content'));
  const input = container.querySelector('input');
  assert.equal(input, retainedInput, 'stable visual wrapper preserves the business subtree');
  assert.equal(input.value, 'draft before switching space');
  input.value = 'classic mode input'; input.focus();
  assert.equal(document.activeElement, input);
  assert.equal(input.value, 'classic mode input');
  await render(surface(true));
  assert.equal(container.querySelectorAll('canvas').length, 1);
  assert.equal(frames.size, 1);
});

test('legacy media listeners synchronize static preference, local pause and cleanup', async () => {
  legacyMedia = true;
  await render(surface());
  const control = () => container.querySelector('button');
  assert.equal(control().disabled, false);
  tick(0); tick(33);
  await act(async () => changeMotion(true));
  assert.equal(control().disabled, true);
  assert.match(control().textContent, /减少动态/);
  assert.equal(control().getAttribute('aria-pressed'), 'true');
  const staticDraws = drawCount; tick(6000);
  assert.equal(frames.size, 0); assert.equal(drawCount, staticDraws);
  await act(async () => changeMotion(false));
  assert.equal(control().disabled, false);
  assert.match(control().textContent, /暂停背景动效/);
  assert.equal(frames.size, 1);
  await act(async () => control().click());
  assert.match(control().textContent, /恢复背景动效/);
  const pausedDraws = drawCount; tick(9000);
  assert.equal(drawCount, pausedDraws); assert.equal(frames.size, 0);
  await act(async () => control().click());
  assert.equal(frames.size, 1);
  await render(null); assertReleased();
});

test('controlled document visibility suspends and resumes the active animation without duplicate callbacks', async () => {
  await render(surface());
  tick(0); tick(33);
  hidden = true; document.dispatchEvent(new Event('visibilitychange'));
  const before = drawCount; tick(90000);
  assert.equal(frames.size, 0); assert.equal(drawCount, before);
  hidden = false; document.dispatchEvent(new Event('visibilitychange'));
  assert.equal(frames.size, 1);
  document.dispatchEvent(new Event('visibilitychange'));
  assert.equal(frames.size, 1, 'repeated visible notification cannot duplicate RAF');
  tick(90033); assert.ok(drawCount > before);
});

test('Canvas2D-unavailable fallback retains business children and a disabled static motion control', async () => {
  noCanvas = true;
  const warnings = []; console.warn = (...args) => warnings.push(args);
  let clicks = 0;
  await render(<StrictMode><StarwaveSurface enabled>
    <StarwaveMotionControl/><button onClick={() => clicks++}>Existing business control</button>
  </StarwaveSurface></StrictMode>);
  assert.ok(container.querySelector('main.sw-stage'), 'CSS black-background host survives');
  assert.equal(container.querySelectorAll('canvas').length, 0);
  assert.ok(warnings.length > 0);
  assert.ok(container.querySelector('button').disabled);
  assert.equal(container.querySelector('button').textContent, '背景静态');
  const business = [...container.querySelectorAll('button')].find(button => button.textContent === 'Existing business control');
  await act(async () => business.click()); assert.equal(clicks, 1);
  assert.equal(frames.size, 0); assert.equal(observed.size, 0);
  await render(null); assertReleased();
  // Arbitrary failures after engine construction has registered resources are
  // outside this fallback claim and require separate fault-injection coverage.
});

test('adapter defaults keep the approved budget, DPR cap, debug privacy and active-time wave expiry', async () => {
  const host = document.createElement('main'); document.body.append(host);
  const controller = mountStarwave(host);
  try {
    assert.equal(controller.ready, true);
    const state = controller.getDebugState();
    assert.equal(state.paused, false); assert.equal(state.reducedMotion, false);
    assert.equal(state.width, 640); assert.equal(state.height, 360);
    assert.ok(state.stars > 0 && state.stars <= 18000);
    const canvas = host.querySelector('canvas');
    assert.equal(canvas.width, 640 * 1.75); assert.equal(canvas.height, 360 * 1.75);
    assert.equal(canvas.hasAttribute('data-ripples'), false, 'default does not publish debug values');
    tick(0);
    host.dispatchEvent(new MouseEvent('click', {bubbles: true, detail: 1, clientX: 100, clientY: 100}));
    assert.equal(controller.getDebugState().ripples, 1);
    for (let time = 16; time <= 2304; time += 16) tick(time);
    assert.equal(controller.getDebugState().ripples, 0);
  } finally {controller.dispose(); host.remove();}
  assertReleased();
});
