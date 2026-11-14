import {useEffect, useRef, useState} from 'react';
import type {ReactNode} from 'react';
import {SPACE_GESTURE_DEFAULTS, SpaceGestureRecognizer} from './spaceGestureMachine';
import type {GestureOutput, GesturePhase, Space} from './spaceGestureMachine';

export type {Space} from './spaceGestureMachine';

const EXCLUDED = [
  'a', 'button', 'input', 'select', 'textarea', 'option', 'label', 'form', 'fieldset', 'summary',
  'table', 'pre', 'code', 'figure', 'canvas', 'svg', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p',
  '[contenteditable]:not([contenteditable="false"])', '[draggable="true"]',
  '[tabindex]:not([tabindex="-1"])', '[aria-haspopup]',
  '[data-space-gesture-ignore]', '[data-interactive]', '[data-no-ripple]',
  '[role="button"]', '[role="link"]', '[role="tab"]', '[role="slider"]', '[role="separator"]',
  '[role="checkbox"]', '[role="switch"]', '[role="radio"]', '[role="textbox"]',
  '[role="combobox"]', '[role="spinbutton"]', '[role="grid"]', '[role="table"]',
  '[role="tree"]', '[role="menu"]', '[role="listbox"]', '[role="dialog"]',
  '[data-chart]', '[data-timeline]',
].join(',');
const EDITABLE = 'input,textarea,select,[contenteditable]:not([contenteditable="false"]),[role="textbox"],[role="combobox"],[role="spinbutton"]';

function hasBlockingOverlay(document: Document) {
  return [...document.querySelectorAll('dialog[open],[aria-modal="true"],[role="menu"],[role="listbox"],[aria-haspopup][aria-expanded="true"],[data-space-gesture-blocked="true"],[popover]')]
    .some(element => {
      if (element.closest('[hidden],[inert],[aria-hidden="true"]')) return false;
      const style = document.defaultView?.getComputedStyle(element);
      return style?.display !== 'none' && style?.visibility !== 'hidden';
    });
}

function allowedPath(event: WheelEvent, region: HTMLElement) {
  const path = event.composedPath();
  if (!path.includes(region)) return false;
  for (const entry of path) {
    if (entry === region) return true;
    if (!(entry instanceof Element)) continue;
    if (entry.matches(EXCLUDED)) return false;
    const style = region.ownerDocument.defaultView?.getComputedStyle(entry);
    if (!entry.children.length && entry.textContent?.trim() && style?.userSelect !== 'none') return false;
    // Even an empty scroll container or a table already at its boundary owns its input.
    if (style && /auto|scroll|overlay/.test(`${style.overflowX} ${style.overflowY}`)) return false;
  }
  return false;
}

export interface SpaceGestureProps {
  children: ReactNode;
  activeSpace: Space;
  /** Off until the user explicitly opts into this uncalibrated input path. */
  enabled?: boolean;
  /** Parent suspends gestures for overlays, file picking or context changes. */
  blocked?: boolean;
  /** Change for identity, epoch, or explicit navigation; never pass credentials. */
  scopeKey?: string | number;
  onNavigate: (target: Space) => void;
  /** Limited content displacement; fixed navigation/identity controls should not move. */
  onPreview?: (offset: number) => void;
  className?: string;
}

/** Mount around an explicitly marked noninteractive header zone, not the page.
 * Keep mounted across spaces so inertia cannot become a new reverse gesture.
 * The component emits presentation intents only: it never writes history or calls APIs. */
