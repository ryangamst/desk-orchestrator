// Run with: node --test tests/test_virtual_numpad_ui.cjs
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

class Element {
  constructor(dataset = {}) {
    this.dataset = dataset;
    this.listeners = {};
    this.attributes = {};
    this.textContent = '';
    this.style = {};
    this.isConnected = true;
    this.classes = new Set();
    this.classList = {
      add: name => this.classes.add(name),
      remove: name => this.classes.delete(name),
      contains: name => this.classes.has(name),
      toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name),
      [Symbol.iterator]: () => this.classes.values(),
    };
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector() { return this.child ||= new Element(); }
  matches(selector) {
    return selector.split(',').some(part => {
      part = part.trim();
      if (part.startsWith('.')) return this.classes.has(part.slice(1));
      const attribute = part.slice(6, -1).replace(/-([a-z])/g, (_, char) => char.toUpperCase());
      return Object.hasOwn(this.dataset, attribute);
    });
  }
  closest(selector) { return this.matches(selector) ? this : null; }
  contains(element) { return element === this; }
}
const settle = () => new Promise(resolve => setImmediate(resolve));
const state = (active = null, extra = {}) => ({
  revision: 1, active_task: active, active_label: active || 'None', busy: false,
  active_color: active === 'work' ? 'blue' : active ? 'lavender' : null,
  controls: {dial: {enabled: false}, slider: {enabled: false}}, ...extra,
});
const response = (data, ok = true) => ({ok, status: ok ? 200 : 409,
  headers: {get: () => 'application/json'}, json: async () => data});

function setup() {
  const ids = {};
  for (const id of ['virtual-numpad', 'virtual-dial', 'virtual-slider', 'virtual-input-status',
    'virtual-input-result', 'virtual-active-task',
    'overview-stats', 'overview-tasks', 'mapping-focus']) ids[id] = new Element();
  const panel = ids['virtual-numpad'];
  ids['virtual-slider'].value = '50';
  panel.dataset = {revision: '1', csrf: 'test', stateUrl: '/api/numpad', pressUrl: '/api/numpad/press'};
  const key = new Element({virtualKey: 'KEY_KP1', linkedTask: 'personal', taskLabel: 'Personal'});
  key.textContent = '1';
  key.classes.add('tone-lavender');
  key.classes.add('key');
  const card = new Element({linkedTask: 'personal', taskLabel: 'Personal'});
  panel.querySelectorAll = selector => selector === '[data-virtual-key]' ? [key] : [];
  const document = new Element();
  document.hidden = false;
  document.getElementById = id => ids[id];
  document.querySelectorAll = selector => [key, card].filter(element => element.matches(selector));
  document.dispatchEvent = event => document.listeners[event.type]?.(event);
  const appScript = readFileSync('desk_orchestrator/static/app.js', 'utf8');
  vm.runInNewContext(appScript.slice(appScript.indexOf('const mappingFocus ='),
    appScript.indexOf('// Reveal collapsed settings')), {document});
  const window = new Element();
  window.location = {href: 'http://localhost/'};
  const requests = [], intervals = [];
  let now = 1000;
  vm.runInNewContext(readFileSync('desk_orchestrator/static/virtual_numpad.js', 'utf8'), {
    document, window, URL, URLSearchParams, AbortController, Event, crypto: webcrypto,
    performance: {now: () => now}, setTimeout, clearTimeout,
    setInterval: callback => intervals.push(callback),
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  });
  return {ids, key, card, document, window, panel, requests, poll: () => intervals[0](),
    advance: ms => { now += ms; },
    physical: controls => document.listeners['numpad-physical-controls']({detail: controls})};
}
async function ready(ui) {
  ui.requests[0].resolve(response(state()));
  await settle();
  assert.equal(ui.key.disabled, false);
}

test('press uses a single background POST and stays pending until fresh state arrives', async () => {
  const ui = setup();
  await ready(ui);
  const pressed = ui.key.listeners.click();
  assert.equal(ui.requests[1].options.method, 'POST');
  assert.equal(ui.key.disabled, true);
  await ui.poll();
  assert.equal(ui.requests.length, 2, 'ordinary polls wait for the command');
  ui.requests[1].resolve(response({ok: true, message: 'Commands sent.'}));
  await settle();
  assert.match(ui.ids['virtual-input-status'].textContent, /Sending input/);
  assert.equal(ui.key.disabled, true);
  ui.requests[2].resolve(response(state('personal')));
  await pressed;
  assert.equal(ui.ids['virtual-active-task'].textContent, 'personal');
  assert.equal(ui.ids['virtual-input-result'].textContent, 'Commands sent.');
  assert.equal(ui.key.disabled, false);
});

