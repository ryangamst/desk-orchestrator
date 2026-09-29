"use strict";
(() => {
  const panel = document.getElementById('virtual-numpad');
  if (!panel) return;
  const keys = Array.from(panel.querySelectorAll('[data-virtual-key]'));
  const dial = document.getElementById('virtual-dial');
  const slider = document.getElementById('virtual-slider');
  const status = document.getElementById('virtual-input-status');
  const result = document.getElementById('virtual-input-result');
  const activeLabel = document.getElementById('virtual-active-task');
  let state = null, pending = false, online = false, polling = null;
  let lastInput = {key: -Infinity, control: -Infinity};
  let sliderPosition = 50;
  let dialPosition = 0;
  let sliderInteracting = false, wheelRemainder = 0;
  let physicalControls = {};
  const activityTimers = new Map();
  function rotateDial(steps) {
    // Keep the angle continuous across full turns to avoid spinning backwards.
    dialPosition += steps * 30;
    dial.querySelector('.wheel-face').style.transform = `rotate(${dialPosition}deg)`;
  }
  function moveSlider(position) {
    slider.value = String(Math.max(0, Math.min(100, position)));
    sliderPosition = Number(slider.value); // Match the native range's step rounding.
  }
  function activate(element) {
    clearTimeout(activityTimers.get(element));
    element.classList.add('is-control-active');
    activityTimers.set(element, setTimeout(() => {
      element.classList.remove('is-control-active');
      activityTimers.delete(element);
    }, 350));
  }
  function clearActivity() {
    for (const [element, timer] of activityTimers) {
      clearTimeout(timer);
      element.classList.remove('is-control-active');
    }
    activityTimers.clear();
    sliderInteracting = false;
    wheelRemainder = 0;
  }
  document.addEventListener('numpad-physical-controls', event => {
    const next = event.detail;
    for (const [name, element] of [['dial', dial], ['slider', slider]]) {
      const current = next[name], previous = physicalControls[name];
      element.classList.toggle('is-physical-active', Boolean(current?.active));
      element.classList.toggle('is-physical-pressed', Boolean(current?.pressed));
      if (!current) continue;
      const sameSession = previous?.token === current.token;
      const changed = !sameSession || previous.sequence !== current.sequence;
      if (!changed) continue;
      // A new/reconnected page shows only recent movement, never old turns.
      const delta = sameSession ? current.steps - previous.steps : current.active ? current.direction : 0;
      if (name === 'dial') {
        if (delta) rotateDial(delta);
      } else if (!sliderInteracting) {
        if (Number.isFinite(current.position)) moveSlider(current.position);
        else if (delta) moveSlider(sliderPosition + delta * 5);
      }
    }
    // Consume movement during a drag too; never replay it when the drag ends.
    physicalControls = next;
  });
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
    // Preserve an in-progress drag; movements while our request runs are dropped.
    slider.disabled = !online || !state || stale || (!pending && state.busy) || !state.controls.slider.enabled;
    dial.setAttribute('aria-disabled', String(!ready || !(state?.controls.dial.enabled || state?.controls.dial.switch_enabled)));
    dial.classList.toggle('mapped', Boolean(state?.controls.dial.enabled));
    for (const [name, element] of [['dial', dial], ['slider', slider]]) {
      const control = state?.controls[name];
      const source = control?.source_count ? ` · Source ${control.source_index} of ${control.source_count}` : '';
      const target = control?.enabled ? control.target + source : 'Unassigned';
      const help = name === 'dial'
        ? `Scroll over the dial or use arrow keys to turn.${control?.switch_enabled ? ' Click to switch target.' : ''}`
        : 'Drag or use arrow keys to move. Position indicates movement, not device volume.';
      element.title = `${name === 'dial' ? 'Dial' : 'Slider'} · ${target}. ${help}`;
      element.setAttribute('aria-label', element.title);
    }
    panel.setAttribute('aria-busy', String(pending || Boolean(state?.busy)));
    setText(status, !online ? 'Controller unavailable. Controls are disabled.' :
      pending ? 'Sending input… Additional presses are not queued.' :
      stale ? 'Updating mappings in the background…' :
      state?.busy ? 'Controller busy. Inputs are not queued.' : '');
    if (state) {
      setText(activeLabel, state.active_label);
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
  function canUseDial(direction) {
    return online && state && String(state.revision) === panel.dataset.revision &&
      (direction === 'switch' ? state.controls.dial.switch_enabled : state.controls.dial.enabled);
  }
  function useDial(direction) {
    if (!canUseDial(direction)) return;
    activate(dial);
    if (direction !== 'switch') rotateDial(direction === 'increase' ? 1 : -1);
    return send({kind: 'control', control: 'dial', direction}, dial);
  }
  // Native button activation remains accessible; holding a key never repeats a task.
  panel.addEventListener('keydown', event => {
    if (event.repeat && ['Enter', ' '].includes(event.key) && event.target.tagName === 'BUTTON') event.preventDefault();
  });
  dial.addEventListener('keydown', event => {
    if (['Enter', ' '].includes(event.key)) {
      event.preventDefault();
      if (!event.repeat) useDial('switch');
      return;
    }
    if (!['ArrowUp', 'ArrowRight', 'ArrowDown', 'ArrowLeft'].includes(event.key)) return;
    event.preventDefault();
    if (!event.repeat) useDial(['ArrowUp', 'ArrowRight'].includes(event.key) ? 'increase' : 'decrease');
  });
  dial.addEventListener('click', () => useDial('switch'));
  dial.addEventListener('wheel', event => {
    if (event.ctrlKey || !event.deltaY || !canUseDial('increase')) return;
    event.preventDefault();
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 120 : 1);
    if (Math.sign(delta) !== Math.sign(wheelRemainder)) wheelRemainder = 0;
    wheelRemainder += delta;
    if (Math.abs(wheelRemainder) < 24) return;
    const direction = wheelRemainder < 0 ? 'increase' : 'decrease';
    wheelRemainder = 0; // Consume the full gesture, including dropped busy inputs.
    return useDial(direction);
  }, {passive: false});
  dial.addEventListener('pointerleave', () => { wheelRemainder = 0; });
  slider.addEventListener('pointerdown', () => { sliderInteracting = true; });
  slider.addEventListener('keydown', event => {
    if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown'].includes(event.key)) sliderInteracting = true;
  });
  slider.addEventListener('keyup', () => { sliderInteracting = false; });
  slider.addEventListener('blur', () => { sliderInteracting = false; });
  window.addEventListener('pointerup', () => { sliderInteracting = false; });
  window.addEventListener('pointercancel', () => { sliderInteracting = false; });
  slider.addEventListener('input', () => {
    const next = Number(slider.value);
    const delta = next - sliderPosition;
    sliderPosition = next; // Consume skipped movement too; never accumulate a backlog.
    if (delta) {
      activate(slider);
      return send({kind: 'control', control: 'slider', direction: delta > 0 ? 'increase' : 'decrease'}, slider);
    }
  });
  window.addEventListener('pagehide', () => { online = false; clearActivity(); render(); });
  window.addEventListener('pageshow', () => refresh());
  document.addEventListener('visibilitychange', () => {
    online = false;
    clearActivity();
    render();
    if (!document.hidden) refresh();
  });
  refresh();
  setInterval(refresh, 1500);
})();