export function SpaceGesture({children, activeSpace, enabled = false, blocked = false,
  scopeKey, onNavigate, onPreview, className = ''}: SpaceGestureProps) {
  const regionRef = useRef<HTMLDivElement>(null);
  const recognizer = useRef(new SpaceGestureRecognizer());
  const latest = useRef({activeSpace, blocked, onNavigate, onPreview});
  latest.current = {activeSpace, blocked, onNavigate, onPreview};
  const [phase, setPhase] = useState<GesturePhase>('idle');
  const lastPreview = useRef(0);
  const emit = useRef<(result: GestureOutput) => void>(() => {});
  emit.current = result => {
    setPhase(result.phase);
    if (lastPreview.current !== result.preview) {
      lastPreview.current = result.preview;
      latest.current.onPreview?.(result.preview);
    }
    if (result.navigate) latest.current.onNavigate(result.navigate);
  };

  useEffect(() => {
    emit.current(recognizer.current.cancel(performance.now()));
  }, [activeSpace, blocked, scopeKey]);

  useEffect(() => {
    const region = regionRef.current;
    if (!enabled || !region) {
      emit.current(recognizer.current.cancel(performance.now()));
      return;
    }
    const document = region.ownerDocument;
    const window = document.defaultView!;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let composing = false;
    let dragging = false;
    let pointerHeld = false;
    let disposed = false;
    const cancel = () => emit.current(recognizer.current.cancel(performance.now()));
    const scheduleQuiet = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (!disposed) emit.current(recognizer.current.quiet(performance.now()));
      }, SPACE_GESTURE_DEFAULTS.quietMs + 1);
    };
    const sample = (event: WheelEvent, inside: boolean) => {
      const focus = document.activeElement;
      const selection = document.getSelection();
      const eligible = inside && !latest.current.blocked && !document.hidden && !composing && !dragging && !pointerHeld &&
        !focus?.matches(EDITABLE) && (!selection || selection.isCollapsed) && !hasBlockingOverlay(document) && allowedPath(event, region);
      const result = recognizer.current.feed({deltaX: event.deltaX, deltaY: event.deltaY,
        deltaMode: event.deltaMode, cancelable: event.cancelable && !event.defaultPrevented,
        modified: event.ctrlKey || event.metaKey || event.altKey || event.shiftKey,
        eligible, time: performance.now(), space: latest.current.activeSpace,
        width: region.getBoundingClientRect().width});
      if (result.consume) {
        event.preventDefault();
        if (!event.defaultPrevented) {
          // A cancelable flag alone does not establish ownership in every host.
          cancel(); scheduleQuiet(); return;
        }
      }
      emit.current(result); scheduleQuiet();
    };
    const wheel = (event: WheelEvent) => sample(event, true);
    // Observation is passive outside the whitelist. This prevents a sequence
    // that began in a table/body from being reclassified when the pointer moves.
    const outsideWheel = (event: WheelEvent) => {
      if (!event.composedPath().includes(region)) sample(event, false);
    };
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !latest.current.blocked && !hasBlockingOverlay(document) &&
        recognizer.current.tracking) {
        event.preventDefault(); cancel();
      }
    };
    const compositionStart = () => {composing = true; cancel();};
    const compositionEnd = () => {composing = false;};
    const dragStart = () => {dragging = true; cancel();};
    const dragEnd = () => {dragging = false;};
    const pointerDown = () => {pointerHeld = true; cancel();};
    const pointerUp = () => {pointerHeld = false;};
    const blur = () => {pointerHeld = false; dragging = false; composing = false; cancel();};
    region.addEventListener('wheel', wheel, {passive: false, capture: true});
    window.addEventListener('wheel', outsideWheel, {passive: true, capture: true});
    window.addEventListener('scroll', cancel, {passive: true, capture: true});
    window.addEventListener('resize', cancel);
    window.addEventListener('blur', blur);
    window.addEventListener('popstate', cancel);
    window.addEventListener('hashchange', cancel);
    document.addEventListener('visibilitychange', cancel);
    document.addEventListener('keydown', keydown);
    document.addEventListener('compositionstart', compositionStart);
    document.addEventListener('compositionend', compositionEnd);
    document.addEventListener('dragstart', dragStart);
    document.addEventListener('dragend', dragEnd);
    document.addEventListener('drop', dragEnd);
    document.addEventListener('pointerdown', pointerDown);
    document.addEventListener('pointerup', pointerUp);
    document.addEventListener('pointercancel', pointerUp);
    return () => {
      disposed = true; clearTimeout(timer);
      region.removeEventListener('wheel', wheel, true);
      window.removeEventListener('wheel', outsideWheel, true);
      window.removeEventListener('scroll', cancel, true);
      window.removeEventListener('resize', cancel);
      window.removeEventListener('blur', blur);
      window.removeEventListener('popstate', cancel);
      window.removeEventListener('hashchange', cancel);
      document.removeEventListener('visibilitychange', cancel);
      document.removeEventListener('keydown', keydown);
      document.removeEventListener('compositionstart', compositionStart);
      document.removeEventListener('compositionend', compositionEnd);
      document.removeEventListener('dragstart', dragStart);
      document.removeEventListener('dragend', dragEnd);
      document.removeEventListener('drop', dragEnd);
      document.removeEventListener('pointerdown', pointerDown);
      document.removeEventListener('pointerup', pointerUp);
      document.removeEventListener('pointercancel', pointerUp);
      recognizer.current.cancel(performance.now());
      if (lastPreview.current !== 0) {lastPreview.current = 0; latest.current.onPreview?.(0);}
    };
  }, [enabled]);

  return <div ref={regionRef} className={`space-gesture-region ${className}`.trim()}
    data-space-gesture-region={activeSpace} data-space-gesture-enabled={enabled}
    data-space-gesture-phase={phase}>{children}</div>;
}
