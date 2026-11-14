/* ESM wrapper adaptation only. Canonical source: ../demo/starwave.js (MIT). */
  'use strict';

  const INTERACTIVE_SELECTOR = [
    'button', 'a', 'input', 'textarea', 'select', 'option', 'label', 'summary',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="checkbox"]',
    '[role="switch"]', '[role="radio"]', '[role="slider"]', '[role="menuitem"]',
    '[role="textbox"]', '[contenteditable]', '[data-no-ripple]', '[data-interactive]'
  ].join(',');

  function shouldCreateRipple(target, event) {
    if (event && event.button !== undefined && event.button !== 0) return false;
    if (event && (event.detail === 0 || event.detail > 1 || event.defaultPrevented)) return false;
    const nodes = event && typeof event.composedPath === 'function'
      ? Array.from(event.composedPath()) : [];
    let cur = target;
    while (cur) { nodes.push(cur); cur = cur.parentElement || null; }
    for (const node of nodes) {
      if (!node || typeof node.matches !== 'function') continue;
      if (node.matches(INTERACTIVE_SELECTOR) || node.isContentEditable) return false;
      if (node.getAttribute && node.getAttribute('aria-disabled') === 'true') return false;
    }
    return true;
  }

  /** Localized, signed elastic displacement. The wave travels in screen space. */
  function ringDisplacement(dx, dy, ageMs, settings) {
    const duration = settings.durationMs ?? 2200;
    if (ageMs < 0 || ageMs >= duration) return { x: 0, y: 0, luminance: 0 };
    const radius = Math.hypot(dx, dy);
    const offset = radius - (settings.speed ?? 320) * ageMs / 1000;
    const falloff = Math.exp(-Math.pow(offset / (settings.ringWidth ?? 54), 2));
    const envelope = Math.pow(Math.max(0, 1 - ageMs / duration), 1.25);
    const attack = Math.min(1, ageMs / 70);
    const displacement = (settings.strength ?? 18) * falloff * envelope * attack
      * Math.cos(offset * (settings.ringFrequency ?? 0.075));
    const unit = radius > .5 ? 1 / radius : 0;
    return { x: dx * unit * displacement, y: dy * unit * displacement,
      luminance: Math.min(1, falloff * envelope * attack) };
  }

  /** Every point shares the same differentiable field; no per-star drifting. */
  function sampleSurface(u, v, time) {
    // Two coupled folds compress and spread neighbouring rows like a projected
    // silk sheet. Near a turning fold the samples gather into a luminous ridge.
    const a = v * .012 + .78 * Math.sin(u * .0041) - time * .19;
    const b = u * .0078 + v * .0105 + .44 * Math.sin(v * .004) + time * .14;
    const c = u * .0035 - v * .005 + time * .1;
    const foldA = Math.sin(a), foldB = Math.sin(b);
    const slope = 1 + 1.05 * Math.cos(b);
    return {
      x: u + 116 * foldA + 25 * Math.sin(u * .008 + v * .004 - time * .12),
      y: v + 100 * foldB + 38 * Math.cos(c),
      ridge: .18 + .82 * Math.exp(-Math.abs(slope) * 1.65),
      depth: .5 + .5 * Math.sin(a - .6)
    };
  }

  function noise01(seed) {
    const v = Math.sin(seed * 127.1 + seed * seed * .00173) * 43758.5453;
    return v - Math.floor(v);
  }
  function smoothstep(a, b, value) {
    const t = Math.max(0, Math.min(1, (value - a) / (b - a)));
    return t * t * (3 - 2 * t);
  }

  const DEFAULTS = Object.freeze({
    maxStars: 18000, maxFps: 45, spacing: 12, density: 1, brightness: 1.02,
    waveStrength: 18, waveDurationMs: 2200, waveSpeed: 320,
    hoverStrength: 2.4, ambientSpeed: .43, maxConcurrentWaves: 4,
    maxDpr: 1.75, reducedMotion: null, paused: false,
    palette: ['#D4D4D4', '#C9C9C9', '#EEEEEE'], debug: false
  });

  function makeStarSprite(color, elongated) {
    const canvas = document.createElement('canvas');
    canvas.width = canvas.height = 48;
    const ctx = canvas.getContext('2d');
    const c = 24;
    const halo = ctx.createRadialGradient(c, c, 0, c, c, 21);
    halo.addColorStop(0, color + '78');
    halo.addColorStop(.22, color + '20');
    halo.addColorStop(1, color + '00');
    ctx.fillStyle = halo;
    ctx.fillRect(0, 0, 48, 48);
    const vertical = elongated ? 22 : 19;
    const horizontal = elongated ? 18 : 16;
    ctx.beginPath();
    ctx.moveTo(c, c - vertical);
    ctx.quadraticCurveTo(c + .8, c - .8, c + horizontal, c);
    ctx.quadraticCurveTo(c + .8, c + .8, c, c + vertical);
    ctx.quadraticCurveTo(c - .8, c + .8, c - horizontal, c);
    ctx.quadraticCurveTo(c - .8, c - .8, c, c - vertical);
    ctx.closePath(); ctx.fillStyle = color; ctx.fill();
    // A very small square core keeps the fine four-arm silhouette, even at 1x DPR.
    ctx.fillStyle = '#F7F7F7'; ctx.fillRect(c - .85, c - .85, 1.7, 1.7);
    return canvas;
  }

  class StarwaveScene {
    constructor(container, userOptions) {
      if (!container || typeof document === 'undefined') throw new TypeError('StarwaveScene requires a real DOM container');
      this.container = container;
      this.options = { ...DEFAULTS, ...(userOptions || {}) };
      this.canvas = document.createElement('canvas');
      this.canvas.className = 'fabric-starwave-canvas';
      this.canvas.setAttribute('aria-hidden', 'true');
      this.canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:0;';
      this.ctx = this.canvas.getContext('2d', { alpha: true });
      if (!this.ctx) throw new Error('Canvas 2D unavailable');
      if (getComputedStyle(container).position === 'static') {
        this._changedPosition = true; container.style.position = 'relative';
      }
      container.prepend(this.canvas);
      this.width = this.height = 0; this.dpr = 1;
      this.stars = []; this.waves = [];
      this.hover = { x: -9999, y: -9999, until: 0 };
      this.stats = { frameMs: 0, renderedFrames: 0 };
      this.elapsedMs = 0; this.lastTime = null;
      this._lastPaintAt = -Infinity; this._rafId = 0; this._disposed = false;
      this._gesture = null;
      this._motionQuery = typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : null;
      this._systemReduced = this._motionQuery ? this._motionQuery.matches : false;
      this._renderBound = time => this._render(time);
      this._onDown = event => {
        this._gesture = { x: event.clientX, y: event.clientY, dragged: false,
          blocked: !shouldCreateRipple(event.target, { button: event.button, detail: 1 }) };
      };
      this._onCancel = () => { if (this._gesture) this._gesture.dragged = true; };
      this._onClick = event => {
        const gesture = this._gesture;
        this._gesture = null;
        if ((gesture && (gesture.dragged || gesture.blocked)) || !shouldCreateRipple(event.target, event)) return;
        const selection = window.getSelection && window.getSelection();
        if ((selection && !selection.isCollapsed) || this.isReducedMotion()) return;
        const bounds = this.container.getBoundingClientRect();
        this.emitWave(event.clientX - bounds.left, event.clientY - bounds.top);
      };
      this._onMove = event => {
        if (this._gesture && Math.hypot(event.clientX - this._gesture.x, event.clientY - this._gesture.y) > 6) this._gesture.dragged = true;
        if (this.isReducedMotion() || event.pointerType === 'touch') return;
        const box = this.container.getBoundingClientRect();
        this.hover.x = event.clientX - box.left;
        this.hover.y = event.clientY - box.top;
        this.hover.until = this.elapsedMs + 140;
      };
      this._onVisibility = () => { if (document.hidden) this._stop(); else this._start(); };
      this._onMotionChange = event => {
        this._systemReduced = event.matches;
        if (this.isReducedMotion()) { this._stop(); this.waves = []; this._draw(this.elapsedMs); }
        else this._start();
      };
      // Bubble phase honours a component's preventDefault without intercepting its event.
      container.addEventListener('click', this._onClick);
      container.addEventListener('pointerdown', this._onDown, { passive: true });
      container.addEventListener('pointermove', this._onMove, { passive: true });
      container.addEventListener('pointercancel', this._onCancel, { passive: true });
      document.addEventListener('visibilitychange', this._onVisibility);
      if (this._motionQuery) {
        if (this._motionQuery.addEventListener) this._motionQuery.addEventListener('change', this._onMotionChange);
        else if (this._motionQuery.addListener) this._motionQuery.addListener(this._onMotionChange);
      }
      this._observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(() => this.resize()) : null;
      if (this._observer) this._observer.observe(container);
      else { this._onResize = () => this.resize(); window.addEventListener('resize', this._onResize); }
      this._makeSprites(); this.resize(); this._start();
    }

    // Explicit resume cannot override the user's operating-system preference.
    isReducedMotion() { return this._systemReduced || !!this.options.reducedMotion || !!this.options.paused; }

    _makeSprites() {
      this.sprites = this.options.palette.map(color => ({ tiny: makeStarSprite(color, false), wide: makeStarSprite(color, true) }));
    }

    _generateStars() {
      const { spacing, density, maxStars } = this.options;
      const margin = 160;
      const spanX = this.width + margin * 2;
      let pitch = Math.max(8, spacing / Math.sqrt(Math.max(.25, density)));
      let cols, sampleRows;
      const makeRows = step => {
        const positions = [];
        // Spend detail on the exposed hero; lower cards need fewer hidden samples.
        for (let y = -margin; y < this.height + margin; y += step * (.74 + .72 * smoothstep(300, 900, y))) positions.push(y);
        return positions;
      };
      do {
        cols = Math.ceil(spanX / pitch); sampleRows = makeRows(pitch);
        if (cols * sampleRows.length <= maxStars) break;
        pitch *= 1.015;
      } while (true);

      this.stars = [];
      for (let row = 0; row < sampleRows.length; row++) {
        for (let col = 0; col < cols; col++) {
          const seed = row * 99991 + col * 1171;
          const r = noise01(seed + 7);
          this.stars.push({ x: col * pitch - margin, y: sampleRows[row],
            tone: Math.floor(noise01(seed + 13) * this.sprites.length),
            scale: .9 + r * .2, accent: r > .986, baseAlpha: .87 + noise01(seed + 72) * .13 });
        }
      }
    }

    resize() {
      if (this._disposed) return;
      const rect = this.container.getBoundingClientRect();
      this.width = Math.max(1, rect.width); this.height = Math.max(1, rect.height);
      this.dpr = Math.min(this.options.maxDpr, Math.max(1, window.devicePixelRatio || 1));
      this.canvas.width = Math.round(this.width * this.dpr);
      this.canvas.height = Math.round(this.height * this.dpr);
      this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
      this._generateStars(); this._draw(this.elapsedMs);
    }

    emitWave(x, y) {
      if (this._disposed || this.isReducedMotion()) return false;
      if (!Number.isFinite(x) || !Number.isFinite(y) || x < 0 || y < 0 || x > this.width || y > this.height) return false;
      this.waves.push({ x, y, time: this.elapsedMs });
      if (this.waves.length > this.options.maxConcurrentWaves) this.waves.shift();
      this._publishDebug(); this._start(); return true;
    }

    clearWaves() {
      if (this._disposed) return;
      this.waves = [];
      this._draw(this.elapsedMs);
    }

    setOptions(next) {
      if (this._disposed) return;
      this.options = { ...this.options, ...next };
      if (next.palette) this._makeSprites();
      if (next.spacing !== undefined || next.density !== undefined || next.maxStars !== undefined || next.palette) this._generateStars();
      if (this.isReducedMotion()) {
        this._stop();
        if (this._systemReduced || this.options.reducedMotion) this.waves = [];
        this._draw(this.elapsedMs);
      } else this._start();
      this._publishDebug();
    }

    _publishDebug() {
      if (!this.options.debug || !this.canvas) return;
      const state = this.getDebugState();
      const values = { ripples: state.ripples, stars: state.stars, frames: state.renderedFrames,
        elapsedMs: state.elapsedMs.toFixed(2), paused: state.paused,
        systemReduced: state.systemReduced, reducedMotion: state.reducedMotion,
        frameMs: state.lastFrameMs };
      for (const key of Object.keys(values)) this.canvas.dataset[key] = String(values[key]);
    }

    getDebugState() {
      return { ripples: this.waves.length, stars: this.stars.length,
        reducedMotion: this.isReducedMotion(), paused: !!this.options.paused,
        systemReduced: this._systemReduced, elapsedMs: this.elapsedMs,
        width: this.width, height: this.height,
        renderedFrames: this.stats.renderedFrames, lastFrameMs: this.stats.frameMs };
    }

    _start() {
      if (this._disposed || document.hidden || this.isReducedMotion() || this._rafId) return;
      this._rafId = requestAnimationFrame(this._renderBound);
    }
    _stop() {
      if (this._rafId) cancelAnimationFrame(this._rafId);
      this._rafId = 0; this.lastTime = null; this._lastPaintAt = -Infinity;
    }
    _render(now) {
      this._rafId = 0;
      if (this._disposed || document.hidden || this.isReducedMotion()) { this.lastTime = null; return; }
      if (this.lastTime !== null) this.elapsedMs += Math.min(64, Math.max(0, now - this.lastTime));
      this.lastTime = now;
      const interval = 1000 / Math.max(1, this.options.maxFps || 45);
      if (now - this._lastPaintAt < interval) { this._start(); return; }
      this._lastPaintAt = now;
      const start = performance.now(); this._draw(this.elapsedMs);
      this.stats.frameMs = Math.round((performance.now() - start) * 100) / 100;
      this.stats.renderedFrames++; this._publishDebug(); this._start();
    }

    _draw(now) {
      const ctx = this.ctx, opts = this.options;
      const time = now / 1000 * opts.ambientSpeed;
      ctx.clearRect(0, 0, this.width, this.height);
      this.waves = this.waves.filter(wave => now - wave.time < opts.waveDurationMs);
      const active = this.waves;
      const hoverOn = !this.isReducedMotion() && now < this.hover.until;
      const ringSettings = { strength: opts.waveStrength, durationMs: opts.waveDurationMs,
        speed: opts.waveSpeed, ringWidth: 56, ringFrequency: .075 };
      for (const star of this.stars) {
        const surface = sampleSurface(star.x, star.y, time);
        let x = surface.x, y = surface.y, highlight = 0;
        if (hoverOn) {
          const dx = x - this.hover.x, dy = y - this.hover.y, dist = Math.hypot(dx, dy);
          if (dist < 120 && dist > 1) {
            const strength = opts.hoverStrength * Math.pow(1 - dist / 120, 2);
            x += dx / dist * strength; y += dy / dist * strength;
          }
        }
        // All impulses sample the undeformed point: overlapping rings remain symmetric.
        const baseX = x, baseY = y;
        for (const impulse of active) {
          const response = ringDisplacement(baseX - impulse.x, baseY - impulse.y, now - impulse.time, ringSettings);
          x += response.x; y += response.y; highlight = Math.max(highlight, response.luminance);
        }
        if (x < -15 || x > this.width + 15 || y < -15 || y > this.height + 15) continue;
        const rightLight = .29 + .71 * smoothstep(.03, .61, x / this.width);
        const lowerFade = 1 - .18 * smoothstep(480, 1100, y);
        const ridgeLight = .28 + .66 * surface.ridge + .1 * surface.depth;
        const alpha = Math.min(.91, star.baseAlpha * opts.brightness * rightLight * lowerFade * ridgeLight + highlight * .58);
        const size = (star.accent ? 8 : 5.2) * star.scale * (.86 + surface.depth * .22 + highlight * .35);
        ctx.globalAlpha = alpha;
        ctx.drawImage(this.sprites[star.tone][star.accent ? 'wide' : 'tiny'], x - size / 2, y - size / 2, size, size);
      }
      ctx.globalAlpha = 1;
      for (const wave of active) {
        const age = now - wave.time, p = Math.min(1, age / opts.waveDurationMs);
        const radius = opts.waveSpeed * age / 1000;
        if (radius <= .5) continue;
        const envelope = Math.pow(1 - p, 1.7) * Math.min(1, age / 70);
        // Thin caustic plus a softly returning inner edge; displaced stars carry the wave.
        for (let n = 0; n < 2; n++) {
          const r = radius - n * 23;
          if (r <= 0) continue;
          ctx.beginPath(); ctx.arc(wave.x, wave.y, r, 0, Math.PI * 2);
          ctx.strokeStyle = 'rgba(210,210,210,' + (envelope * (n ? .045 : .19)).toFixed(3) + ')';
          ctx.lineWidth = n ? .7 : 1; ctx.stroke();
        }
        if (age < 320) {
          const spot = ctx.createRadialGradient(wave.x, wave.y, 0, wave.x, wave.y, 24);
          const a = .4 * Math.max(0, 1 - age / 320);
          spot.addColorStop(0, 'rgba(240,240,240,' + a.toFixed(3) + ')');
          spot.addColorStop(.2, 'rgba(204,204,204,' + (a * .24).toFixed(3) + ')');
          spot.addColorStop(1, 'rgba(204,204,204,0)');
          ctx.fillStyle = spot; ctx.fillRect(wave.x - 24, wave.y - 24, 48, 48);
        }
      }
      ctx.globalAlpha = 1;
      this._publishDebug();
    }

    dispose() {
      this._disposed = true; this._stop();
      this.container.removeEventListener('click', this._onClick);
      this.container.removeEventListener('pointerdown', this._onDown);
      this.container.removeEventListener('pointermove', this._onMove);
      this.container.removeEventListener('pointercancel', this._onCancel);
      document.removeEventListener('visibilitychange', this._onVisibility);
      if (this._observer) this._observer.disconnect();
      if (this._onResize) window.removeEventListener('resize', this._onResize);
      if (this._motionQuery) {
        if (this._motionQuery.removeEventListener) this._motionQuery.removeEventListener('change', this._onMotionChange);
        else if (this._motionQuery.removeListener) this._motionQuery.removeListener(this._onMotionChange);
      }
      if (this.canvas.isConnected) this.canvas.remove();
      if (this._changedPosition) this.container.style.position = '';
      this.waves.length = this.stars.length = 0;
    }
  }

export { StarwaveScene, shouldCreateRipple, ringDisplacement, sampleSurface, DEFAULTS };
