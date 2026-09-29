const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

const settle = () => new Promise(resolve => setImmediate(resolve));
const response = (pressed_keys, controls = {}) => ({ok: true, headers: {get: () => 'application/json'},
  json: async () => ({pressed_keys, controls})});

function setup() {
  const keys = ['KEY_KP1', 'KEY_KP2', 'KEY_CUSTOM_test'].map(id => {
    const classes = new Set();
    return {dataset: {virtualKey: id}, disabled: true, classes,
      classList: {toggle: (name, on) => on ? classes.add(name) : classes.delete(name)}};
  });
  const panel = {dataset: {keysUrl: '/api/numpad/keys'}, querySelectorAll: () => keys};
  const events = [];
  const document = {hidden: false, listeners: {}, getElementById: () => panel,
    dispatchEvent: event => events.push(event),
    addEventListener(name, fn) { this.listeners[name] = fn; }};
  const window = {listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }};
  const requests = [], timers = new Map();
  let poll, timerId = 0;
  vm.runInNewContext(readFileSync('desk_orchestrator/static/physical_numpad.js', 'utf8'), {
    document, window, AbortController, CustomEvent,
    setInterval: fn => { poll = fn; },
    setTimeout: fn => { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
    fetch: (url, options) => new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new Error('Aborted')));
      requests.push({url, options, resolve, reject});
    }),
  });
  return {keys, document, window, requests, timers, events, poll: () => poll(),
    lit: () => keys.filter(key => key.classes.has('is-physical-pressed')).map(key => key.dataset.virtualKey)};
}

test('held, simultaneous, unassigned and custom keys highlight without dispatching actions', async () => {
  const ui = setup();
  ui.keys[0].classes.add('is-pressed'); // Independent virtual input in progress.
  ui.requests[0].resolve(response(['KEY_KP1', 'KEY_CUSTOM_test']));
  await settle();
  assert.deepEqual(ui.lit(), ['KEY_KP1', 'KEY_CUSTOM_test']);
  assert.ok(ui.keys.every(key => key.disabled));
  const poll = ui.poll();
  ui.requests[1].resolve(response(['KEY_KP2']));
  await poll;
  assert.deepEqual(ui.lit(), ['KEY_KP2']);
  assert.ok(ui.keys[0].classes.has('is-pressed'));
  assert.ok(ui.requests.every(req => req.url === '/api/numpad/keys' && !req.options.method));
});

test('requests do not overlap and failed or timed-out feedback clears held keys', async () => {
  const ui = setup();
  await ui.poll();
  assert.equal(ui.requests.length, 1);
  ui.requests[0].resolve(response(['KEY_KP1']));
  await settle();
  const poll = ui.poll();
  ui.requests[1].reject(new Error('Offline'));
  await poll;
  assert.deepEqual(ui.lit(), []);
  const recover = ui.poll();
  ui.requests[2].resolve(response(['KEY_KP2']));
  await recover;
  const timeout = ui.poll();
  for (const fn of ui.timers.values()) fn();
  await timeout;
  assert.deepEqual(ui.lit(), []);
});

test('hidden and closed pages clear feedback and resume with a fresh snapshot', async () => {
  const ui = setup();
  ui.requests[0].resolve(response(['KEY_KP1']));
  await settle();
  const oldPoll = ui.poll();
  ui.document.hidden = true;
  ui.document.listeners.visibilitychange();
  assert.deepEqual(ui.lit(), []);
  assert.equal(ui.requests[1].options.signal.aborted, true);
  await oldPoll;
  await ui.poll();
  assert.equal(ui.requests.length, 2);
  ui.document.hidden = false;
  ui.document.listeners.visibilitychange();
  ui.requests[2].resolve(response(['KEY_KP2']));
  await settle();
  assert.deepEqual(ui.lit(), ['KEY_KP2']);
  ui.window.listeners.pagehide();
  await ui.poll();
  assert.deepEqual(ui.lit(), []);
  assert.equal(ui.requests.length, 3);
  ui.window.listeners.pageshow();
  ui.requests[3].resolve(response([]));
  await settle();
});

test('expired login clears highlights instead of treating HTML as key feedback', async () => {
  const ui = setup();
  ui.requests[0].resolve(response(['KEY_KP1']));
  await settle();
  const poll = ui.poll();
  ui.requests[1].resolve({...response(['KEY_KP1']), redirected: true});
  await poll;
  assert.deepEqual(ui.lit(), []);
});

test('control snapshots and disconnect clears reach the virtual controls without a POST', async () => {
  const ui = setup();
  const controls = {dial: {token: 'one', steps: 2, sequence: 2, active: true}};
  ui.requests[0].resolve(response([], controls));
  await settle();
  assert.equal(ui.events[0].type, 'numpad-physical-controls');
  assert.deepEqual(ui.events[0].detail, controls);
  const poll = ui.poll();
  ui.requests[1].reject(new Error('Offline'));
  await poll;
  assert.equal(Object.keys(ui.events.at(-1).detail).length, 0);
  assert.ok(ui.requests.every(req => !req.options.method));
});
