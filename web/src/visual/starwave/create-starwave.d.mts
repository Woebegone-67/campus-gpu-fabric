export interface StarwaveController {
  readonly ready: boolean;
  readonly error: unknown;
  setPaused(value: boolean): void;
  clearWaves(): void;
  refreshLayout(): void;
  getDebugState(): {ripples?: number; stars?: number; renderedFrames?: number; elapsedMs?: number; width?: number; height?: number; lastFrameMs?: number; paused?: boolean; systemReduced?: boolean; reducedMotion?: boolean};
  dispose(): void;
}
export function mountStarwave(host: HTMLElement, options?: {paused?: boolean; debug?: boolean; onError?: (error: unknown) => void}): StarwaveController;
