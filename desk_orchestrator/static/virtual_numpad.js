"use strict";
(() => {
  const panel = document.getElementById('virtual-numpad');
  if (!panel) return;
  const keys = Array.from(panel.querySelectorAll('[data-virtual-key]'));
  const controlButtons = Array.from(panel.querySelectorAll('[data-virtual-control]'));
  const dial = document.getElementById('virtual-dial');
  const slider = document.getElementById('virtual-slider');
  const status = document.getElementById('virtual-input-status');
  const result = document.getElementById('virtual-input-result');
  const activeLabel = document.getElementById('virtual-active-task');
  let state = null, pending = false, online = false, polling = null;
  let lastInput = {key: -Infinity, control: -Infinity};
  let sliderPosition = 50;
  let dialPosition = 0;
  function setText(element, text) {
    if (element.textContent !== text) element.textContent = text;
  }
  function available() {
    return online && state && !pending && !state.busy && String(state.revision) === panel.dataset.revision;
  }
  function render() {
    const ready = available();
    const stale = state && String(state.revision) !== panel.dataset.revision;
    for (const key of keys) {
      const command = !key.dataset.linkedTask && state?.key_commands?.[key.dataset.virtualKey];
      if (!key.dataset.linkedTask) {
        for (const name of Array.from(key.classList)) {
          if (name.startsWith('tone-')) key.classList.remove(name);
        }
        if (command && state.active_color) key.classList.add(`tone-${state.active_color}`);
        key.classList.toggle('is-active-task', Boolean(command));
      }
      if (command) {
        key.dataset.commandTask = state.active_task;
        key.dataset.keyCommand = command;
        key.dataset.taskLabel = state.active_label;
      } else {
        delete key.dataset.commandTask;
        delete key.dataset.keyCommand;
        if (!key.dataset.linkedTask) delete key.dataset.taskLabel;
      }
      key.disabled = !ready || !(key.dataset.linkedTask || command);
      key.classList.toggle('mapped', Boolean(key.dataset.linkedTask || command));
      const action = key.dataset.linkedTask ? `Run ${key.dataset.taskLabel}` : command || 'Unassigned';
      key.title = action;
      key.setAttribute('aria-label', `${key.textContent.trim()}: ${action}`);
    }
    for (const button of controlButtons) button.disabled = !ready || !state?.controls[button.dataset.virtualControl]?.enabled;
    // Preserve an in-progress drag; movements while our request runs are dropped.
    slider.disabled = !online || !state || stale || (!pending && state.busy) || !state.controls.slider.enabled;
    dial.setAttribute('aria-disabled', String(!ready || !state?.controls.dial.switch_enabled));
    dial.setAttribute('aria-label', `Switch wheel volume target. Current target: ${state?.controls.dial.target || 'Unassigned'}. Use arrow keys to turn the dial.`);
    dial.classList.toggle('mapped', Boolean(state?.controls.dial.enabled));
    panel.setAttribute('aria-busy', String(pending || Boolean(state?.busy)));
    setText(status, !online ? 'Controller unavailable. Controls are disabled.' :
      pending ? 'Sending input… Additional presses are not queued.' :
      stale ? 'Updating mappings in the background…' :
      state?.busy ? 'Controller busy. Inputs are not queued.' : '');
    if (state) {
      setText(activeLabel, state.active_label);
      for (const name of ['dial', 'slider']) {
        const control = state.controls[name];
        const position = control.source_count ? (control.source_index ? ` · Source ${control.source_index} of ${control.source_count}` : ' · Default') : '';
        setText(document.getElementById(`virtual-${name}-target`), control.enabled ? control.target + position : 'Unassigned');
      }
      for (const element of document.querySelectorAll('[data-linked-task]')) {
        element.classList.toggle('is-active-task', element.dataset.linkedTask === state.active_task);
      }
    }
    document.dispatchEvent(new Event('numpad-mappings-updated'));
  }
  async function readResponse(response) {
    if (response.redirected) {
      throw new Error('Sign in again, then reload the page. Input will not be retried.');
    }
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new Error(`Request failed (HTTP ${response.status}). Reload the page; input was not retried.`);
    }
    return response.json();
  }
  function applyState(next) {
    if (panel.dataset.layoutSignature && next.layout_signature && panel.dataset.layoutSignature !== next.layout_signature) {
      online = false;
      window.location.reload();
      return;
    }
    if (String(next.revision) !== panel.dataset.revision && next.overview) {
      // Replace configuration summaries only when they change. Keep the controls,
      // focus, slider gesture, and command result in the existing document.
      document.getElementById('overview-stats').innerHTML = next.overview.stats;
      document.getElementById('overview-tasks').innerHTML = next.overview.tasks;
      for (const key of keys) {
        const mapping = next.overview.mappings[key.dataset.virtualKey];
        for (const name of Array.from(key.classList)) {
          if (name.startsWith('tone-')) key.classList.remove(name);
        }
        key.classList.toggle('mapped', Boolean(mapping));
        if (mapping) {
          key.dataset.linkedTask = mapping.task;
          key.dataset.taskLabel = mapping.label;
          key.classList.add(`tone-${mapping.color}`);
        } else {
          delete key.dataset.linkedTask;
          delete key.dataset.taskLabel;
          key.classList.remove('is-active-task');
        }
        const action = mapping ? `Run ${mapping.label}` : 'Unassigned';
        key.title = action;
        key.setAttribute('aria-label', `${key.textContent.trim()}: ${action}`);
      }
      panel.dataset.revision = String(next.revision);
      document.dispatchEvent(new Event('overview-updated'));
    }
    if (next.active_task !== state?.active_task) {
      sliderPosition = 50;
      slider.value = '50';
    }
    state = next;
    online = true;
  }
  async function refresh(force = false) {
    if (document.hidden || (!force && (polling || pending))) return;
    polling?.abort();
    const controller = new AbortController();
    polling = controller;
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const url = new URL(panel.dataset.stateUrl, window.location.href);
      url.searchParams.set('revision', panel.dataset.revision);
      const response = await fetch(url, {cache: 'no-store', signal: controller.signal});
      const next = await readResponse(response);
      if (polling !== controller) return;
      if (!response.ok) throw new Error(next.error || 'Controller unavailable.');
      applyState(next);
    } catch (_) {
      if (polling === controller) online = false;
    } finally {
      clearTimeout(timeout);
      if (polling === controller) { polling = null; render(); }
    }
  }
  function requestId() {
    return Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16).padStart(8, '0')).join('');
  }
  async function send(input, element) {
    const now = performance.now();
    if (!available() || now - lastInput[input.kind] < (input.kind === 'key' ? 350 : 100)) return;
    if (input.kind === 'control' && !state.controls[input.control]?.enabled) return;
    if (input.direction === 'switch' && !state.controls.dial.switch_enabled) return;
    lastInput[input.kind] = now;
    const body = new URLSearchParams({...input, csrf: panel.dataset.csrf,
      revision: panel.dataset.revision, request_id: requestId(), active_task: state.active_task || ''});
    pending = true;
    // A poll started before the command must never overwrite its newer state.
    polling?.abort();
    polling = null;
    result.textContent = '';
    result.classList.remove('error-text');
    element?.classList.add('is-pressed');
    render();
    try {
      // Never retry a live input, including on connection loss or an HTTP error.
      const response = await fetch(panel.dataset.pressUrl, {method: 'POST', body});
      const data = await readResponse(response);
      if (!response.ok || !data.ok) throw new Error(data.error || 'Input was rejected.');
      result.textContent = data.message;
      if (input.kind === 'control' && input.control === 'dial' && input.direction !== 'switch') {
        dialPosition = (dialPosition + (input.direction === 'increase' ? 30 : -30)) % 360;
        dial.querySelector('.wheel-face').style.transform = `rotate(${dialPosition}deg)`;
      }
    } catch (error) {
      result.textContent = error instanceof TypeError ? 'Connection lost. The command may have run. Check Activity before trying again; nothing was retried.' : error.message;
      result.classList.add('error-text');
    } finally {
      // Keep controls disabled until a fresh controller state is obtained.
      await refresh(true);
      pending = false;
      element?.classList.remove('is-pressed');
      render();
    }
  }
  for (const key of keys) {
    key.addEventListener('click', () => send({kind: 'key', key: key.dataset.virtualKey}, key));
  }
  for (const button of controlButtons) {
    button.addEventListener('click', () => {
      const control = button.dataset.virtualControl;
      if (control === 'slider') {
        sliderPosition = Math.max(0, Math.min(100, sliderPosition + (button.dataset.direction === 'increase' ? 1 : -1)));
        slider.value = String(sliderPosition);
      }
      send({kind: 'control', control, direction: button.dataset.direction}, button);
    });
  }
  // Native button activation remains accessible; holding a key never repeats a task.
  panel.addEventListener('keydown', event => {
    if (event.repeat && ['Enter', ' '].includes(event.key) && event.target.tagName === 'BUTTON') event.preventDefault();
  });
  dial.addEventListener('keydown', event => {
    if (['Enter', ' '].includes(event.key)) {
      event.preventDefault();
      if (!event.repeat) send({kind: 'control', control: 'dial', direction: 'switch'}, dial);
      return;
    }
    if (!['ArrowUp', 'ArrowRight', 'ArrowDown', 'ArrowLeft'].includes(event.key)) return;
    event.preventDefault();
    if (!event.repeat) send({kind: 'control', control: 'dial', direction: ['ArrowUp', 'ArrowRight'].includes(event.key) ? 'increase' : 'decrease'}, dial);
  });
  dial.addEventListener('click', () => send({kind: 'control', control: 'dial', direction: 'switch'}, dial));
  dial.addEventListener('wheel', event => {
    if (document.activeElement !== dial) return;
    event.preventDefault();
    if (event.deltaY) send({kind: 'control', control: 'dial', direction: event.deltaY < 0 ? 'increase' : 'decrease'}, dial);
  }, {passive: false});
  slider.addEventListener('input', () => {
    const next = Number(slider.value);
    const delta = next - sliderPosition;
    sliderPosition = next; // Consume skipped movement too; never accumulate a backlog.
    if (delta) send({kind: 'control', control: 'slider', direction: delta > 0 ? 'increase' : 'decrease'}, slider);
  });
  window.addEventListener('pagehide', () => { online = false; render(); });
  window.addEventListener('pageshow', () => refresh());
  document.addEventListener('visibilitychange', () => {
    online = false;
    render();
    if (!document.hidden) refresh();
  });
  refresh();
  setInterval(refresh, 1500);
})();