test('poll started before a press cannot overwrite the post-command state', async () => {
  const ui = setup();
  await ready(ui);
  const oldPoll = ui.poll();
  const pressed = ui.key.listeners.click();
  assert.equal(ui.requests[1].options.signal.aborted, true);
  ui.requests[2].resolve(response({ok: true, message: 'Done'}));
  await settle();
  ui.requests[3].resolve(response(state('personal')));
  await pressed;
  ui.requests[1].resolve(response(state('work')));
  await oldPoll;
  assert.equal(ui.ids['virtual-active-task'].textContent, 'personal');
  assert.equal(ui.key.disabled, false);
});

test('connection loss does not retry a command and failed state refresh disables inputs', async () => {
  const ui = setup();
  await ready(ui);
  const pressed = ui.key.listeners.click();
  ui.requests[1].reject(new Error('Lost connection'));
  await settle();
  ui.requests[2].reject(new Error('Still offline'));
  await pressed;
  assert.equal(ui.requests.filter(item => item.options.method === 'POST').length, 1);
  assert.equal(ui.key.disabled, true);
  assert.match(ui.ids['virtual-input-status'].textContent, /unavailable/);
  assert.equal(ui.ids['virtual-input-result'].textContent, 'Lost connection');
  const retryPoll = ui.poll();
  ui.requests[3].resolve(response(state()));
  await retryPoll;
  assert.equal(ui.key.disabled, false);
});

test('mapping changes update the existing key, summaries, and next POST revision', async () => {
  const ui = setup();
  await ready(ui);
  const poll = ui.poll();
  assert.equal(ui.requests[1].url.searchParams.get('revision'), '1');
  ui.requests[1].resolve(response(state(null, {revision: 2, overview: {
    stats: 'New counts', tasks: 'Updated tasks',
    mappings: {KEY_KP1: {task: 'work', label: 'Work', color: 'blue'}},
  }})));
  await poll;
  assert.equal(ui.key.dataset.linkedTask, 'work');
  assert.equal(ui.key.attributes['aria-label'], '1: Run Work');
  assert.equal(ui.key.classes.has('tone-lavender'), false);
  assert.equal(ui.key.classes.has('tone-blue'), true);
  assert.equal(ui.ids['overview-tasks'].innerHTML, 'Updated tasks');
  assert.equal(ui.panel.dataset.revision, '2');
  const pressed = ui.key.listeners.click();
  assert.equal(ui.requests[2].options.body.get('revision'), '2');
  ui.requests[2].resolve(response({ok: true, message: 'Done'}));
  await settle();
  ui.requests[3].resolve(response(state('work', {revision: 2})));
  await pressed;
});

test('unmapped keys lose their old task and remain disabled after a refresh', async () => {
  const ui = setup();
  await ready(ui);
  ui.key.classes.add('is-active-task');
  const poll = ui.poll();
  ui.requests[1].resolve(response(state(null, {revision: 2,
    overview: {stats: '', tasks: '', mappings: {}}})));
  await poll;
  assert.equal(ui.key.dataset.linkedTask, undefined);
  assert.equal(ui.key.disabled, true);
  assert.equal(ui.key.classes.has('is-active-task'), false);
  assert.equal(ui.key.attributes['aria-label'], '1: Unassigned');
});

test('active-task commands enable unused keys and refresh labels without a revision change', async () => {
  const ui = setup();
  delete ui.key.dataset.linkedTask;
  delete ui.key.dataset.taskLabel;
  ui.key.dataset.virtualKey = 'KEY_NUMLOCK';
  ui.key.textContent = 'Num Lock';
  ui.requests[0].resolve(response(state('personal', {key_commands: {KEY_NUMLOCK: 'Play / Pause'}})));
  await settle();
  assert.equal(ui.key.disabled, false);
  assert.equal(ui.key.title, 'Play / Pause');
  assert.equal(ui.key.classes.has('tone-lavender'), true);
  assert.equal(ui.key.classes.has('is-active-task'), true);
  ui.document.listeners.focusin({target: ui.key});
  assert.equal(ui.ids['mapping-focus'].textContent, 'personal · Key Num Lock · Play / Pause');
  assert.equal(ui.card.classes.has('is-linked'), true);
  const poll = ui.poll();
  ui.requests[1].resolve(response(state('work', {key_commands: {KEY_NUMLOCK: 'Mute'}})));
  await poll;
  assert.equal(ui.key.title, 'Mute');
  assert.equal(ui.key.classes.has('tone-lavender'), false);
  assert.equal(ui.key.classes.has('tone-blue'), true);
  assert.equal(ui.ids['mapping-focus'].textContent, 'work · Key Num Lock · Mute');
  assert.equal(ui.card.classes.has('is-linked'), false);
  assert.equal(ui.key.attributes['aria-label'], 'Num Lock: Mute');
  const pressed = ui.key.listeners.click();
  assert.equal(ui.requests[2].options.body.get('active_task'), 'work');
  assert.equal(ui.requests[2].options.body.get('key'), 'KEY_NUMLOCK');
  ui.requests[2].resolve(response({ok: true, message: 'Commands sent.'}));
  await settle();
  ui.requests[3].resolve(response(state()));
  await pressed;
  assert.equal(ui.key.disabled, true);
  assert.equal(ui.key.title, 'Unassigned');
  assert.equal(ui.key.classes.has('tone-blue'), false);
  assert.equal(ui.key.classes.has('mapped'), false);
  assert.equal(ui.key.classes.has('is-active-task'), false);
  assert.equal(ui.key.classes.has('is-linked'), false);
  assert.equal(ui.ids['mapping-focus'].textContent, 'Key Num Lock · Unassigned');
});

