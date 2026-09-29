"use strict";
(() => {
  const panel = document.getElementById('virtual-numpad');
  if (!panel?.dataset.keysUrl) return;
  const keys = Array.from(panel.querySelectorAll('[data-virtual-key]'));
  let polling = null, stopped = false;
  function show(pressed, controls = {}) {
    const down = new Set(pressed);
    for (const key of keys) {
      key.classList.toggle('is-physical-pressed', down.has(key.dataset.virtualKey));
    }
    document.dispatchEvent(new CustomEvent('numpad-physical-controls', {detail: controls}));
  }
  function clear() {
    polling?.abort();
    polling = null;
    show([]);
  }
  async function refresh() {
    if (document.hidden || stopped || polling) return;
    const controller = new AbortController();
    polling = controller;
    const timeout = setTimeout(() => controller.abort(), 1500);
    try {
      const response = await fetch(panel.dataset.keysUrl, {cache: 'no-store', signal: controller.signal});
      if (!response.ok || response.redirected || !response.headers.get('content-type')?.includes('application/json')) {
        throw new Error('Key feedback unavailable');
      }
      const data = await response.json();
      if (polling === controller) show(data.pressed_keys || [], data.controls || {});
    } catch (_) {
      if (polling === controller) show([]);
    } finally {
      clearTimeout(timeout);
      if (polling === controller) polling = null;
    }
  }
  window.addEventListener('pagehide', () => { stopped = true; clear(); });
  window.addEventListener('pageshow', () => { stopped = false; refresh(); });
  document.addEventListener('visibilitychange', () => {
    clear();
    if (!document.hidden) refresh();
  });
  refresh();
  setInterval(refresh, 150);
})();
