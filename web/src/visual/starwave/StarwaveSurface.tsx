import {createContext, useContext, useEffect, useRef, useState, type ReactNode} from 'react';
import {mountStarwave, type StarwaveController} from './create-starwave.mjs';

const MotionContext = createContext<ReactNode>(null);
export function StarwaveMotionControl() { return useContext(MotionContext); }

/** One viewport-sized visual host. Business scrolling happens in its child,
 * so canvas coordinates never include scrollTop or an unbounded event history. */
export function StarwaveSurface({children, enabled, visible = true}: {children: ReactNode; enabled: boolean; visible?: boolean}) {
  const host = useRef<HTMLElement>(null);
  const controller = useRef<StarwaveController | null>(null);
  const [paused, setPaused] = useState(false);
  const [systemReduced, setSystemReduced] = useState(() =>
    typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [ready, setReady] = useState(false);
  useEffect(() => {
    if (!enabled || !host.current) return;
    const element = host.current;
    const instance = mountStarwave(element, {
      paused: paused || !visible,
      debug: import.meta.env?.DEV === true,
      onError: error => console.warn('[Starwave] Static background fallback:', error),
    });
    controller.current = instance;
    setReady(instance.ready);
    // Development-only, local browser inspection. No globals, storage or network.
    const inspectable = element as HTMLElement & {__starwave?: StarwaveController};
    if (import.meta.env?.DEV) inspectable.__starwave = instance;
    const query = typeof matchMedia === 'function' ? matchMedia('(prefers-reduced-motion: reduce)') : null;
    const syncMotion = () => setSystemReduced(query?.matches ?? false);
    if (query?.addEventListener) query.addEventListener('change', syncMotion);
    else query?.addListener(syncMotion);
    syncMotion();
    return () => {
      if (query?.removeEventListener) query.removeEventListener('change', syncMotion);
      else query?.removeListener(syncMotion);
      instance.dispose();
      delete inspectable.__starwave;
      if (controller.current === instance) controller.current = null;
    };
  }, [enabled]);
  useEffect(() => {
    const instance = controller.current;
    if (!instance) return;
    if (visible) {
      instance.setPaused(paused);
      return;
    }
    const canvas = host.current?.querySelector<HTMLCanvasElement>('.fabric-starwave-canvas');
    if (!canvas || paused || systemReduced) {
      instance.setPaused(true);
      return;
    }
    // Keep the same scene moving throughout its fade, then stop hidden work.
    // Read the CSS timing so reduced-motion and future style changes stay in sync.
    const style = getComputedStyle(canvas);
    const milliseconds = (value: string) => (parseFloat(value) || 0) * (value.trim().endsWith('ms') ? 1 : 1000);
    const delays = style.transitionDelay.split(',').map(milliseconds);
    const duration = Math.max(0, ...style.transitionDuration.split(',').map((value, index) =>
      milliseconds(value) + delays[index % delays.length]));
    if (!duration) {
      instance.setPaused(true);
      return;
    }
    const stop = () => {
      clearTimeout(timer);
      instance.setPaused(true);
    };
    const onEnd = (event: TransitionEvent) => {
      if (event.target === canvas && event.propertyName === 'opacity') stop();
    };
    canvas.addEventListener('transitionend', onEnd);
    const timer = setTimeout(stop, duration + 50);
    return () => {
      clearTimeout(timer);
      canvas.removeEventListener('transitionend', onEnd);
    };
  }, [paused, enabled, visible, systemReduced]);
  const motionControl = enabled && visible ? <button type="button" className="button quiet sw-motion-control"
    disabled={systemReduced || !ready} aria-pressed={paused || systemReduced}
    onClick={() => setPaused(value => !value)}>
    {systemReduced ? '背景静态 · 减少动态' : !ready ? '背景静态' : paused ? '恢复背景动效' : '暂停背景动效'}
  </button> : null;
  return <MotionContext.Provider value={motionControl}>
    <main ref={host} className={enabled ? 'sw-stage' : 'sw-stage sw-stage-static'} data-starwave-visible={enabled && visible}>
      <div className="sw-content"><div className="sw-scroll-content">{children}</div></div>
    </main>
  </MotionContext.Provider>;
}