test('task shortcuts take priority and command edits refresh the focused preview', async () => {
  const ui = setup();
  ui.requests[0].resolve(response(state('personal', {key_commands: {KEY_KP1: 'Mute'}})));
  await settle();
  assert.equal(ui.key.title, 'Run Personal');
  assert.equal(ui.key.dataset.keyCommand, undefined);
  ui.document.listeners.focusin({target: ui.key});
  ui.document.listeners.pointerover({target: ui.key});
  const poll = ui.poll();
  ui.requests[1].resolve(response(state('personal', {revision: 2,
    key_commands: {KEY_KP1: 'Save <document>'},
    overview: {stats: '', tasks: '', mappings: {}}})));
  ui.document.activeElement = ui.key;
  await poll;
  assert.equal(ui.key.dataset.linkedTask, undefined);
  assert.equal(ui.key.title, 'Save <document>');
  assert.equal(ui.ids['mapping-focus'].textContent, 'personal · Key 1 · Save <document>');
  ui.document.listeners.pointerout({target: ui.key});
  ui.document.listeners.focusout({target: ui.key});
  ui.document.listeners.pointerover({target: ui.card});
  assert.equal(ui.key.classes.has('is-linked'), true);
  assert.match(ui.ids['mapping-focus'].textContent, /Keyboard: 1/);
});

const controlsState = (extra = {}) => state('personal', {controls: {
  dial: {enabled: true, switch_enabled: true, target: 'Speakers', source_count: 2, source_index: 1},
  slider: {enabled: true, target: 'Monitor'},
}, ...extra});
async function controlsReady(ui, extra = {}) {
  ui.requests[0].resolve(response(controlsState(extra)));
  await settle();
}
async function completeControl(ui, promise, next = controlsState()) {
  ui.requests.at(-1).resolve(response({ok: true, message: 'Sent'}));
  await settle();
  ui.requests.at(-1).resolve(response(next));
  await promise;
  await settle();
}
const rotation = ui => ui.ids['virtual-dial'].querySelector('.wheel-face').style.transform;
const physicalControl = (extra = {}) => ({token: 'listener-one', steps: 0, sequence: 0,
  direction: 0, position: null, active: false, pressed: false, ...extra});

test('hover scrolling animates immediately, keeps commands bounded, and preserves target labels', async () => {
  const ui = setup();
  await controlsReady(ui);
  const dial = ui.ids['virtual-dial'];
  assert.match(dial.title, /Speakers · Source 1 of 2/);
  assert.match(ui.ids['virtual-slider'].attributes['aria-label'], /Monitor/);
  assert.equal(ui.ids['virtual-dial-target'], undefined);
  let prevented = 0;
  const scroll = deltaY => dial.listeners.wheel({deltaY, deltaMode: 0, preventDefault() { prevented++; }});
  const sent = scroll(-100);
  assert.equal(rotation(ui), 'rotate(30deg)');
  assert.ok(dial.classes.has('is-control-active'));
  assert.equal(ui.requests[1].options.body.get('direction'), 'increase');
  scroll(-100);
  assert.equal(rotation(ui), 'rotate(60deg)');
  assert.equal(ui.requests.length, 2, 'busy movement is never queued');
  await completeControl(ui, sent);
  assert.equal(rotation(ui), 'rotate(60deg)', 'response must not rotate a second time');
  assert.equal(prevented, 2);
  assert.equal(ui.requests.filter(r => r.options.method === 'POST').length, 1);
});

