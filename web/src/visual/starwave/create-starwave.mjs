/* Optional, framework-neutral integration adapter. No CP4 business dependencies. */
import { StarwaveScene } from './starwave-engine.mjs';
import { STARWAVE_PRESET } from './starwave-preset.mjs';
const mounted = new WeakMap();

function boundedNumber(value, min, max, label) {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max) {
    throw new RangeError(`${label} must be a finite number between ${min} and ${max}`);
  }
  return value;
}

// The host must be a mounted, unscaled main-content element. It must exclude the sidebar.
export function mountStarwave(host, { paused = false, debug = false, onError } = {}) {
  if (!host || host.nodeType !== 1) throw new TypeError('A real host element is required');
  if (mounted.has(host)) throw new Error('Starwave already mounted: dispose before mounting again');
  if (typeof paused !== 'boolean' || typeof debug !== 'boolean') throw new TypeError('paused/debug must be booleans');
  let scene = null, initError = null, reportingError = null, disposed = false, changedFill = false;
  const originalFill = host.style.getPropertyValue('--sw-glass-alpha');
  const originalPriority = host.style.getPropertyPriority('--sw-glass-alpha');
  const originalChildren = new Set(host.children);
  try {
    scene = new StarwaveScene(host, { ...STARWAVE_PRESET, paused: !!paused, debug: !!debug });
  } catch (error) {
    initError = error;
    // Common failure (Canvas 2D unavailable) happens before insertion. Remove only
    // a canvas newly inserted by this attempt, never a pre-existing child.
    for (const child of [...host.children]) {
      if (!originalChildren.has(child) && child.classList.contains('fabric-starwave-canvas')) child.remove();
    }
  }
  function alive() { if (disposed) throw new Error('Starwave adapter has been disposed'); }
  const api = {
    get ready() { return !disposed && !!scene; },
    get error() { return initError; },
    get reportingError() { return reportingError; },
    setPaused(value) { alive(); if (typeof value !== 'boolean') throw new TypeError('paused must be a boolean'); scene?.setOptions({ paused: value }); },
    setDensity(value) { alive(); boundedNumber(value, .5, 1.6, 'density'); scene?.setOptions({ density: value }); },
    setWaveStrength(value) { alive(); boundedNumber(value, 6, 36, 'waveStrength'); scene?.setOptions({ waveStrength: value }); },
    setGlassFill(percent) {
      alive(); boundedNumber(percent, 0, 40, 'glass fill');
      host.style.setProperty('--sw-glass-alpha', (percent / 100).toFixed(2)); changedFill = true;
    },
    clearWaves() { alive(); scene?.clearWaves(); },
    refreshLayout() { alive(); scene?.resize(); },
    getDebugState() { return scene ? scene.getDebugState() : { ready: false, disposed, reason: String(initError || '') }; },
    dispose() {
      if (disposed) return;
      disposed = true; scene?.dispose(); scene = null; mounted.delete(host);
      if (changedFill) {
        if (originalFill) host.style.setProperty('--sw-glass-alpha', originalFill, originalPriority);
        else host.style.removeProperty('--sw-glass-alpha');
      }
    }
  };
  mounted.set(host, api);
  if (initError && typeof onError === 'function') {
    try { onError(initError); } catch (error) { reportingError = error; }
  }
  return api;
}
