export function workHref(intentId: string) {
  return `#continuity?intent=${encodeURIComponent(intentId)}`;
}
export function decisionHref(decisionId: string, intentId = '') {
  const params = new URLSearchParams({decision: decisionId});
  if (intentId) params.set('intent', intentId);
  return `#decisions?${params}`;
}
export function workEventsHref(intentId: string) {
  return `#events?intent=${encodeURIComponent(intentId)}`;
}