test('small trackpad movements accumulate, zoom passes through, and rotation works without click mapping', async () => {
  const ui = setup();
  const next = controlsState();
  next.controls.dial.switch_enabled = false;
  ui.requests[0].resolve(response(next));
  await settle();
  const dial = ui.ids['virtual-dial'];
  assert.equal(dial.attributes['aria-disabled'], 'false');
  let prevented = 0;
  const wheel = (deltaY, extra = {}) => dial.listeners.wheel({deltaY, preventDefault() { prevented++; }, ...extra});
  wheel(-100, {ctrlKey: true});
  assert.equal(prevented, 0);
  wheel(-8); wheel(-8);
  assert.equal(ui.requests.length, 1);
  const sent = wheel(-8);
  assert.equal(rotation(ui), 'rotate(30deg)');
  await completeControl(ui, sent, next);
  ui.advance(101);
  dial.listeners.click();
  assert.equal(ui.requests.filter(r => r.options.method === 'POST').length, 1);
  const down = dial.listeners.wheel({deltaY: 2, deltaMode: 1, preventDefault() {}});
  assert.equal(rotation(ui), 'rotate(0deg)');
  await completeControl(ui, down, next);
});

test('physical controls animate once per change, even when unassigned or busy, without commands', async () => {
  const ui = setup();
  await controlsReady(ui, {busy: true});
  const base = physicalControl();
  ui.physical({dial: base, slider: base});
  const moving = {dial: physicalControl({steps: 2, sequence: 2, active: true, pressed: true}),
    slider: physicalControl({steps: -1, sequence: 1, active: true})};
  ui.physical(moving);
  assert.equal(rotation(ui), 'rotate(60deg)');
  assert.equal(ui.ids['virtual-slider'].value, '45');
  assert.ok(ui.ids['virtual-dial'].classes.has('is-physical-pressed'));
  assert.ok(ui.ids['virtual-slider'].classes.has('is-physical-active'));
  ui.physical(moving);
  assert.equal(rotation(ui), 'rotate(60deg)');
  ui.physical({});
  assert.ok(!ui.ids['virtual-dial'].classes.has('is-physical-pressed'));
  ui.physical({dial: physicalControl({token: 'reconnected', steps: 999, sequence: 999})});
  assert.equal(rotation(ui), 'rotate(60deg)', 'old activity is not replayed');
  assert.equal(ui.requests.length, 1);
});

test('physical slider position and dragging share a baseline without jumps or synthetic inputs', async () => {
  const ui = setup();
  await controlsReady(ui);
  const slider = ui.ids['virtual-slider'];
  ui.physical({slider: physicalControl({position: 75, sequence: 1})});
  assert.equal(slider.value, '75');
  slider.listeners.pointerdown();
  slider.value = '80';
  const sent = slider.listeners.input();
  assert.equal(ui.requests[1].options.body.get('direction'), 'increase');
  ui.physical({slider: physicalControl({position: 20, sequence: 2})});
  assert.equal(slider.value, '80', 'physical feedback cannot interrupt a drag');
  ui.window.listeners.pointerup();
  ui.physical({slider: physicalControl({position: 20, sequence: 2})});
  assert.equal(slider.value, '80', 'skipped movement is never replayed');
  await completeControl(ui, sent);
  assert.equal(slider.value, '80');
  ui.physical({slider: physicalControl({position: 30, sequence: 3})});
  assert.equal(slider.value, '30');
  assert.equal(ui.requests.filter(r => r.options.method === 'POST').length, 1);
  slider.listeners.keydown({key: 'ArrowUp'});
  ui.physical({slider: physicalControl({position: 40, sequence: 4})});
  assert.equal(slider.value, '30');
  slider.listeners.keyup();
  ui.window.listeners.pagehide();
});

test('dial keyboard input animates, ignores repeats, and click still switches target', async () => {
  const ui = setup();
  await controlsReady(ui);
  const dial = ui.ids['virtual-dial'];
  dial.listeners.keydown({key: 'ArrowRight', repeat: true, preventDefault() {}});
  assert.equal(ui.requests.length, 1);
  dial.listeners.keydown({key: 'ArrowRight', repeat: false, preventDefault() {}});
  assert.equal(rotation(ui), 'rotate(30deg)');
  // Keyboard listeners use the same asynchronous command path.
  await completeControl(ui);
  ui.advance(101);
  const click = dial.listeners.click();
  assert.equal(ui.requests.at(-1).options.body.get('direction'), 'switch');
  await completeControl(ui, click);
  assert.equal(rotation(ui), 'rotate(30deg)');
});
