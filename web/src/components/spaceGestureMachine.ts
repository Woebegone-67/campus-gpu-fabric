/** Presentation-only recognizer. Pixel thresholds are uncalibrated prototype
 * defaults from UI_GESTURE_AND_MOTION.md, not proof of trackpad compatibility. */
export type Space = 'overview' | 'workbench';
export type GesturePhase = 'idle' | 'candidate' | 'native' | 'tracking' | 'cooldown';

export const SPACE_GESTURE_DEFAULTS = Object.freeze({
  start: 18, axisRatio: 1.8, commit: 96, reverse: 24,
  quietMs: 180, cooldownMs: 550, previewRatio: 0.18, maxPreview: 180,
});

export type GestureSample = {
  deltaX: number; deltaY: number; deltaMode: number; time: number;
  cancelable: boolean; eligible: boolean; modified: boolean;
  space: Space; width: number;
};
export type GestureOutput = {
  phase: GesturePhase; preview: number; consume: boolean;
  navigate?: Space;
};

/** No timers, DOM, routing, identity data or API calls live in this class. */
export class SpaceGestureRecognizer {
  private phase: GesturePhase = 'idle';
  private source: Space = 'overview';
  private lastTime = -Infinity;
  private cooldownAt = -Infinity;
  private owned = false;
  private x = 0;
  private absoluteX = 0;
  private absoluteY = 0;
  private direction = 0;
  private reverse = 0;
  private samples = 0;
  private preview = 0;

  constructor(private readonly config = SPACE_GESTURE_DEFAULTS) {}

  get tracking() {return this.phase === 'tracking';}

  private output(consume = false, navigate?: Space): GestureOutput {
    return {phase: this.phase, preview: this.preview, consume, ...(navigate ? {navigate} : {})};
  }

  private idle() {
    this.phase = 'idle'; this.owned = false; this.preview = 0;
    this.x = 0; this.absoluteX = 0; this.absoluteY = 0;
    this.direction = 0; this.reverse = 0; this.samples = 0;
  }

  /** External interruptions cancel a preview, never undo an already committed
   * navigation or release its inertia lock. */
  cancel(time: number): GestureOutput {
    if (this.phase === 'candidate' || this.phase === 'tracking') {
      this.phase = 'cooldown'; this.cooldownAt = time;
      this.owned = false;
    }
    this.preview = 0;
    return this.output();
  }

  /** Called after input becomes quiet. A short gesture returns to its origin. */
  quiet(time: number): GestureOutput {
    if (time - this.lastTime < this.config.quietMs) return this.output();
    if (this.phase === 'tracking') {
      this.phase = 'cooldown'; this.cooldownAt = time; this.preview = 0;
    } else if (this.phase === 'candidate' || this.phase === 'native' ||
      (this.phase === 'cooldown' && time - this.cooldownAt >= this.config.cooldownMs)) {
      this.idle();
    }
    return this.output();
  }

  feed(sample: GestureSample): GestureOutput {
    const {time, deltaX, deltaY} = sample;
    const quiet = time - this.lastTime >= this.config.quietMs;
    if (quiet && (this.phase !== 'cooldown' || time - this.cooldownAt >= this.config.cooldownMs)) {
      this.idle();
    }
    this.lastTime = time;
    const usable = sample.eligible && sample.cancelable && !sample.modified && sample.deltaMode === 0 &&
      Number.isFinite(deltaX) && Number.isFinite(deltaY);

    if (this.phase === 'cooldown') return this.output(usable && this.owned);
    if (!usable || (this.phase !== 'idle' && sample.space !== this.source)) {
      this.phase = 'native'; this.preview = 0; this.owned = false;
      return this.output();
    }
    if (this.phase === 'native') return this.output();
    if (this.phase === 'idle') {this.source = sample.space; this.phase = 'candidate';}

    this.x += deltaX;
    this.absoluteX += Math.abs(deltaX); this.absoluteY += Math.abs(deltaY);
    if (deltaX !== 0) this.samples++;

    if (this.phase === 'candidate') {
      if (this.absoluteY >= this.config.start && this.absoluteY >= this.absoluteX * this.config.axisRatio) {
        this.phase = 'native'; return this.output();
      }
      if (Math.abs(this.x) < this.config.start || this.absoluteX < this.absoluteY * this.config.axisRatio) {
        return this.output();
      }
      this.direction = Math.sign(this.x);
      if ((this.source === 'overview' && this.direction < 0) ||
          (this.source === 'workbench' && this.direction > 0)) {
        this.phase = 'native'; return this.output();
      }
      this.phase = 'tracking'; this.owned = true;
    } else {
      this.reverse += Math.max(0, -deltaX * this.direction);
      if (this.reverse >= this.config.reverse && this.x * this.direction <= this.config.start) {
        this.phase = 'cooldown'; this.cooldownAt = time; this.preview = 0;
        return this.output(true);
      }
    }

    // Positive deltaX means content moving left, not a claim about finger direction.
    this.preview = -this.direction * Math.min(Math.max(0, this.x * this.direction),
      Math.max(0, sample.width) * this.config.previewRatio, this.config.maxPreview);
    // A single unusually large event is never sufficient on its own.
    if (this.samples >= 2 && this.x * this.direction >= this.config.commit) {
      this.phase = 'cooldown'; this.cooldownAt = time; this.preview = 0;
      return this.output(true, this.source === 'overview' ? 'workbench' : 'overview');
    }
    return this.output(true);
  }
}
