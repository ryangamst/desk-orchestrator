"use strict";
(() => {
  const root = document.getElementById('numpad-editor');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const clone = value => JSON.parse(JSON.stringify(value));
  let draft = JSON.parse($('layout-data').textContent);
  let sources = JSON.parse($('layout-sources').textContent), learningField = null;
  draft.controls ||= [];
  const assignments = JSON.parse($('layout-assignments').textContent);
  let selected = draft.keys[0]?.id, unit = 64, undo = [], redo = [], drag = null;
  let saved = fingerprint(), pending = false, token = '', timer = null, capture = null, generation = 0;
  const dialog = $('learn-dialog');
  const uid = () => 'KEY_CUSTOM_' + Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16).padStart(8, '0')).join('');
  function fingerprint() { return JSON.stringify([draft, $('layout-device').value, $('layout-grab').checked, sources]); }
  function elements(value = draft) { return [...value.keys, ...(value.controls || [])]; }
  function isControl(item) { return item && ['dial', 'slider'].includes(item.id); }
  function key() { return elements().find(k => k.id === selected); }
  function snapshot() { return clone({draft, sources}); }
  function restore(value) { draft = value.draft; sources = value.sources; }
  function tell(message, error = false) { $('editor-status').textContent = message; $('editor-status').classList.toggle('is-error', error); }
  function checkpoint() { undo.push(snapshot()); if (undo.length > 100) undo.shift(); redo = []; }
  function intersects(a, b) { return a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h; }
  function issue(value = draft) {
    if (!value.name.trim() || value.name.length > 80) return 'Enter a container name (80 characters maximum).';
    if (!Number.isFinite(value.width) || !Number.isFinite(value.height) || value.width < 1 || value.width > 32 || value.height < 1 || value.height > 16) return 'Container dimensions must be 1-32 by 1-16 units.';
    for (const n of [value.width, value.height]) if (n * 4 !== Math.round(n * 4)) return 'Use quarter-unit dimensions.';
    if (value.keys.length > 200) return 'Maximum 200 keys per layout.';
    const signals = new Set();
    const items = elements(value);
    for (let i = 0; i < items.length; i++) {
      const k = items[i];
      if (!k.label.trim() || k.label.length > 40) return 'Each key needs a label (40 characters maximum).';
      if (['x', 'y', 'w', 'h'].some(p => !Number.isFinite(k[p]) || k[p] * 4 !== Math.round(k[p] * 4))) return 'Use quarter-unit positions and sizes.';
      if (k.x < 0 || k.y < 0 || k.w < .25 || k.h < .25 || k.x + k.w > value.width || k.y + k.h > value.height) return `${k.label} extends outside the container.`;
      if (items.slice(i + 1).some(b => intersects(k, b))) return `${k.label} overlaps another element.`;
      if (isControl(k)) {
        if (k.w < 1 || k.h < 1) return 'Controls need at least one unit of width and height.';
        if (k.id === 'dial' && k.w !== k.h) return 'Dial width and height must match.';
      } else {
        if (k.code !== null && signals.has(k.code)) return 'Two keys use the same hardware signal.';
        if (k.code !== null) signals.add(k.code);
      }
    }
    return '';
  }
  function commit(change) {
    const previous = snapshot();
    change();
    const error = issue();
    if (error) { restore(previous); tell(error, true); render(false); return false; }
    undo.push(previous); if (undo.length > 100) undo.shift(); redo = [];
    render(); return true;
  }
  function fitText(button) {
    button.style.fontSize = '14px';
    const label = button.firstElementChild;
    while ((label.scrollHeight > button.clientHeight - 8 || label.scrollWidth > button.clientWidth - 8) && parseFloat(button.style.fontSize) > 8) {
      button.style.fontSize = `${parseFloat(button.style.fontSize) - 1}px`;
    }
  }
  function render(status = true) {
    if (!key()) selected = elements()[0]?.id;
    const board = $('layout-canvas');
    board.replaceChildren();
    Object.assign(board.style, {width: `${draft.width * unit}px`, height: `${draft.height * unit}px`});
    for (const k of elements()) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'layout-key'; b.dataset.id = k.id;
      b.classList.toggle('is-selected', k.id === selected); b.classList.toggle('is-mapped', isControl(k) ? !!sources[k.id] : k.code !== null);
      b.classList.toggle('is-conflict', elements().some(other => other.id !== k.id && intersects(k, other)));
      if (isControl(k)) { b.classList.add('editor-' + k.id); b.dataset.orientation = k.orientation || 'vertical'; }
      b.setAttribute('aria-pressed', String(k.id === selected));
      b.setAttribute('aria-label', `${k.label}, ${k.code === null ? 'unmapped' : 'signal ' + k.code}`);
      b.title = `${k.label}: ${k.code === null ? 'Unmapped' : 'Signal ' + k.code}`;
      if (isControl(k)) { b.title = k.label + (sources[k.id] ? ': Input configured' : ': Input disabled'); b.setAttribute('aria-label', b.title); }
      Object.assign(b.style, {left: `${k.x * unit + 2}px`, top: `${k.y * unit + 2}px`, width: `${Math.max(4, k.w * unit - 4)}px`, height: `${Math.max(4, k.h * unit - 4)}px`});
      if (!isControl(k)) {
        const label = document.createElement('span'); label.textContent = k.label; b.append(label);
      }
      const handle = document.createElement('span'); handle.className = 'layout-resize'; handle.title = 'Resize key'; b.append(handle);
      b.addEventListener('pointerdown', e => startDrag(e, k.id, e.target === handle));
      b.addEventListener('click', () => { if (selected !== k.id) { selected = k.id; render(); } });
      b.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); selected = k.id; render(); focusKey(); return; }
        if (e.key === 'Delete') { selected = k.id; remove(); return; }
        const delta = {ArrowLeft: [-.25, 0], ArrowRight: [.25, 0], ArrowUp: [0, -.25], ArrowDown: [0, .25]}[e.key];
        if (!delta) return;
        e.preventDefault(); selected = k.id;
        commit(() => { key().x += delta[0] * (e.shiftKey ? 4 : 1); key().y += delta[1] * (e.shiftKey ? 4 : 1); }); focusKey();
      });
      board.append(b); if (!isControl(k)) fitText(b);
    }
    for (const p of ['name', 'width', 'height']) $('layout-' + p).value = draft[p];
    const current = key(); $('key-empty').hidden = !!current; $('key-properties').hidden = !current;
    $('key-delete').disabled = !current;
    $('key-duplicate').disabled = !current || isControl(current);
    for (const type of ['dial', 'slider']) $(type + '-add').disabled = draft.controls.some(item => item.id === type);
    if (current) {
      for (const p of ['label', 'x', 'y', 'w', 'h']) $('key-' + p).value = current[p];
      $('key-signal').textContent = current.code === null ? 'Hardware signal: Unmapped' : `Hardware signal: ${current.code}`;
      $('key-assignments').textContent = assignments[current.id]?.length ? 'Assigned to: ' + assignments[current.id].join(', ') : 'No task assignments';
      $('selection-heading').textContent = current.id === 'dial' ? 'Rotary dial' : current.id === 'slider' ? 'Slider' : 'Selected key';
      $('key-input-properties').hidden = isControl(current);
      $('control-input-properties').hidden = !isControl(current);
      if (isControl(current)) renderSource(current);
    }
    $('layout-undo').disabled = !undo.length; $('layout-redo').disabled = !redo.length;
    if (status) tell(fingerprint() === saved ? 'Layout saved' : 'Unsaved changes');
  }
  function focusKey() { Array.from($('layout-canvas').children).find(b => b.dataset.id === selected)?.focus(); }
  function startDrag(event, id, resize) {
    if (event.button !== 0) return;
    event.preventDefault();
    selected = id;
    drag = {id, resize, x: event.clientX, y: event.clientY, old: snapshot(), original: clone(key()), changed: false};
    render(false); focusKey();
    $('layout-canvas').setPointerCapture(event.pointerId);
  }
  window.addEventListener('pointermove', e => {
    if (!drag) return;
    const dx = Math.round((e.clientX - drag.x) / unit * 4) / 4, dy = Math.round((e.clientY - drag.y) / unit * 4) / 4;
    const k = key(), original = drag.original;
    if (drag.resize) { k.w = Math.max(isControl(k) ? 1 : .25, original.w + dx); k.h = k.id === 'dial' ? k.w : Math.max(isControl(k) ? 1 : .25, original.h + dy); }
    else { k.x = original.x + dx; k.y = original.y + dy; }
    drag.changed = dx !== 0 || dy !== 0; render(false);
  });
  function endDrag(cancel = false) {
    if (!drag) return;
    const error = issue();
    if (cancel || error) restore(drag.old);
    else if (drag.changed) { undo.push(drag.old); redo = []; }
    drag = null; render(); focusKey(); if (error && !cancel) tell(error, true);
  }
  window.addEventListener('pointerup', () => endDrag());
  window.addEventListener('pointercancel', () => endDrag(true));
  window.addEventListener('blur', () => endDrag(true));
  function freePlace(w, h) {
    for (let y = 0; y + h <= draft.height; y += .25) for (let x = 0; x + w <= draft.width; x += .25) {
      if (!elements().some(k => intersects({x, y, w, h}, k))) return {x, y};
    }
    return null;
  }
  function add(copy = false) {
    const source = copy ? key() : {label: 'Key ' + (draft.keys.length + 1), w: 1, h: 1};
    if (!source) return;
    const place = freePlace(source.w, source.h);
    if (!place) { tell('No space for this key. Increase the container dimensions.', true); return; }
    const k = {...source, ...place, code: null, id: uid()};
    commit(() => { draft.keys.push(k); selected = k.id; }); focusKey();
  }
  function remove() {
    const current = key(); if (!current) return;
    if (assignments[current.id]?.length && !window.confirm(`Delete ${current.label}? Saving will remove its assignments for: ${assignments[current.id].join(', ')}.`)) return;
    if (isControl(current) && sources[current.id] && !window.confirm(`Remove ${current.label} and disable its hardware input for this profile? Task actions will be kept.`)) return;
    commit(() => { draft.keys = draft.keys.filter(k => k.id !== selected); draft.controls = draft.controls.filter(k => k.id !== selected); delete sources[selected]; selected = elements()[0]?.id; });
  }
  function addControl(type) {
    if (draft.controls.some(item => item.id === type)) return;
    const item = type === 'dial' ? {id:type, label:'Dial', w:2, h:2} : {id:type, label:'Slider', w:1, h:3, orientation:'vertical'};
    const place = freePlace(item.w, item.h);
    if (!place) { tell('No space for this control. Increase the container dimensions.', true); return; }
    commit(() => { draft.controls.push({...item, ...place}); selected = type; }); focusKey();
  }
  function renderSource(current) {
    const source = sources[current.id] || {}, mode = source.mode || '';
    $('slider-orientation-field').hidden = current.id !== 'slider';
    $('slider-orientation').value = current.orientation || 'vertical';
    $('control-gmmk-option').hidden = current.id !== 'slider';
    $('control-mode').value = mode;
    $('control-source-fields').hidden = !mode;
    $('control-device').value = source.device || $('layout-device').value;
    $('control-code').value = source.code ?? 0;
    $('control-code').max = mode === 'relative' ? 15 : mode === 'absolute' ? 63 : 767;
    $('control-code-label').textContent = mode === 'keys' ? 'Increase key code' : 'Axis code';
    $('control-down-code').value = source.down_code ?? 114;
    $('control-down-field').hidden = mode !== 'keys';
    $('control-invert').checked = source.invert || false;
    $('control-deadband').value = source.deadband ?? 4;
    $('control-threshold-field').hidden = !['absolute', 'gmmk_raw'].includes(mode);
    $('dial-click-fields').hidden = current.id !== 'dial';
    $('control-press-code').value = source.press_code ?? '';
    $('control-press-device').value = source.press_device || '';
    $('control-learn-code').disabled = mode === 'gmmk_raw';
    $('control-code').disabled = mode === 'gmmk_raw';
    $('control-learn-code').textContent = mode === 'keys' ? 'Learn increase' : 'Learn axis';
  }
  function readSource() {
    const mode = $('control-mode').value;
    if (!mode) { delete sources[selected]; return; }
    const source = {mode, device:$('control-device').value.trim(), code:Number($('control-code').value), invert:$('control-invert').checked};
    if (mode === 'keys') source.down_code = Number($('control-down-code').value);
    if (['absolute', 'gmmk_raw'].includes(mode)) source.deadband = Number($('control-deadband').value);
    if (selected === 'dial' && $('control-press-code').value !== '') {
      source.press_code = Number($('control-press-code').value);
      if ($('control-press-device').value.trim()) source.press_device = $('control-press-device').value.trim();
    }
    sources[selected] = source;
  }
  $('dial-add').onclick = () => addControl('dial');
  $('slider-add').onclick = () => addControl('slider');
  $('slider-orientation').onchange = e => commit(() => { key().orientation = e.target.value; [key().w, key().h] = [key().h, key().w]; });
  $('control-mode').onchange = e => {
    const mode = e.target.value;
    commit(() => {
      if (!mode) { delete sources[selected]; return; }
      const device = sources[selected]?.device || $('layout-device').value.trim();
      sources[selected] = {mode, device, code:mode === 'keys' ? 115 : mode === 'relative' ? 8 : 32};
      if (mode === 'keys') sources[selected].down_code = 114;
    });
  };
  for (const id of ['control-device','control-code','control-down-code','control-invert','control-deadband','control-press-code','control-press-device']) $(id).addEventListener('change', () => commit(readSource));
  $('key-add').onclick = () => add(); $('key-duplicate').onclick = () => add(true); $('key-delete').onclick = remove;
  $('layout-undo').onclick = () => { if (undo.length) { redo.push(snapshot()); restore(undo.pop()); render(); } };
  $('layout-redo').onclick = () => { if (redo.length) { undo.push(snapshot()); restore(redo.pop()); render(); } };
  for (const p of ['name', 'width', 'height']) $('layout-' + p).addEventListener('change', e => commit(() => { draft[p] = p === 'name' ? e.target.value : Number(e.target.value); }));
  for (const p of ['label', 'x', 'y', 'w', 'h']) $('key-' + p).addEventListener('change', e => commit(() => { key()[p] = p === 'label' ? e.target.value : Number(e.target.value); if (key().id === 'dial' && ['w','h'].includes(p)) key().w = key().h = Number(e.target.value); }));
  for (const id of ['layout-device', 'layout-grab']) $(id).addEventListener('change', () => tell('Unsaved changes'));
  $('key-clear').onclick = () => commit(() => { key().code = null; });
  $('layout-zoom').oninput = e => { unit = Number(e.target.value); $('zoom-value').textContent = `${Math.round(unit / 64 * 100)}%`; render(false); };
  $('layout-preset').onchange = e => {
    const preset = e.target.value; e.target.value = ''; if (!preset) return;
    if (elements().length && !window.confirm('Replace the current layout? Removed keys lose their assignments and removed controls have their inputs disabled when you save.')) return;
    checkpoint(); draft.width = 4; draft.height = 5; draft.keys = []; draft.controls = []; sources = {};
    if (preset === 'standard') {
      const positions = [['Num',0,0],['/',1,0],['*',2,0],['-',3,0],['7',0,1],['8',1,1],['9',2,1],['+',3,1,1,2],['4',0,2],['5',1,2],['6',2,2],['1',0,3],['2',1,3],['3',2,3],['Enter',3,3,1,2],['0',0,4,2,1],['.',2,4]];
      draft.keys = positions.map(([label,x,y,w=1,h=1]) => ({id:uid(),label,x,y,w,h,code:null}));
    }
    selected = draft.keys[0]?.id; render();
  };
  async function post(url, data, keepalive = false) {
    const response = await fetch(url, {method:'POST', body:new URLSearchParams({csrf:root.dataset.csrf, revision:root.dataset.revision, ...data}), keepalive});
    if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) throw new Error('Session expired. Sign in again and reload this page.');
    const result = await response.json(); if (!response.ok) throw new Error(result.error || 'Request failed.'); return result;
  }
  $('layout-save').onclick = async () => {
    if (pending) return;
    const error = issue(); if (error) { tell(error, true); return; }
    const removed = Object.keys(assignments).filter(id => JSON.parse($('layout-data').textContent).keys.some(k => k.id === id) && !draft.keys.some(k => k.id === id));
    if (removed.length && !window.confirm('Saving removes assignments for deleted keys in: ' + [...new Set(removed.flatMap(id => assignments[id]))].join(', ') + '. Continue?')) return;
    pending = true; $('layout-save').disabled = true;
    const submitting = fingerprint();
    try {
      const result = await post(root.dataset.saveUrl, {profile:JSON.stringify(draft), controls:JSON.stringify(sources), device:$('layout-device').value, grab:$('layout-grab').checked ? 'yes' : 'no', remove_assignments:removed.length ? 'yes' : 'no'});
      root.dataset.revision = result.revision; saved = submitting; $('layout-data').textContent = JSON.stringify(JSON.parse(submitting)[0]);
      window.history.replaceState(null, '', result.url); tell(fingerprint() === saved ? 'Layout saved' : 'Saved. Newer edits are unsaved.');
    } catch (error) { tell(error.message, true); }
    finally { pending = false; $('layout-save').disabled = false; }
  };
  async function stopLearning() {
    generation++; clearTimeout(timer); capture = null; $('learn-accept').disabled = true;
    const previous = token; token = '';
    if (previous) await post(root.dataset.learnUrl, {action:'cancel', token:previous}).catch(() => {});
  }
  function learningSignal() {
    return !learningField || learningField !== 'code' || sources[selected]?.mode === 'keys' ? 'key' : sources[selected]?.mode;
  }
  function learningDevice() {
    const source = sources[selected];
    return (learningField ? (learningField === 'press_code' ? source?.press_device || source?.device : source?.device) : $('layout-device').value)?.trim();
  }
  function captureConflict(code) {
    const device = learningDevice(), signal = learningSignal();
    if (signal === 'key' && device === $('layout-device').value.trim()) {
      const duplicate = draft.keys.find(k => k.id !== selected && k.code === code);
      if (duplicate) return duplicate.label;
    }
    for (const [id, source] of Object.entries(sources)) {
      for (const field of ['code','down_code','press_code']) {
        if (id === selected && field === learningField || source[field] === undefined) continue;
        const type = field === 'press_code' || source.mode === 'keys' ? 'key' : source.mode;
        const path = field === 'press_code' ? source.press_device || source.device : source.device;
        if (type === signal && path === device && source[field] === code) return `${id} ${field === 'press_code' ? 'click' : field === 'down_code' ? 'decrease' : 'increase / axis'}`;
      }
    }
    return null;
  }
  async function learn(field = null) {
    learningField = field;
    const startingGeneration = generation + 1;
    await stopLearning();
    if (generation !== startingGeneration) return;
    if (!key() || !learningDevice()) { tell('Select a hardware input and a layout element first.', true); return; }
    if (!dialog.open) dialog.showModal();
    const version = generation;
    $('learn-status').textContent = 'Connecting to the numpad listener...';
    try {
      const result = await post(root.dataset.learnUrl, {action:'start', device:learningDevice(), signal:learningSignal()});
      if (version !== generation) { await post(root.dataset.learnUrl, {action:'cancel', token:result.token}); return; }
      token = result.token;
      const poll = async () => {
        if (version !== generation || !token) return;
        try {
          const state = await post(root.dataset.learnUrl, {action:'poll', token});
          if (version !== generation) return;
          if (state.status === 'error' || state.status === 'cancelled') throw new Error(state.error || 'Learning cancelled.');
          if (state.status === 'captured') {
            const duplicate = captureConflict(state.code);
            capture = duplicate ? null : state.code;
            $('learn-status').textContent = duplicate ? `Signal ${state.code} is already mapped to ${duplicate}. Clear that mapping or retry.` : `${Array.isArray(state.name) ? state.name.join(' / ') : state.name} (${state.code}) detected for ${key().label}.`;
            $('learn-accept').disabled = !!duplicate;
          } else $('learn-status').textContent = state.status === 'listening' ? `Listening. ${learningField ? learningField === 'press_code' ? 'Press the dial.' : learningField === 'down_code' ? 'Move toward decrease.' : 'Move toward increase.' : 'Press the hardware key.'} (${key().label})` : 'Waiting for the numpad listener...';
          timer = setTimeout(poll, 400);
        } catch (error) { $('learn-status').textContent = error.message; await stopLearning(); }
      };
      await poll();
    } catch (error) { $('learn-status').textContent = error.message; }
  }
  $('key-learn').onclick = () => learn(); $('learn-retry').onclick = () => learn(learningField);
  $('control-learn-code').onclick = () => learn('code');
  $('control-learn-down').onclick = () => learn('down_code');
  $('control-learn-click').onclick = () => learn('press_code');
  $('learn-cancel').onclick = async () => { await stopLearning(); dialog.close(); };
  dialog.addEventListener('cancel', () => { stopLearning(); });
  $('learn-accept').onclick = async () => {
    if (capture === null) return;
    const code = capture, index = draft.keys.findIndex(k => k.id === selected);
    const acceptingGeneration = generation + 1;
    await stopLearning();
    if (generation !== acceptingGeneration) return;
    commit(() => { if (learningField) sources[selected][learningField] = code; else key().code = code; });
    if (!learningField && $('learn-next').checked && index + 1 < draft.keys.length) { selected = draft.keys[index + 1].id; render(); await learn(); }
    else dialog.close();
  };
  window.addEventListener('pagehide', () => { if (token) post(root.dataset.learnUrl, {action:'cancel', token}, true).catch(() => {}); });
  window.addEventListener('beforeunload', e => { if (fingerprint() !== saved) { e.preventDefault(); e.returnValue = ''; } });
  render();
})();
