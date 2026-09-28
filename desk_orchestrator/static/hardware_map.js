"use strict";
// Hardware page: one SVG diagram of the inventory.
// Edit mode changes a local draft of hardware details (added, renamed, deleted)
// and each device's `map` (position, ports, links), saved in one revision-checked
// request. Outside edit mode the page sends live tasks, device commands and numpad keys with the same
// rules as the virtual numpad: busy input is rejected and nothing is retried.
(() => {
  const root = document.getElementById('hardware-map');
  if (!root) return;
  const NS = 'http://www.w3.org/2000/svg';
  const NODE_W = 236, HEAD = 56, ROW = 28, PAD = 12, GRID = 10, MAX_COORD = 5000;
  const SIGNAL_COLORS = {video: '#94c2ff', audio: '#7ddacb', usb: '#c4a6ff', ir: '#efa8c2',
    network: '#efc37f', power: '#e6e2ee', other: '#a19cb2'};
  const TYPE_COLUMN = {Computing: 0, Numpad: 0, KVM: 1, Controller: 1, Monitor: 2, Audio: 3, Peripheral: 3, Other: 3};
  const METHOD_LABELS = {none: 'Inventory only', ir: 'Infrared', smartthings: 'SmartThings', usb_hid: 'USB keyboard / KVM', numpad: 'USB input'};
  const $ = id => document.getElementById(id);
  const svg = $('map-svg'), canvas = $('map-canvas'), inspector = $('map-inspector');
  const toolbar = $('map-toolbar'), editBar = $('map-edit-bar'), dirtyPill = $('map-dirty');
  const statusLine = $('map-status'), resultLine = $('map-result'), legend = $('map-legend');
  const editButton = $('map-edit');

  let model = JSON.parse($('map-model').textContent);
  let mode = 'operate';
  let saved = {}, draft = {};         // layouts: id -> {x, y, ports, links}
  let savedInfo = {}, draftInfo = {}; // hardware details: id -> {name, type, notes, method}
  let selection = null;       // {type: 'device', id, port?} | {type: 'link', owner, index}
  let hoverTask = null;
  let state = null, online = false, pending = false, polling = null, lastInput = -Infinity;
  let press = null, connect = null, remoteChanged = false, saving = false, frame = 0;
  let zoom = null;            // null until the first render chooses "fit"
  let size = {width: 900, height: 520};

  // ---------- helpers ----------
  const clone = value => JSON.parse(JSON.stringify(value));
  const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
  const snap = value => clamp(Math.round(value / GRID) * GRID, 0, MAX_COORD);
  function h(tag, attrs = {}, ...children) {
    const element = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key.startsWith('on')) element.addEventListener(key.slice(2), value);
      else if (key === 'class') element.className = value;
      else if (key === 'dataset') Object.assign(element.dataset, value);
      else if (key in element && typeof value !== 'string') element[key] = value;
      else element.setAttribute(key, value === true ? '' : value);
    }
    for (const child of children.flat()) {
      if (child !== null && child !== undefined && child !== false) element.append(child);
    }
    return element;
  }
  function s(tag, attrs = {}, ...children) {
    const element = document.createElementNS(NS, tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === 'dataset') Object.assign(element.dataset, value);
      else element.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child !== null && child !== undefined && child !== false) element.append(child);
    }
    return element;
  }
  function truncate(text, length) {
    return text.length > length ? text.slice(0, length - 1) + '…' : text;
  }
  function setText(element, text) {
    if (element.textContent !== text) element.textContent = text;
  }
  // In edit mode a device's details come from the draft, which can include
  // hardware added on this page that has not been saved (and has no commands yet).
  const NEW_DEVICE = {commands: [], action_choices: [], summary: 'Save to configure its controls.', map: null};
  function device(id) {
    const base = model.devices[id];
    if (mode !== 'edit') return base;
    const info = draftInfo[id];
    return info ? {...(base || NEW_DEVICE), ...info, isNew: !base} : undefined;
  }
  const detailsOf = info => ({name: info.name, type: info.type, notes: info.notes, method: info.method});
  const portsOf = (layouts, id) => layouts[id]?.ports || {};
  const deviceName = id => device(id)?.name || id;
  function portLabel(layouts, id, port) {
    return portsOf(layouts, id)[port]?.label || port;
  }
  function sameAction(a, b) {
    return Boolean(a && b) && JSON.stringify(a) === JSON.stringify(b);
  }
  function commandFor(action) {
    if (!action) return null;
    if (action.kind === 'ir') return `ir:${action.command}`;
    if (action.kind === 'monitor') return `input:${action.input}`;
    if (action.kind === 'kvm') return `kvm:${action.port}`;
    return null;
  }
  function slug(label, taken) {
    let base = label.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 40) || 'port';
    if (!/^[a-z]/.test(base)) base = `p_${base}`.slice(0, 40);
    let id = base, n = 2;
    while (taken.has(id)) id = `${base}_${n++}`;
    return id;
  }

  // ---------- layout model ----------
  function nodeHeight(layouts, id) {
    return HEAD + Math.max(1, Object.keys(portsOf(layouts, id)).length) * ROW + PAD;
  }
  function autoPlace(layouts, ids) {
    const bottoms = {};
    for (const [id, layout] of Object.entries(layouts)) {
      if (ids.includes(id)) continue;
      const column = Math.round((layout.x - 40) / 300);
      bottoms[column] = Math.max(bottoms[column] || 0, layout.y + nodeHeight(layouts, id));
    }
    const ordered = [...ids].sort((a, b) => (TYPE_COLUMN[device(a).type] ?? 3) - (TYPE_COLUMN[device(b).type] ?? 3)
      || deviceName(a).localeCompare(deviceName(b)));
    for (const id of ordered) {
      const column = TYPE_COLUMN[device(id).type] ?? 3;
      const y = bottoms[column] ? bottoms[column] + 36 : 40;
      layouts[id] = {...(layouts[id] || {ports: {}, links: []}), x: 40 + column * 300, y};
      bottoms[column] = y + nodeHeight(layouts, id);
    }
  }
  function layoutsFromModel() {
    const layouts = {}, unplaced = [];
    for (const [id, info] of Object.entries(model.devices)) {
      if (info.map && Number.isFinite(info.map.x) && Number.isFinite(info.map.y)) {
        layouts[id] = {x: info.map.x, y: info.map.y, ports: info.map.ports || {}, links: info.map.links || []};
      } else {
        // Never moved yet: keep any ports and connections, choose a position by type.
        if (info.map) layouts[id] = {ports: info.map.ports || {}, links: info.map.links || []};
        unplaced.push(id);
      }
    }
    autoPlace(layouts, unplaced);
    return layouts;
  }
  function loadModel(next) {
    model = next;
    root.dataset.revision = String(model.revision);
    savedInfo = Object.fromEntries(Object.entries(model.devices).map(([id, info]) => [id, detailsOf(info)]));
    draftInfo = clone(savedInfo);
    saved = layoutsFromModel();
    draft = clone(saved);
    remoteChanged = false;
    if (selection?.type === 'device' && !model.devices[selection.id]) selection = null;
    if (selection?.type === 'new' && mode !== 'edit') selection = null;
    if (selection?.type === 'link' && !draft[selection.owner]?.links?.[selection.index]) selection = null;
    renderAll();
  }
  const layouts = () => mode === 'edit' ? draft : saved;
  const dirty = () => JSON.stringify([draft, draftInfo]) !== JSON.stringify([saved, savedInfo]);
  function resetDraft() {
    draft = clone(saved);
    draftInfo = clone(savedInfo);
  }
  function allLinks(source = layouts()) {
    const links = [];
    for (const [owner, layout] of Object.entries(source)) {
      (layout.links || []).forEach((link, index) => links.push({owner, index, ...link}));
    }
    return links;
  }

  // ---------- task routes ----------
  function route(taskName) {
    const task = model.tasks[taskName];
    if (!task) return null;
    const devices = new Set(Object.keys(task.touches));
    const ports = new Set();
    for (const [id, list] of Object.entries(task.touches)) for (const port of list) ports.add(`${id}/${port}`);
    const links = new Set();
    for (const link of allLinks()) {
      const a = `${link.owner}/${link.port}`, b = `${link.to}/${link.to_port}`;
      const control = (link.owner === model.controller && devices.has(link.to)) ||
                      (link.to === model.controller && devices.has(link.owner));
      if (ports.has(a) || ports.has(b) || control) links.add(`${link.owner}#${link.index}`);
    }
    // The far end of a routed cable is part of the route (e.g. the computer on a KVM port).
    for (const link of allLinks()) {
      if (links.has(`${link.owner}#${link.index}`)) { devices.add(link.owner); devices.add(link.to); }
    }
    if (devices.size) devices.add(model.controller);
    return {name: taskName, color: task.color, devices, ports, links};
  }

  // ---------- SVG ----------
  function anchor(source, id, port, towardX) {
    const layout = source[id];
    const keys = Object.keys(layout.ports || {});
    const index = Math.max(0, keys.indexOf(port));
    const direction = layout.ports?.[port]?.direction || 'both';
    const y = layout.y + HEAD + index * ROW + ROW / 2;
    const side = direction === 'in' ? 'left' : direction === 'out' ? 'right' :
      (towardX < layout.x + NODE_W / 2 ? 'left' : 'right');
    return {x: side === 'left' ? layout.x : layout.x + NODE_W, y, side};
  }
  function curve(a, b) {
    const dx = Math.max(50, Math.abs(b.x - a.x) / 2);
    const c1 = a.x + (a.side === 'left' ? -dx : dx), c2 = b.x + (b.side === 'left' ? -dx : dx);
    return `M${a.x} ${a.y} C${c1} ${a.y} ${c2} ${b.y} ${b.x} ${b.y}`;
  }
  function center(source, id) {
    return source[id].x + NODE_W / 2;
  }
  function renderSvg() {
    const source = layouts();
    const focused = document.activeElement?.closest?.('[data-device]')?.dataset.device;
    let width = 900, height = 520;
    for (const id of Object.keys(source)) {
      width = Math.max(width, source[id].x + NODE_W + 120);
      height = Math.max(height, source[id].y + nodeHeight(source, id) + 80);
    }
    size = {width, height};
    if (zoom === null) zoom = fitZoom();
    svg.setAttribute('width', Math.round(width * zoom));
    svg.setAttribute('height', Math.round(height * zoom));
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    setText($('map-zoom-level'), `${Math.round(zoom * 100)}%`);
    const title = svg.querySelector('title');
    svg.replaceChildren(title);
    svg.classList.toggle('is-editing', mode === 'edit');
    const active = state?.active_task || null;
    const highlight = route(hoverTask) || null;
    const activeRoute = route(active);
    svg.classList.toggle('has-route', Boolean(highlight));
    const pattern = s('pattern', {id: 'map-grid', width: 20, height: 20, patternUnits: 'userSpaceOnUse'},
      s('circle', {cx: 1, cy: 1, r: 1, class: 'map-grid-dot'}));
    svg.append(s('defs', {}, pattern), s('rect', {class: 'map-background', width, height, fill: 'url(#map-grid)'}));

    const linkLayer = s('g', {class: 'map-links'});
    for (const link of allLinks(source)) {
      if (!source[link.to]?.ports?.[link.to_port] || !source[link.owner]?.ports?.[link.port]) continue;
      const a = anchor(source, link.owner, link.port, center(source, link.to));
      const b = anchor(source, link.to, link.to_port, center(source, link.owner));
      const signal = source[link.owner].ports[link.port].signal;
      const key = `${link.owner}#${link.index}`;
      const selected = selection?.type === 'link' && selection.owner === link.owner && selection.index === link.index;
      const classes = ['map-link', `signal-${signal}`,
        highlight?.links.has(key) ? `is-route tone-${highlight.color}` : '',
        !highlight && activeRoute?.links.has(key) ? `is-active-route tone-${activeRoute.color}` : '',
        selected ? 'is-selected' : ''].filter(Boolean).join(' ');
      const d = curve(a, b);
      linkLayer.append(s('g', {class: classes, dataset: {owner: link.owner, index: link.index}},
        s('path', {class: 'map-link-hit', d}),
        s('path', {class: 'map-link-line', d, stroke: SIGNAL_COLORS[signal] || SIGNAL_COLORS.other}),
        s('title', {}, `${deviceName(link.owner)} · ${portLabel(source, link.owner, link.port)} → ${deviceName(link.to)} · ${portLabel(source, link.to, link.to_port)}`)));
    }
    svg.append(linkLayer);

    const nodeLayer = s('g', {class: 'map-nodes'});
    const topmost = press?.id || (selection?.type === 'device' ? selection.id : null);
    const order = Object.entries(source).sort(([a], [b]) => (a === topmost) - (b === topmost));
    for (const [id, layout] of order) {
      const info = device(id);
      if (!info) continue;
      const ports = Object.entries(layout.ports || {});
      const nodeClasses = ['map-node', id === model.controller ? 'is-controller' : '', info.fixed === 'numpad' ? 'is-numpad' : '',
        highlight?.devices.has(id) ? `is-route tone-${highlight.color}` : '',
        !highlight && activeRoute?.devices.has(id) ? `is-active-route tone-${activeRoute.color}` : '',
        selection?.type === 'device' && selection.id === id ? 'is-selected' : ''].filter(Boolean).join(' ');
      const nodeHeightValue = nodeHeight(source, id);
      const node = s('g', {class: nodeClasses, transform: `translate(${layout.x} ${layout.y})`, tabindex: 0,
        role: 'button', 'aria-label': `${info.name}, ${info.type}. ${ports.length} ports. ${mode === 'edit' ? 'Arrow keys move; Enter selects.' : 'Enter shows commands.'}`,
        dataset: {device: id}},
        s('rect', {class: 'map-node-body', width: NODE_W, height: nodeHeightValue, rx: 12}),
        s('rect', {class: 'map-node-head', width: NODE_W, height: HEAD - 6, rx: 12}),
        s('text', {class: 'map-node-title', x: 16, y: 25}, truncate(info.name, 26)),
        s('text', {class: 'map-node-sub', x: 16, y: 43}, truncate(`${info.type} · ${METHOD_LABELS[info.method] || info.method}`, 34)));
      if (!ports.length) {
        node.append(s('text', {class: 'map-port-empty', x: 16, y: HEAD + 18}, mode === 'edit' ? 'Select to add ports' : 'No ports mapped'));
      }
      ports.forEach(([portId, port], index) => {
        const key = `${id}/${portId}`;
        const portClasses = ['map-port', port.action ? 'has-action' : '',
          highlight?.ports.has(key) ? 'is-route' : '', !highlight && activeRoute?.ports.has(key) ? 'is-active-route' : '',
          selection?.type === 'device' && selection.id === id && selection.port === portId ? 'is-selected' : ''].filter(Boolean).join(' ');
        const color = SIGNAL_COLORS[port.signal] || SIGNAL_COLORS.other;
        const right = port.direction === 'out';
        const group = s('g', {class: portClasses, transform: `translate(0 ${HEAD + index * ROW})`, dataset: {port: portId}},
          s('rect', {class: 'map-port-row', x: 8, y: 2, width: NODE_W - 16, height: ROW - 4, rx: 7}),
          s('text', {class: 'map-port-label', x: right ? NODE_W - 18 : 18, y: ROW / 2 + 4, 'text-anchor': right ? 'end' : 'start'},
            truncate(`${port.action ? '▸ ' : ''}${port.label}`, 28)),
          s('title', {}, `${port.label} · ${model.signals[port.signal] || port.signal} ${(model.directions[port.direction] || '').toLowerCase()}${port.action ? ' · runs a command' : ''}`));
        const sides = port.direction === 'both' ? [0, NODE_W] : [right ? NODE_W : 0];
        for (const cx of sides) {
          group.append(s('circle', {class: 'map-port-dot', cx, cy: ROW / 2, r: 6, fill: color, dataset: {portDot: portId}}));
        }
        node.append(group);
      });
      nodeLayer.append(node);
    }
    svg.append(nodeLayer);
    if (connect) {
      svg.append(s('path', {class: 'map-link-draft', d: curve(connect.from, connect.to)}));
    }
    if (focused) svg.querySelector(`[data-device="${CSS.escape(focused)}"]`)?.focus({preventScroll: true});
  }
  function scheduleSvg() {
    if (frame) return;
    frame = requestAnimationFrame(() => { frame = 0; renderSvg(); });
  }

  function fitZoom() {
    const available = canvas.clientWidth - 2;
    return available > 0 ? clamp(Math.floor(available / size.width * 20) / 20, 0.7, 1) : 1;
  }
  function setZoom(next) {
    const middle = {x: (canvas.scrollLeft + canvas.clientWidth / 2) / zoom, y: (canvas.scrollTop + canvas.clientHeight / 2) / zoom};
    zoom = clamp(Math.round(next * 20) / 20, 0.5, 1.5);
    renderSvg();
    canvas.scrollLeft = middle.x * zoom - canvas.clientWidth / 2;
    canvas.scrollTop = middle.y * zoom - canvas.clientHeight / 2;
  }

  // ---------- legend ----------
  function renderLegend() {
    legend.replaceChildren(...Object.entries(model.signals).map(([key, label]) =>
      h('span', {class: 'map-legend-item'}, h('span', {class: `map-legend-swatch signal-${key}`, 'aria-hidden': 'true'}), label)),
      h('span', {class: 'map-legend-item'}, h('span', {class: 'map-legend-action', 'aria-hidden': 'true'}, '▸'), 'Port runs a command'));
  }

  // ---------- inspector: operate ----------
  function liveButton(label, onclick, {class: extraClass = '', ...extra} = {}) {
    const button = h('button', {type: 'button', ...extra, class: `button small map-live ${extraClass}`.trim(), onclick: () => onclick(button)}, label);
    return button;
  }
  function traceRoute(name) {
    const on = () => { hoverTask = name; scheduleSvg(); }, off = () => { hoverTask = null; scheduleSvg(); };
    return {onmouseenter: on, onmouseleave: off, onfocus: on, onblur: off};
  }
  function pressKey(key, button) {
    // Same path as the Overview's virtual numpad: a bound task key, or the active task's key mapping.
    send(root.dataset.pressUrl, {kind: 'key', key, active_task: state?.active_task || ''}, button);
  }
  function numpadPanel() {
    const parts = [h('h3', {}, 'Task keys')];
    const bound = Object.entries(model.keys).filter(([key]) => model.bindings[key] && model.tasks[model.bindings[key]]);
    parts.push(bound.length ? h('ul', {class: 'map-list map-keys'}, bound.map(([key, label]) => {
      const name = model.bindings[key], task = model.tasks[name];
      return h('li', {class: `tone-${task.color}`}, h('kbd', {}, label), h('span', {}, task.label),
        liveButton('Press', b => pressKey(key, b), {'aria-label': `Press numpad ${label}: run ${task.label}`, ...traceRoute(name)}));
    })) : h('p', {class: 'hint'}, 'No keys run tasks yet. Assign keys when editing a task.'));
    parts.push(h('h3', {}, 'Active task keys'),
      h('p', {class: 'hint'}, 'Active task: ', h('strong', {}, state?.active_label || 'None')));
    const commands = Object.entries(state?.key_commands || {});
    parts.push(commands.length ? h('ul', {class: 'map-list map-keys'}, commands.map(([key, text]) => {
      const label = model.keys[key] || key;
      return h('li', {class: state?.active_color ? `tone-${state.active_color}` : ''}, h('kbd', {}, label), h('span', {}, text),
        liveButton('Press', b => pressKey(key, b), {'aria-label': `Press numpad ${label}: ${text}`}));
    })) : h('p', {class: 'hint'}, state?.active_task ? 'The active task has no key mappings.' : 'Key mappings follow the last task sent.'));
    const controls = Object.entries(state?.controls || {});
    if (controls.length) {
      parts.push(h('h3', {}, 'Dial and slider'), h('ul', {class: 'map-list'}, controls.map(([name, control]) =>
        h('li', {}, h('span', {}, name === 'dial' ? 'Wheel' : 'Slider'), h('span', {class: 'hint'}, control.enabled ? control.target : 'Unassigned')))));
    }
    return parts;
  }
  function runCommand(id, command, button) {
    send(root.dataset.runUrl, {kind: 'command', device: id, command}, button);
  }

  function linkRows(source, id, editable) {
    const rows = [];
    for (const link of allLinks(source)) {
      if (link.owner !== id && link.to !== id) continue;
      const outgoing = link.owner === id;
      const text = outgoing ?
        `${portLabel(source, id, link.port)} → ${deviceName(link.to)} · ${portLabel(source, link.to, link.to_port)}` :
        `${portLabel(source, id, link.to_port)} ← ${deviceName(link.owner)} · ${portLabel(source, link.owner, link.port)}`;
      rows.push(h('li', {}, h('span', {}, text), editable ?
        h('button', {type: 'button', class: 'text-button danger', 'aria-label': `Remove connection ${text}`,
          onclick: () => removeLink(link.owner, link.index)}, 'Remove') : null));
    }
    return rows.length ? h('ul', {class: 'map-list'}, rows) : h('p', {class: 'hint'}, 'No connections.');
  }
  function usedBy(id) {
    return Object.entries(model.tasks).filter(([, task]) => id in task.touches);
  }
  function dependents(id) {
    return Object.values(model.tasks).filter(task => task.uses.includes(id)).map(task => task.label);
  }
  const urlFor = (template, id) => template.replace('__ID__', encodeURIComponent(id));
  function controlLinks(id, info) {
    if (info.fixed === 'numpad') return h('p', {class: 'map-links-row'}, h('a', {href: root.dataset.numpadUrl}, 'Numpad settings ↗'));
    if (info.isNew) return h('p', {class: 'hint'}, 'Save, then configure its control settings (IR commands, SmartThings inputs, or KVM connection).');
    return h('p', {class: 'map-links-row'},
      h('a', {href: urlFor(root.dataset.hardwareUrl, id)}, id === model.controller ? 'Name and notes ↗' : 'Control settings ↗'),
      info.method === 'ir' ? h('a', {href: urlFor(root.dataset.irUrl, id)}, 'Add & learn IR commands ↗') : null);
  }
  function operateDevice(id, focusPort) {
    const info = device(id), layout = saved[id];
    const parts = [h('div', {class: 'section-heading'}, h('h2', {}, info.name), h('span', {class: 'pill neutral'}, info.type)),
      h('p', {class: 'hint'}, `${METHOD_LABELS[info.method] || info.method} · ${info.summary}`),
      info.notes ? h('p', {class: 'map-notes'}, info.notes) : null];
    if (info.fixed === 'numpad') {
      return [...parts, ...numpadPanel(), controlLinks(id, info),
        h('p', {class: 'hint map-note'}, 'Presses operate real hardware, exactly like the physical numpad. Busy inputs are rejected, never queued or retried.')];
    }
    const port = focusPort && layout.ports[focusPort];
    if (port) {
      const command = commandFor(port.action);
      parts.push(h('div', {class: 'map-port-card'},
        h('strong', {}, port.label),
        h('p', {class: 'hint'}, `${model.signals[port.signal]} · ${model.directions[port.direction]}`),
        command ? liveButton(`Send: ${info.commands.find(c => c.id === command)?.label || port.label}`, b => runCommand(id, command, b), {class: 'primary'}) :
          h('p', {class: 'hint'}, 'This port has no command. Add one in Edit map.')));
    }
    parts.push(h('h3', {}, 'Commands'));
    parts.push(info.commands.length ? h('div', {class: 'map-command-list'},
      info.commands.map(command => liveButton(command.label, b => runCommand(id, command.id, b)))) :
      h('p', {class: 'hint'}, info.method === 'none' ? 'Inventory-only hardware has no commands.' : 'No saved commands yet.'));
    parts.push(h('h3', {}, 'Connections'), linkRows(saved, id, false));
    const tasks = usedBy(id);
    parts.push(h('h3', {}, 'Used by tasks'), tasks.length ? h('div', {class: 'map-command-list'},
      tasks.map(([name, task]) => liveButton(`Run ${task.label}`, b => send(root.dataset.runUrl, {kind: 'task', task: name}, b),
        {class: `tone-${task.color} map-task-button`, ...traceRoute(name)}))) : h('p', {class: 'hint'}, 'No task actions use this device.'));
    parts.push(controlLinks(id, info));
    parts.push(h('p', {class: 'hint map-note'}, 'Commands operate real hardware. The map shows saved configuration and the last task sent, not measured state. Busy inputs are rejected, never queued or retried.'));
    return parts;
  }
  function operateLink(owner, index) {
    const link = saved[owner]?.links?.[index];
    if (!link) return null;
    const port = saved[owner].ports[link.port];
    return [h('h2', {}, 'Connection'),
      h('p', {}, `${deviceName(owner)} · ${port.label} → ${deviceName(link.to)} · ${portLabel(saved, link.to, link.to_port)}`),
      h('p', {class: 'hint'}, model.signals[port.signal]),
      h('div', {class: 'map-command-list'},
        h('button', {type: 'button', class: 'button small', onclick: () => select({type: 'device', id: owner})}, `Show ${deviceName(owner)}`),
        h('button', {type: 'button', class: 'button small', onclick: () => select({type: 'device', id: link.to})}, `Show ${deviceName(link.to)}`))];
  }

  // ---------- inspector: edit ----------
  function suggestions(id) {
    const info = device(id), ports = draft[id].ports, existing = new Set(Object.values(ports).map(p => p.label.toLowerCase()));
    const pretty = name => name.replace(/_/g, ' ').replace(/\bhdmi\s?(\d*)/i, (_, n) => `HDMI ${n}`.trim())
      .replace(/\bdisplayport\b/i, 'DisplayPort').replace(/\busb\b/i, 'USB').replace(/\brca\b/i, 'RCA')
      .replace(/^./, c => c.toUpperCase());
    const list = [];
    if (info.method === 'smartthings') {
      for (const choice of info.action_choices) list.push({label: pretty(choice.value.input), signal: 'video', direction: 'in', action: choice.value});
    } else if (info.method === 'usb_hid') {
      for (const choice of info.action_choices) list.push({label: `Computer ${choice.value.port}`, signal: 'usb', direction: 'in', action: choice.value});
      list.push({label: 'Keyboard input', signal: 'usb', direction: 'in'}, {label: 'Video out', signal: 'video', direction: 'out'},
        {label: 'USB devices out', signal: 'usb', direction: 'out'});
    } else if (info.method === 'ir') {
      list.push({label: 'IR receiver', signal: 'ir', direction: 'in'});
    } else if (info.type === 'Computing') {
      list.push({label: 'Video out', signal: 'video', direction: 'out'}, {label: 'USB', signal: 'usb', direction: 'both'});
    }
    return list.filter(port => !existing.has(port.label.toLowerCase()) &&
      !(port.action && Object.values(ports).some(p => sameAction(p.action, port.action))));
  }
  function overlaps(a, b) {
    return a.x < b.x + NODE_W && b.x < a.x + NODE_W && a.y < b.y + b.h && b.y < a.y + a.h;
  }
  function makeRoom(id) {
    // Push devices below a node that grew, so nodes never cover each other's ports.
    const queue = [id];
    while (queue.length) {
      const current = queue.shift();
      const box = {...draft[current], h: nodeHeight(draft, current) + 24};
      for (const [other, layout] of Object.entries(draft)) {
        if (other === current || layout.y < draft[current].y) continue;
        if (overlaps(box, {...layout, h: nodeHeight(draft, other)})) {
          layout.y = snap(box.y + box.h);
          queue.push(other);
        }
      }
    }
  }
  function addPorts(id, list) {
    const ports = draft[id].ports;
    for (const port of list) {
      if (Object.keys(ports).length >= 32) break;
      ports[slug(port.label, new Set(Object.keys(ports)))] = clone(port);
    }
    makeRoom(id);
    changed(true);
  }
  function removePort(id, portId) {
    delete draft[id].ports[portId];
    draft[id].links = draft[id].links.filter(link => link.port !== portId);
    for (const layout of Object.values(draft)) {
      layout.links = layout.links.filter(link => !(link.to === id && link.to_port === portId));
    }
    if (selection?.type === 'link') selection = null;
    changed(true);
  }
  function movePort(id, portId, delta) {
    const entries = Object.entries(draft[id].ports);
    const index = entries.findIndex(([key]) => key === portId), target = index + delta;
    if (target < 0 || target >= entries.length) return;
    [entries[index], entries[target]] = [entries[target], entries[index]];
    draft[id].ports = Object.fromEntries(entries);
    changed(true);
    inspector.querySelector(`[data-port-editor="${CSS.escape(portId)}"] [data-move="${delta}"]`)?.focus();
  }
  function addLink(fromId, fromPort, toId, toPort) {
    if (fromId === toId && fromPort === toPort) return 'A port cannot connect to itself.';
    // Links are stored on the device owning the output side when directions say so.
    const a = draft[fromId].ports[fromPort], b = draft[toId].ports[toPort];
    if (!a || !b) return 'Choose two ports.';
    if (a.direction === 'in' && b.direction !== 'in') [fromId, fromPort, toId, toPort] = [toId, toPort, fromId, fromPort];
    const exists = allLinks(draft).some(link =>
      (link.owner === fromId && link.port === fromPort && link.to === toId && link.to_port === toPort) ||
      (link.owner === toId && link.port === toPort && link.to === fromId && link.to_port === fromPort));
    if (exists) return 'Those ports are already connected.';
    if (draft[fromId].links.length >= 64) return 'This device has the maximum number of connections.';
    draft[fromId].links.push({port: fromPort, to: toId, to_port: toPort});
    changed(true);
    const signalA = draft[fromId].ports[fromPort].signal, signalB = draft[toId].ports[toPort].signal;
    return signalA === signalB ? null : `Connected. Note: ${model.signals[signalA]} to ${model.signals[signalB]}.`;
  }
  function removeLink(owner, index) {
    draft[owner].links.splice(index, 1);
    if (selection?.type === 'link') selection = null;
    changed(true);
  }
  function select_(options, value, label) {
    return h('select', {'aria-label': label}, options.map(([key, text]) => h('option', {value: key, selected: key === value}, text)));
  }
  function portEditor(id, portId, port, index, count) {
    const info = device(id);
    const title = h('span', {}, port.label);
    const swatch = h('span', {class: `map-legend-swatch signal-${port.signal}`, 'aria-hidden': 'true'});
    const label = h('input', {value: port.label, maxlength: 60, required: true, 'aria-label': 'Port name',
      oninput: () => {
        if (label.value.trim()) { port.label = label.value; title.textContent = label.value; }
        changed(false);
      }});
    const signal = select_(Object.entries(model.signals), port.signal, 'Signal type');
    signal.addEventListener('change', () => { port.signal = signal.value; swatch.className = `map-legend-swatch signal-${port.signal}`; changed(false); });
    const direction = select_(Object.entries(model.directions), port.direction, 'Direction');
    direction.addEventListener('change', () => { port.direction = direction.value; changed(false); });
    const choices = info.action_choices;
    const current = choices.findIndex(choice => sameAction(choice.value, port.action));
    const action = select_([['', 'No command'], ...choices.map((choice, i) => [String(i), choice.label])],
      current >= 0 ? String(current) : '', 'Command this port runs');
    action.addEventListener('change', () => {
      if (action.value === '') delete port.action; else port.action = clone(choices[Number(action.value)].value);
      changed(false);
    });
    return h('fieldset', {class: `map-port-editor ${selection?.port === portId ? 'is-selected' : ''}`, dataset: {portEditor: portId}},
      h('legend', {}, swatch, title),
      h('label', {}, 'Name', label),
      h('div', {class: 'map-port-grid'}, h('label', {}, 'Signal', signal), h('label', {}, 'Direction', direction)),
      choices.length ? h('label', {}, 'Runs command', action) : null,
      h('div', {class: 'map-port-actions'},
        h('button', {type: 'button', class: 'text-button', disabled: index === 0, dataset: {move: -1}, 'aria-label': `Move ${port.label} up`, onclick: () => movePort(id, portId, -1)}, '↑ Up'),
        h('button', {type: 'button', class: 'text-button', disabled: index === count - 1, dataset: {move: 1}, 'aria-label': `Move ${port.label} down`, onclick: () => movePort(id, portId, 1)}, '↓ Down'),
        h('button', {type: 'button', class: 'text-button danger', 'aria-label': `Remove port ${port.label}`, onclick: () => removePort(id, portId)}, 'Remove')));
  }
  function connectForm(id) {
    const own = Object.entries(draft[id].ports);
    const others = Object.keys(draft).filter(other => Object.keys(draft[other].ports).length && other !== id)
      .sort((a, b) => deviceName(a).localeCompare(deviceName(b)));
    if (!own.length || !others.length) return h('p', {class: 'hint'}, 'Add ports here and on another device to connect them.');
    const from = select_(own.map(([key, port]) => [key, port.label]), own[0][0], 'From port');
    const to = select_(others.map(other => [other, deviceName(other)]), others[0], 'Connect to device');
    const toPort = h('select', {'aria-label': 'Connect to port'});
    const note = h('p', {class: 'hint', role: 'status'});
    const fill = () => toPort.replaceChildren(...Object.entries(draft[to.value].ports).map(([key, port]) => h('option', {value: key}, port.label)));
    to.addEventListener('change', fill);
    fill();
    return h('div', {class: 'map-connect'}, h('label', {}, 'From port', from), h('label', {}, 'To device', to), h('label', {}, 'To port', toPort),
      h('button', {type: 'button', class: 'button small', onclick: () => {
        const message = addLink(id, from.value, to.value, toPort.value);
        if (message) resultMessage(message, !message.startsWith('Connected'));
      }}, 'Add connection'), note);
  }
  function editDevice(id) {
    const info = device(id), ports = Object.entries(draft[id].ports);
    const ideas = suggestions(id);
    return [...detailsEditor(id, info),
      h('h3', {}, `Ports (${ports.length})`),
      ports.length ? h('div', {class: 'map-port-editors'}, ports.map(([key, port], index) => portEditor(id, key, port, index, ports.length))) :
        h('p', {class: 'hint'}, 'No ports yet.'),
      h('div', {class: 'map-command-list'},
        h('button', {type: 'button', class: 'button small', disabled: ports.length >= 32,
          onclick: () => { addPorts(id, [{label: `Port ${ports.length + 1}`, signal: 'other', direction: 'both'}]); focusLastPort(); }}, '＋ Add port'),
        ideas.length ? h('button', {type: 'button', class: 'button small', onclick: () => addPorts(id, ideas),
          title: ideas.map(p => p.label).join(', ')}, `＋ Add suggested ports (${ideas.length})`) : null),
      h('h3', {}, 'Connections'), linkRows(draft, id, true),
      h('h3', {}, 'Add a connection'), h('p', {class: 'hint'}, 'Or drag from one port’s dot to another on the diagram.'), connectForm(id),
      deleteSection(id, info)];
  }
  function detailsEditor(id, info) {
    if (info.fixed === 'numpad') {
      return [h('div', {class: 'section-heading'}, h('h2', {}, info.name), h('span', {class: 'pill neutral'}, 'Numpad')),
        h('p', {class: 'hint'}, `${info.notes} · ${info.summary}`),
        h('p', {class: 'hint'}, 'The numpad is chosen on the Numpad page. Here you can place it and edit its ports and connections.'),
        controlLinks(id, info)];
    }
    const controller = id === model.controller;
    const heading = h('h2', {}, info.name);
    const name = h('input', {value: info.name, maxlength: 100, required: true, 'aria-label': 'Hardware name',
      oninput: () => {
        if (!name.value.trim()) return;
        draftInfo[id].name = name.value;
        heading.textContent = name.value;
        changed(false);
      }});
    let type;
    if (controller) {
      type = h('input', {value: 'Controller', disabled: true, 'aria-label': 'Hardware type'});
    } else {
      type = select_(model.types.map(value => [value, value]), info.type, 'Hardware type');
      type.addEventListener('change', () => { draftInfo[id].type = type.value; changed(false); });
    }
    const notes = h('textarea', {rows: 3, maxlength: 2000, 'aria-label': 'Connection notes',
      placeholder: 'Cables, ports, and anything you’ll want to remember…',
      oninput: () => { draftInfo[id].notes = notes.value; renderEditBar(); }});
    notes.value = info.notes;
    return [h('div', {class: 'section-heading'}, heading, info.isNew ? h('span', {class: 'pill amber'}, 'Not saved yet') : null),
      h('div', {class: 'map-details'},
        h('label', {}, 'Name', name),
        h('div', {class: 'map-port-grid'},
          h('label', {}, 'Type', type),
          h('label', {}, 'Control method', h('input', {value: model.methods[info.method] || info.method, disabled: true}))),
        h('label', {}, 'Connection notes', notes)),
      h('p', {class: 'hint'}, `ID ${id} · ${info.summary}`),
      controlLinks(id, info)];
  }
  function deleteSection(id, info) {
    if (info.fixed === 'numpad') return h('p', {class: 'hint map-delete'}, 'The numpad appears while one is selected on the Numpad page, and can’t be deleted here.');
    if (id === model.controller) return h('p', {class: 'hint map-delete'}, 'The Raspberry Pi controller is always on the map and cannot be deleted.');
    const tasks = info.isNew ? [] : dependents(id);
    return h('div', {class: 'map-delete'}, h('h3', {}, 'Delete'),
      tasks.length ? h('p', {class: 'hint'}, `Used by ${tasks.join(', ')}. Remove it from those tasks before deleting it.`) :
        h('p', {class: 'hint'}, 'Removes the device and its connections when you save. Discard changes to undo.'),
      h('button', {type: 'button', class: 'button small danger-button', disabled: tasks.length > 0,
        onclick: () => removeDevice(id)}, `Delete ${info.name}`));
  }
  function removeDevice(id) {
    const name = deviceName(id);
    delete draft[id];
    delete draftInfo[id];
    for (const layout of Object.values(draft)) layout.links = layout.links.filter(link => link.to !== id);
    selection = null;
    changed(true);
    resultMessage(`${name} removed from the draft. Save to delete it, or Discard changes to undo.`);
  }
  function deviceId(name) {
    let base = name.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '').slice(0, 44) || 'device';
    if (!/^[a-z]/.test(base)) base = `d_${base}`.slice(0, 44);
    let id = base, n = 2;
    while (draftInfo[id] || model.devices[id]) id = `${base}_${n++}`;
    return id;
  }
  function addForm() {
    const hasKvm = Object.values(draftInfo).some(info => info.method === 'usb_hid');
    const name = h('input', {maxlength: 100, required: true, placeholder: 'e.g. Living room amplifier', 'aria-label': 'Hardware name'});
    const id = h('input', {maxlength: 48, required: true, placeholder: 'living_room_amp', 'aria-label': 'Device ID', spellcheck: 'false'});
    let idEdited = false;
    name.addEventListener('input', () => { if (!idEdited) id.value = name.value.trim() ? deviceId(name.value) : ''; });
    id.addEventListener('input', () => { idEdited = Boolean(id.value); });
    const type = select_(model.types.map(value => [value, value]), 'Audio', 'Hardware type');
    const method = h('select', {'aria-label': 'Control method'}, Object.entries(model.methods).map(([key, label]) =>
      h('option', {value: key, disabled: key === 'usb_hid' && hasKvm, selected: key === 'ir'},
        key === 'usb_hid' && hasKvm ? `${label} (one KVM already added)` : label)));
    const error = h('p', {class: 'hint error-text', role: 'alert'});
    const form = h('form', {class: 'map-details', novalidate: true, onsubmit: event => {
      event.preventDefault();
      const label = name.value.trim(), key = id.value.trim();
      const problem = !label ? 'Enter a hardware name.' :
        !/^[a-z][a-z0-9_]{0,47}$/.test(key) ? 'The ID must start with a lowercase letter and use only lowercase letters, numbers, or underscores.' :
        key === model.numpad || key === model.controller ? 'That ID is reserved. Choose another.' :
        draftInfo[key] || model.devices[key] ? 'That ID is already used. Choose another.' :
        Object.keys(draftInfo).length >= 100 ? 'The inventory is limited to 100 devices.' : '';
      if (problem) { error.textContent = problem; (label ? id : name).focus(); return; }
      draftInfo[key] = {name: label, type: type.value, notes: '', method: method.value};
      // Next free spot in the column for its type, below the devices already there.
      draft[key] = {ports: {}, links: []};
      autoPlace(draft, [key]);
      selection = {type: 'device', id: key};
      changed(true);
      const zoomed = zoom || 1;
      canvas.scrollTo({left: Math.max(0, draft[key].x * zoomed - 40), top: Math.max(0, draft[key].y * zoomed - 40), behavior: 'smooth'});
      resultMessage(`${label} added. Add its ports and connections, then Save.`);
      inspector.querySelector('.map-details input')?.focus();
    }},
      h('label', {}, 'Hardware name', name),
      h('label', {}, 'Device ID', id, h('small', {}, 'A stable ID used by task actions. It can’t be changed later.')),
      h('div', {class: 'map-port-grid'}, h('label', {}, 'Type', type), h('label', {}, 'Control method', method)),
      error,
      h('div', {class: 'map-command-list'}, h('button', {type: 'submit', class: 'button primary small'}, 'Add to map'),
        h('button', {type: 'button', class: 'button small', onclick: () => select(null)}, 'Cancel')));
    setTimeout(() => name.focus());
    return [h('h2', {}, 'Add hardware'),
      h('p', {class: 'hint'}, 'The device appears on the map. Its IR commands, SmartThings inputs, or KVM connection are set up in Control settings after you save.'),
      form];
  }
  function focusLastPort() {
    const inputs = inspector.querySelectorAll('.map-port-editor input');
    const last = inputs[inputs.length - 1];
    last?.focus();
    last?.select();
  }
  function editOverview() {
    return [h('h2', {}, 'Edit hardware'),
      h('button', {type: 'button', class: 'button small', onclick: () => select({type: 'new'})}, '＋ Add hardware'),
      h('ul', {class: 'map-help'},
        h('li', {}, 'Select a device to rename it, edit its notes and ports, or delete it.'),
        h('li', {}, 'Drag a device to move it. With a device focused, arrow keys move it (Shift for larger steps).'),
        h('li', {}, 'Select a device to add ports: each has a signal type, a direction, and optionally the command it runs.'),
        h('li', {}, 'Drag from a port’s dot to another port’s dot to connect them, or use Add a connection.'),
        h('li', {}, 'Save writes new hardware, changes, deletions, and the layout in one step. Exit edit mode when you’re done.')),
      h('p', {class: 'hint'}, 'Outside edit mode, ports with a command can be sent from the side panel, and they light up in the routes of tasks that use them.')];
  }
  function editLink(owner, index) {
    const link = draft[owner]?.links?.[index];
    if (!link) return editOverview();
    const text = `${deviceName(owner)} · ${portLabel(draft, owner, link.port)} → ${deviceName(link.to)} · ${portLabel(draft, link.to, link.to_port)}`;
    return [h('h2', {}, 'Connection'), h('p', {}, text),
      h('button', {type: 'button', class: 'button small danger-button', onclick: () => removeLink(owner, index)}, 'Remove connection'),
      h('p', {class: 'hint'}, 'Delete or Backspace also removes the selected connection.')];
  }
  function renderInspector() {
    let parts;
    if (selection?.type === 'new' && mode === 'edit') {
      parts = addForm();
    } else if (selection?.type === 'device' && device(selection.id)) {
      parts = mode === 'edit' ? editDevice(selection.id) : operateDevice(selection.id, selection.port);
    } else if (selection?.type === 'link') {
      parts = mode === 'edit' ? editLink(selection.owner, selection.index) : operateLink(selection.owner, selection.index);
    } else {
      parts = mode === 'edit' ? editOverview() : null;
    }
    // Outside edit mode the page is just the map until a device or cable is selected.
    const empty = !parts;
    inspector.hidden = empty;
    root.classList.toggle('no-inspector', empty);
    const close = selection ? h('button', {type: 'button', class: 'text-button map-close', onclick: () => select(null)}, mode === 'edit' ? '← Back' : '× Close') : null;
    inspector.replaceChildren(...[close, ...(parts || [])].filter(Boolean));
    updateLive();
  }

  // ---------- state, rendering, and live input ----------
  function available() {
    return online && state && !pending && !state.busy && String(state.revision) === root.dataset.revision && mode === 'operate';
  }
  function updateLive() {
    const ready = available();
    for (const button of root.querySelectorAll('.map-live')) button.disabled = !ready;
    root.setAttribute('aria-busy', String(pending || Boolean(state?.busy)));
    const stale = state && String(state.revision) !== root.dataset.revision;
    setText(statusLine, mode === 'edit' ? (remoteChanged ? 'Configuration changed in another tab. Discard to load it; saving now would be rejected.' :
        'Editing. Live controls are paused until you exit edit mode.') :
      !online ? 'Controller unavailable. Controls are disabled.' :
      pending ? 'Sending input… Additional presses are not queued.' :
      stale ? 'Updating the map in the background…' :
      state?.busy ? 'Controller busy. Inputs are not queued.' : 'Ready · actions operate real hardware');
    statusLine.classList.toggle('error-text', mode === 'edit' && remoteChanged);
    // Live status matters once something is selected (or while editing); otherwise show only the map.
    statusLine.hidden = mode === 'operate' && !selection && online;
  }
  function renderEditBar() {
    editBar.hidden = mode !== 'edit';
    toolbar.hidden = mode !== 'edit';
    const isDirty = dirty();
    setText(dirtyPill, saving ? 'Saving…' : isDirty ? 'Unsaved changes' : 'No unsaved changes');
    dirtyPill.className = `pill ${isDirty ? 'amber' : 'neutral'}`;
    $('map-save').disabled = !isDirty || saving;
    $('map-discard').disabled = (!isDirty && !remoteChanged) || saving;
    editButton.hidden = mode === 'edit';
    root.dataset.mode = mode;
  }
  function renderAll() {
    renderSvg();
    renderInspector();
    renderEditBar();
    renderLegend();
  }
  function changed(structure) {
    renderSvg();
    renderEditBar();
    if (structure) renderInspector();
  }
  function select(next) {
    if (!next && mode === 'operate') resultMessage('');
    selection = next;
    renderSvg();
    renderInspector();
  }
  function resultMessage(text, error = false) {
    resultLine.textContent = text;
    resultLine.classList.toggle('error-text', error);
  }
  async function readResponse(response) {
    if (response.redirected) throw new Error('Sign in again, then reload the page. Nothing was retried.');
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new Error(`Request failed (HTTP ${response.status}). Reload the page; nothing was retried.`);
    }
    return response.json();
  }
  async function reloadModel() {
    try {
      const response = await fetch(root.dataset.modelUrl, {cache: 'no-store'});
      const next = await readResponse(response);
      if (!response.ok) throw new Error(next.error || 'Map unavailable.');
      if (mode === 'edit' && dirty()) { remoteChanged = true; return; }
      loadModel(next);
    } catch (_) {
      online = false;
    }
  }
  async function refresh(force = false) {
    if (document.hidden || (!force && (polling || pending))) return;
    polling?.abort();
    const controller = new AbortController();
    polling = controller;
    const timeout = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(root.dataset.stateUrl, {cache: 'no-store', signal: controller.signal});
      const next = await readResponse(response);
      if (polling !== controller) return;
      if (!response.ok) throw new Error(next.error || 'Controller unavailable.');
      const activeChanged = next.active_task !== state?.active_task;
      const previous = state;
      state = next;
      online = true;
      if (String(next.revision) !== root.dataset.revision) {
        if (mode === 'edit' && dirty()) remoteChanged = true;
        else if (!saving) await reloadModel();
      }
      if (activeChanged) renderSvg();
      // The numpad panel shows the active task's keys and dial/slider targets.
      if (mode === 'operate' && selection?.type === 'device' && selection.id === model.numpad &&
          (activeChanged || JSON.stringify(previous?.key_commands) !== JSON.stringify(next.key_commands) ||
           JSON.stringify(previous?.controls) !== JSON.stringify(next.controls))) renderInspector();
    } catch (_) {
      if (polling === controller) online = false;
    } finally {
      clearTimeout(timeout);
      if (polling === controller) { polling = null; updateLive(); renderEditBar(); }
    }
  }
  function requestId() {
    return Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16).padStart(8, '0')).join('');
  }
  async function send(url, fields, element) {
    const now = performance.now();
    if (!available() || now - lastInput < 350) return;
    lastInput = now;
    const body = new URLSearchParams({...fields, csrf: root.dataset.csrf, revision: root.dataset.revision, request_id: requestId()});
    pending = true;
    polling?.abort();
    polling = null;
    resultMessage('');
    element?.classList.add('is-pressed');
    updateLive();
    try {
      // Never retry a live input, including on connection loss or an HTTP error.
      const response = await fetch(url, {method: 'POST', body});
      const data = await readResponse(response);
      if (!response.ok || !data.ok) throw new Error(data.error || 'Input was rejected.');
      resultMessage(data.message);
    } catch (error) {
      resultMessage(error instanceof TypeError ? 'Connection lost. The command may have run. Check Activity before trying again; nothing was retried.' : error.message, true);
    } finally {
      await refresh(true);
      pending = false;
      element?.classList.remove('is-pressed');
      updateLive();
      renderSvg();
    }
  }
  async function save() {
    if (!dirty() || saving) return;
    saving = true;
    renderEditBar();
    try {
      const body = new URLSearchParams({csrf: root.dataset.csrf, revision: root.dataset.revision, devices: JSON.stringify(
        Object.fromEntries(Object.entries(draftInfo).map(([id, info]) => [id, {...info, map: draft[id]}])))});
      const response = await fetch(root.dataset.saveUrl, {method: 'POST', body});
      const data = await readResponse(response);
      if (!response.ok || !data.ok) throw new Error(data.error || 'The map could not be saved.');
      saving = false;
      loadModel(data.model);
      resultMessage(data.message);
    } catch (error) {
      resultMessage(error instanceof TypeError ? 'Connection lost. Reload to check whether the map was saved.' : error.message, true);
    } finally {
      saving = false;
      renderEditBar();
    }
  }
  function setMode(next) {
    if (next === mode) return;
    if (mode === 'edit' && dirty() && !window.confirm('You have unsaved changes. Exit edit mode and discard them?')) return;
    if (mode === 'edit') resetDraft();
    if (selection?.type === 'new' || (selection?.type === 'device' && !model.devices[selection.id])) selection = null;
    mode = next;
    connect = null;
    press = null;
    hoverTask = null;
    if (mode === 'operate' && remoteChanged) { reloadModel(); }
    renderAll();
    updateLive();
  }

  // ---------- pointer and keyboard interaction ----------
  function point(event) {
    const matrix = svg.getScreenCTM()?.inverse();
    if (!matrix) return {x: 0, y: 0};
    const p = new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix);
    return {x: p.x, y: p.y};
  }
  function portDotAt(event) {
    const element = document.elementFromPoint(event.clientX, event.clientY);
    const dot = element?.closest?.('[data-port-dot]');
    const node = dot?.closest('[data-device]');
    return dot && node ? {id: node.dataset.device, port: dot.dataset.portDot} : null;
  }
  svg.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    const node = event.target.closest('[data-device]');
    const link = event.target.closest('[data-owner]');
    const dot = event.target.closest('[data-port-dot]');
    const port = event.target.closest('[data-port]');
    const at = point(event);
    press = {id: node?.dataset.device, port: port?.dataset.port, link: link ? {owner: link.dataset.owner, index: Number(link.dataset.index)} : null,
      start: at, moved: false};
    if (mode === 'edit' && node && dot) {
      const layout = draft[node.dataset.device];
      const cx = Number(dot.getAttribute('cx'));
      const from = {x: layout.x + cx, y: layout.y + HEAD + Object.keys(layout.ports).indexOf(dot.dataset.portDot) * ROW + ROW / 2,
        side: cx === 0 ? 'left' : 'right'};
      connect = {id: node.dataset.device, port: dot.dataset.portDot, from, to: {...at, side: at.x < from.x ? 'right' : 'left'}};
      svg.setPointerCapture(event.pointerId);
      event.preventDefault();
    } else if (mode === 'edit' && node) {
      press.origin = {x: draft[node.dataset.device].x, y: draft[node.dataset.device].y};
      svg.setPointerCapture(event.pointerId);
      event.preventDefault();
    }
  });
  svg.addEventListener('pointermove', event => {
    if (!press) return;
    const at = point(event);
    if (Math.hypot(at.x - press.start.x, at.y - press.start.y) > 4) press.moved = true;
    if (connect) {
      connect.to = {...at, side: at.x < connect.from.x ? 'right' : 'left'};
      scheduleSvg();
    } else if (mode === 'edit' && press.origin && press.moved) {
      const layout = draft[press.id];
      layout.x = snap(press.origin.x + at.x - press.start.x);
      layout.y = snap(press.origin.y + at.y - press.start.y);
      scheduleSvg();
      renderEditBar();
    }
  });
  function finishPress(event) {
    if (!press) return;
    const current = press;
    press = null;
    if (connect) {
      const target = event.type === 'pointerup' ? portDotAt(event) : null;
      const from = connect;
      connect = null;
      if (target && (target.id !== from.id || target.port !== from.port)) {
        const message = addLink(from.id, from.port, target.id, target.port);
        if (message) resultMessage(message, !message.startsWith('Connected'));
        return;
      }
      if (!current.moved) select({type: 'device', id: current.id, port: current.port});
      else scheduleSvg();
      return;
    }
    if (current.moved || event.type !== 'pointerup') { scheduleSvg(); return; }
    if (current.id) select({type: 'device', id: current.id, port: current.port});
    else if (current.link) select({type: 'link', ...current.link});
    else if (selection) select(null);
  }
  svg.addEventListener('pointerup', finishPress);
  svg.addEventListener('pointercancel', finishPress);
  svg.addEventListener('keydown', event => {
    const node = event.target.closest?.('[data-device]');
    if (!node) return;
    const id = node.dataset.device;
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (!event.repeat) select({type: 'device', id});
      return;
    }
    const moves = {ArrowUp: [0, -1], ArrowDown: [0, 1], ArrowLeft: [-1, 0], ArrowRight: [1, 0]};
    if (mode === 'edit' && moves[event.key]) {
      event.preventDefault();
      const step = event.shiftKey ? 50 : GRID;
      draft[id].x = snap(draft[id].x + moves[event.key][0] * step);
      draft[id].y = snap(draft[id].y + moves[event.key][1] * step);
      changed(false);
    }
  });
  document.addEventListener('keydown', event => {
    if (mode !== 'edit' || selection?.type !== 'link' || !['Delete', 'Backspace'].includes(event.key)) return;
    if (event.target.closest?.('input, select, textarea')) return;
    event.preventDefault();
    removeLink(selection.owner, selection.index);
  });
  editButton.addEventListener('click', () => setMode('edit'));
  $('map-exit').addEventListener('click', () => setMode('operate'));
  $('map-add').addEventListener('click', () => select({type: 'new'}));
  $('map-save').addEventListener('click', save);
  $('map-zoom-out').addEventListener('click', () => setZoom(zoom - 0.1));
  $('map-zoom-in').addEventListener('click', () => setZoom(zoom + 0.1));
  $('map-zoom-fit').addEventListener('click', () => { renderSvg(); setZoom(fitZoom()); });
  $('map-discard').addEventListener('click', () => {
    if (dirty() && !window.confirm('Discard unsaved changes?')) return;
    if (selection?.type === 'device' && !model.devices[selection.id]) selection = null;
    if (remoteChanged) { resetDraft(); reloadModel(); return; }
    resetDraft();
    resultMessage('Changes discarded.');
    renderAll();
  });
  $('map-arrange').addEventListener('click', () => {
    const placed = {};
    for (const [id, layout] of Object.entries(draft)) placed[id] = {ports: layout.ports, links: []};
    autoPlace(placed, Object.keys(draft));
    for (const [id, layout] of Object.entries(draft)) Object.assign(layout, {x: placed[id].x, y: placed[id].y});
    changed(false);
  });
  window.addEventListener('beforeunload', event => {
    if (mode === 'edit' && dirty()) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => { online = false; updateLive(); });
  window.addEventListener('pageshow', () => refresh());
  document.addEventListener('visibilitychange', () => {
    online = false;
    updateLive();
    if (!document.hidden) refresh();
  });
  if (root.dataset.select && model.devices[root.dataset.select]) selection = {type: 'device', id: root.dataset.select};
  loadModel(model);
  refresh();
  setInterval(refresh, 1500);
})();
