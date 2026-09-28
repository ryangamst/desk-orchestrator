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
  querySelector() { return new Element(); }
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
    'virtual-input-result', 'virtual-active-task', 'virtual-dial-target', 'virtual-slider-target',
    'overview-stats', 'overview-tasks', 'mapping-focus']) ids[id] = new Element();
  const panel = ids['virtual-numpad'];
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
  vm.runInNewContext(readFileSync('desk_orchestrator/static/virtual_numpad.js', 'utf8'), {
    document, window, URL, URLSearchParams, AbortController, Event, crypto: webcrypto,
    performance: {now: () => 1000}, setTimeout, clearTimeout,
    setInterval: callback => intervals.push(callback),
    fetch: (url, options) => new Promise((resolve, reject) => requests.push({url, options, resolve, reject})),
  });
  return {ids, key, card, document, panel, requests, poll: () => intervals[0]()};
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
