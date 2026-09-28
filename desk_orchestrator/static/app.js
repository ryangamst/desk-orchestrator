"use strict";

const taskAppearance = document.getElementById("task-appearance");
if (taskAppearance) {
  taskAppearance.addEventListener("change", (event) => {
    if (event.target.name === "color") {
      taskAppearance.className = `task-appearance tone-${event.target.value}`;
    }
  });
}

// Editors use DOM APIs and textContent so device names never become HTML.
function node(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const child of children.flat()) {
    if (child !== null && child !== undefined) element.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (key === "class") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key in element) element[key] = value;
    else element.setAttribute(key, value);
  }
  return element;
}
function input(value = "", attrs = {}) { return node("input", {value, ...attrs}); }
function field(label, control, hint = "") { return node("label", {}, label, control, hint ? node("small", {}, hint) : null); }
function select(options, value, onChange) {
  return node("select", {value, onchange: onChange || (() => {})}, options.map(([id, label]) => node("option", {value: id}, label)));
}
function button(label, action, cls = "button small") { return node("button", {type: "button", class: cls, onclick: action}, label); }
function checkbox(label, checked, hint = "") {
  const control = input("", {type: "checkbox", checked});
  return {control, element: node("label", {class: "checkbox-field"}, control, label, hint ? node("small", {}, hint) : null)};
}
function readJSON(id) { return JSON.parse(document.getElementById(id).textContent); }
const numpadChoice = document.getElementById("numpad-device-choice");
if (numpadChoice) {
  const candidates = readJSON("numpad-devices");
  const manual = document.getElementById("manual-numpad-field");
  const details = document.getElementById("numpad-choice-details");
  const update = () => {
    manual.hidden = numpadChoice.value !== "manual";
    const candidate = candidates.find(item => item.selection_path === numpadChoice.value);
    details.textContent = candidate ? `${candidate.selection_path} · USB ${candidate.vendor_id}:${candidate.product_id}${candidate.aliases.length ? " · Stable device path" : " · Event number may change after reconnecting"}${candidate.readable ? "" : " · Input permissions need attention"}` : "";
  };
  numpadChoice.addEventListener("change", update);
  update();
}
function failForm(form, message) {
  let error = form.querySelector(".editor-error");
  if (!error) { error = node("div", {class: "notice error editor-error", role: "alert"}); form.prepend(error); }
  error.textContent = message;
  error.scrollIntoView({block: "center", behavior: "smooth"});
}

for (const form of document.querySelectorAll("[data-confirm]")) {
  form.addEventListener("submit", event => { if (!window.confirm(form.dataset.confirm)) event.preventDefault(); });
}
for (const form of document.querySelectorAll("[data-dirty-guard]")) {
  let dirty = false;
  form.addEventListener("input", () => dirty = true);
  form.addEventListener("change", () => dirty = true);
  form.addEventListener("click", event => { if (event.target.closest("button[type=button]")) dirty = true; });
  form.addEventListener("submit", event => { queueMicrotask(() => { if (!event.defaultPrevented) dirty = false; }); });
  window.addEventListener("beforeunload", event => { if (dirty) { event.preventDefault(); event.returnValue = ""; } });
}

function pulsesEditor(initial = []) {
  const container = node("div", {class: "pulses"});
  const list = node("div");
  let rows = [];
  const render = () => {
    list.replaceChildren(...rows.map((row, index) => node("div", {class: "pulse-row"},
      field("Learned IR key", row.key), field("Remote override", row.remote), field("Presses", row.count), field("Gap (s)", row.gap),
      button("↑", () => { if (index) { [rows[index-1], rows[index]] = [rows[index], rows[index-1]]; render(); } }, "icon-button"),
      button("×", () => { rows.splice(index, 1); render(); }, "icon-button danger"))));
  };
  const add = pulse => rows.push({remote: input(pulse.remote || "", {placeholder: "Device default"}), key: input(pulse.key || "", {required: true, placeholder: "KEY_VOLUMEUP"}),
    count: input(pulse.count ?? 1, {type: "number", min: 1, max: 300, required: true}),
    gap: input(pulse.gap ?? .15, {type: "number", min: .02, max: 5, step: .01, required: true})});
  initial.forEach(add); render();
  container.append(list, button("＋ Add IR pulse", () => { add({}); render(); }, "text-button"));
  return {element: container, read: () => rows.map(row => ({key: row.key.value, ...(row.remote.value ? {remote: row.remote.value} : {}), count: Number(row.count.value), gap: Number(row.gap.value)}))};
}

const hardwareForm = document.getElementById("hardware-form");
if (hardwareForm) {
  const data = readJSON("hardware-data");
  const methodSelect = document.getElementById("control-method");
  const root = document.getElementById("control-settings");
  const cache = {[data.method]: data.settings};
  let method = data.method, readSettings = () => ({});
  function renderSettings(settings) {
    root.replaceChildren();
    if (method === "none") {
      root.append(node("p", {class: "hint"}, "Keep this device in your inventory. Control connected computers through your KVM; this entry does not send commands."));
      readSettings = () => ({});
    } else if (method === "usb_hid") {
      const isSerial = settings.transport === "ch9328";
      const adapters = readJSON("serial-devices");
      const transport = select([["gadget", "Raspberry Pi USB gadget"], ["ch9328", "CH9328 USB-to-serial bridge"]], settings.transport || "gadget");
      transport.addEventListener("change", () => {
        const current = readSettings();
        renderSettings({...current, transport: transport.value,
          device: transport.value === "ch9328" ? (adapters.length === 1 ? adapters[0].path : "") : "/dev/hidg0"});
      });
      const device = input(settings.device || (isSerial ? "" : "/dev/hidg0"), {required: true,
        placeholder: isSerial ? "/dev/serial/by-id/…" : "/dev/hidg0"});
      if (isSerial) device.setAttribute("list", "serial-adapter-paths");
      const consumerDevice = input(settings.consumer_device || "/dev/hidg1", {required: true});
      const delay = input(settings.key_delay ?? .08, {type: "number", min: .02, max: .5, step: .01, required: true});
      const confirmed = checkbox("USB keyboard wiring and hotkey sequence verified", !!settings.confirmed);
      root.append(node("p", {class: "hint"}, "Sends two left-Ctrl taps and a top-row port number. One KVM is supported per controller."),
        field("Keyboard connection", transport),
        node("div", {class: "form-grid"}, field(isSerial ? "Serial adapter device" : "USB keyboard device", device,
          isSerial ? "Prefer /dev/serial/by-id/ or /dev/serial/by-path/ so reconnecting keeps the same device." : ""),
          isSerial ? null : field("USB media control device", consumerDevice), field("Time between keys (seconds)", delay)), confirmed.element);
      if (isSerial) {
        root.append(node("datalist", {id: "serial-adapter-paths"}, adapters.map(adapter => node("option", {value: adapter.path}, adapter.node))),
          node("p", {class: "hint"}, "CH9328 Mode 3: switches 1 ON, 2 OFF, 3 ON, 4 ON; 9600 baud. Supports KVM switching and keyboard macros. Standard media keys require USB HID Consumer Control, which this bridge cannot transmit. A media-capable bridge or the Pi Consumer Control gadget is required."),
          node("p", {class: "hint"}, adapters.length ? adapters.map(adapter => `${adapter.path}${adapter.writable ? "" : " — serial permissions need attention"}`).join(" · ") : "No USB serial adapters found on this controller. Connect the adapter and reload, or enter its path manually."));
      }
      readSettings = () => ({transport: transport.value, device: device.value,
        ...(isSerial ? {baudrate: 9600} : {consumer_device: consumerDevice.value}),
        key_delay: Number(delay.value), confirmed: confirmed.control.checked});
    } else if (method === "smartthings") {
      const controls = {};
      const defaults = {device_id: "", component: "main", capability: "samsungvd.mediaInputSource", command: "setInputSource", attribute: "inputSource"};
      const labels = {device_id: "SmartThings device ID", component: "Component", capability: "Input capability", command: "Set-input command", attribute: "Input status attribute"};
      const grid = node("div", {class: "form-grid"});
      for (const key of Object.keys(defaults)) { controls[key] = input(settings[key] ?? defaults[key]); grid.append(field(labels[key], controls[key])); }
      const timeout = input(settings.verify_timeout ?? 15, {type: "number", min: 1, max: 60, required: true});
      grid.append(field("Verification timeout (seconds)", timeout));
      const verified = checkbox("Capability and input values verified on this device", !!settings.confirmed);
      const list = node("div"); let rows = [];
      const render = () => list.replaceChildren(...rows.map((row, index) => node("div", {class: "mapping-row"}, field("Input name", row.name), field("SmartThings value", row.value), button("×", () => { rows.splice(index, 1); render(); }, "icon-button danger"))));
      const add = (name = "", value = "") => rows.push({name: input(name, {required: true, placeholder: "hdmi1"}), value: input(value, {required: true, placeholder: "Discovered API value"})});
      Object.entries(settings.inputs || {}).forEach(([name, value]) => add(name, value)); render();
      root.append(node("p", {class: "hint"}, "Discover the actual capability and input values in Settings. Example capability names may differ from your monitor."), grid, verified.element, node("h3", {}, "Input mappings"), list, button("＋ Add input", () => { add(); render(); }));
      readSettings = () => {
        const inputs = {};
        rows.forEach(row => { if (Object.hasOwn(inputs, row.name.value)) throw new Error("Input names must be unique."); inputs[row.name.value] = row.value.value; });
        return {...Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value])), inputs, confirmed: verified.control.checked, verify_timeout: Number(timeout.value)};
      };
    } else if (method === "ir") {
      const remote = input(settings.remote || "", {placeholder: "oppo_ha1"});
      root.append(field("LIRC remote name", remote, "The remote name in the learned lircd configuration on the Pi."), node("h3", {}, "Commands"), node("p", {class: "hint"}, "Define named actions using learned IR keys. Verified means you have tested the real hardware. Discrete power means the command reliably selects on or off, rather than toggling."));
      const list = node("div", {class: "macro-list"}); let commands = [];
      function addCommand(name = "", value = {}) {
        const sequence = pulsesEditor(value.sequence || []);
        commands.push({name: input(name, {placeholder: "power_on", required: true}),
          verified: checkbox("Verified", !!value.verified), discrete: checkbox("Discrete power command", !!value.discrete), sequence});
      }
      function renderCommands() {
        list.replaceChildren(...commands.map((row, index) => node("div", {class: "macro"},
          node("div", {class: "macro-heading"}, field("Action name", row.name), row.verified.element, row.discrete.element,
            button("Remove", () => { commands.splice(index, 1); renderCommands(); }, "text-button danger")), row.sequence.element)));
      }
      Object.entries(settings.commands || {}).forEach(([name, value]) => addCommand(name, value)); renderCommands();
      root.append(list, node("div", {class: "stack-buttons"},
        button("＋ Add command", () => { addCommand(); renderCommands(); }),
        button("＋ Add volume commands", () => {
          for (const name of ["volume_up", "volume_down"]) {
            if (!commands.some(row => row.name.value === name)) addCommand(name, {sequence: [{count: 1, gap: .15}]});
          }
          renderCommands();
          hardwareForm.dispatchEvent(new Event("input", {bubbles: true}));
        })), node("p", {class: "hint"}, "Volume commands are for the dial and slider. Enter the actual learned IR keys, keep count at 1, then verify on hardware. Adding names does not learn or verify the codes."));
      const approximate = checkbox("Allow calibrated approximate volume", !!settings.allow_approximate_volume,
        "The HA-1 has a motorized analog volume knob. IR alone cannot guarantee an exact dB reading.");
      root.append(node("hr"), node("h3", {}, "Volume presets"), approximate.element);
      const presetList = node("div", {class: "macro-list"}); let presets = [];
      function addPreset(value = {}) {
        presets.push({db: input(value.db ?? -32, {type: "number", step: .1, min: -120, max: 20, required: true}),
          calibrated: checkbox("Calibrated on hardware", !!value.calibrated),
          reference: checkbox("Sequence establishes its own reference", !!value.starts_from_known_reference), sequence: pulsesEditor(value.sequence || [])});
      }
      function renderPresets() {
        presetList.replaceChildren(...presets.map((row, index) => node("div", {class: "macro"},
          node("div", {class: "macro-heading"}, field("Target (dB)", row.db), row.calibrated.element, row.reference.element,
            button("Remove", () => { presets.splice(index, 1); renderPresets(); }, "text-button danger")), row.sequence.element)));
      }
      (settings.volume_presets || []).forEach(addPreset); renderPresets();
      root.append(presetList, button("＋ Add volume preset", () => { addPreset(); renderPresets(); }));
      readSettings = () => {
        const macros = {};
        commands.forEach(row => { if (Object.hasOwn(macros, row.name.value)) throw new Error("Command names must be unique."); macros[row.name.value] = {verified: row.verified.control.checked, discrete: row.discrete.control.checked, sequence: row.sequence.read()}; });
        return {remote: remote.value, commands: macros, allow_approximate_volume: approximate.control.checked,
          volume_presets: presets.map(row => ({db: Number(row.db.value), calibrated: row.calibrated.control.checked, starts_from_known_reference: row.reference.control.checked, sequence: row.sequence.read()}))};
      };
    }
  }
  renderSettings(data.settings);
  methodSelect.addEventListener("change", () => {
    try { cache[method] = readSettings(); } catch (_) { /* unfinished edits are retained in the current DOM until method changes */ }
    method = methodSelect.value; renderSettings(cache[method] || {});
  });
  hardwareForm.addEventListener("submit", event => {
    try { document.getElementById("settings-json").value = JSON.stringify(readSettings()); }
    catch (error) { event.preventDefault(); failForm(hardwareForm, error.message); }
  });
}

const inputTestForm = document.getElementById("smartthings-test-form");
if (inputTestForm && hardwareForm) {
  const result = document.getElementById("smartthings-test-result");
  const buttons = [...inputTestForm.querySelectorAll("button")];
  let dirty = false, busy = false;
  const show = (message, kind = "hint") => { result.textContent = message; result.className = kind; };
  const sync = () => buttons.forEach(button => button.disabled = dirty || busy);
  const markDirty = () => {
    dirty = true; sync();
    if (!busy) show("Save hardware, then reopen this page to test your updated mappings.");
  };
  hardwareForm.addEventListener("input", markDirty);
  hardwareForm.addEventListener("change", markDirty);
  hardwareForm.addEventListener("click", event => { if (event.target.closest("button[type=button]")) markDirty(); });
  inputTestForm.addEventListener("submit", async event => {
    event.preventDefault();
    if (dirty || busy || !event.submitter) return;
    const chosen = event.submitter.value;
    const data = new FormData(inputTestForm);
    data.set("input", chosen);
    busy = true; sync(); inputTestForm.setAttribute("aria-busy", "true");
    show(`Testing ${chosen}… Waiting for SmartThings confirmation.`);
    try {
      const response = await fetch(inputTestForm.action, {method: "POST", body: data});
      if (response.redirected) throw new Error("Your session expired. Reload and sign in before testing again.");
      if (!(response.headers.get("content-type") || "").includes("application/json")) {
        throw new Error("The test could not be confirmed. Reload the page before trying again.");
      }
      const reply = await response.json();
      show(reply.message, response.ok && reply.ok ? "notice success" : "notice error");
    } catch (error) {
      show(`${error.message} Check the monitor before retrying; the input may have changed.`, "notice error");
    } finally {
      busy = false; sync(); inputTestForm.removeAttribute("aria-busy");
      if (dirty) result.append(" Save your edits and reopen this page before another test.");
    }
  });
}

const taskForm = document.getElementById("task-form");
if (taskForm) {
  const keepActiveTask = document.getElementById("keep-active-task");
  const updateTaskMode = () => {
    for (const group of taskForm.querySelectorAll("[data-active-task-mappings]")) {
      group.hidden = keepActiveTask.checked;
      group.disabled = keepActiveTask.checked;
    }
  };
  keepActiveTask.addEventListener("change", updateTaskMode);
  updateTaskMode();
  const data = readJSON("task-data");
  let steps = data.steps;
  const inventory = data.inventory;
  const list = document.getElementById("action-list");
  const kinds = [["ir", "Send IR command"], ["monitor", "Select monitor input"], ["volume", "Set volume preset"], ["kvm", "Select KVM port"], ["wait", "Wait"]];
  function defaultStep(kind) {
    if (kind === "wait") return {kind, seconds: 1};
    if (kind === "kvm") return {kind, port: 1};
    const method = kind === "monitor" ? "smartthings" : "ir";
    const device = Object.keys(inventory).find(id => inventory[id].method === method) || "";
    const step = {kind, device}; setValue(step); return step;
  }
  function setValue(step) {
    const cfg = inventory[step.device]?.settings || {};
    if (step.kind === "ir") step.command = Object.keys(cfg.commands || {})[0] || "";
    if (step.kind === "monitor") step.input = Object.keys(cfg.inputs || {})[0] || "";
    if (step.kind === "volume") step.db = cfg.volume_presets?.[0]?.db ?? -32;
  }
  function render() {
    list.replaceChildren(...steps.map((step, index) => {
      const controls = node("div", {class: "action-controls"});
      controls.append(field("Action", select(kinds, step.kind, event => { steps[index] = defaultStep(event.target.value); render(); })));
      if (step.kind === "wait") {
        controls.append(field("Seconds", input(step.seconds, {type: "number", min: 0, max: 60, step: .1, required: true, oninput: event => step.seconds = Number(event.target.value)})));
      } else if (step.kind === "kvm") {
        const kvm = Object.values(inventory).find(item => item.method === "usb_hid");
        controls.append(field(kvm?.name || "Add a KVM in Hardware first", select([1,2,3,4].map(port => [String(port), `Port ${port}`]), String(step.port), event => step.port = Number(event.target.value))));
      } else {
        const method = step.kind === "monitor" ? "smartthings" : "ir";
        const options = Object.entries(inventory).filter(([,item]) => item.method === method).map(([id,item]) => [id,item.name]);
        controls.append(field("Hardware", select(options.length ? options : [["", "Add compatible hardware first"]], step.device, event => { step.device = event.target.value; setValue(step); render(); })));
        const cfg = inventory[step.device]?.settings || {};
        let values, property;
        if (step.kind === "ir") { property = "command"; values = Object.keys(cfg.commands || {}).map(key => [key, key.replaceAll("_", " ")]); }
        else if (step.kind === "monitor") { property = "input"; values = Object.keys(cfg.inputs || {}).map(key => [key,key]); }
        else { property = "db"; values = (cfg.volume_presets || []).map(preset => [String(preset.db), `${preset.db} dB`]); }
        controls.append(field(step.kind === "volume" ? "Volume target" : "Command / input", select(values.length ? values : [["", "Configure hardware actions first"]], String(step[property]), event => step[property] = property === "db" ? Number(event.target.value) : event.target.value)));
      }
      const move = delta => { const target = index + delta; if (target >= 0 && target < steps.length) { [steps[index], steps[target]] = [steps[target], steps[index]]; render(); } };
      const up = button("↑", () => move(-1), "icon-button"); up.disabled = index === 0; up.setAttribute("aria-label", `Move action ${index+1} up`);
      const down = button("↓", () => move(1), "icon-button"); down.disabled = index === steps.length - 1; down.setAttribute("aria-label", `Move action ${index+1} down`);
      const remove = button("×", () => { steps.splice(index, 1); render(); }, "icon-button danger"); remove.setAttribute("aria-label", `Remove action ${index+1}`);
      return node("div", {class: "action-row"}, node("span", {class: "action-number"}, String(index + 1).padStart(2, "0")), controls, node("div", {class: "action-tools"}, up, down, remove));
    }));
    if (!steps.length) list.append(node("p", {class: "hint"}, "Add at least one action to this task."));
  }
  render();
  document.getElementById("add-action").addEventListener("click", () => { steps.push(defaultStep("wait")); render(); });
  taskForm.addEventListener("submit", event => {
    if (!steps.length) { event.preventDefault(); failForm(taskForm, "Add at least one action."); return; }
    document.getElementById("steps-json").value = JSON.stringify(steps);
  });
}

// This checks the configuration server, not physical hardware power or the listener.
const connectionStatus = document.getElementById("connection-status");
if (connectionStatus) {
  const label = connectionStatus.querySelector(".connection-state");
  const setConnection = (state, text) => {
    connectionStatus.dataset.state = state;
    label.textContent = text;
  };
  async function checkConnection() {
    const abort = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 4000);
    try {
      const response = await fetch(connectionStatus.dataset.healthUrl, {
        cache: "no-store", credentials: "same-origin", signal: abort.signal,
      });
      if (response.redirected || response.status === 401 || response.status === 403) {
        setConnection("unknown", "Session expired · sign in again");
      } else if (!response.ok) {
        setConnection("offline", "Unavailable");
      } else {
        const data = await response.json();
        if (data.status === "online") setConnection("online", "Online");
        else setConnection("unknown", "Status unavailable");
      }
    } catch {
      setConnection("offline", "Disconnected");
    } finally {
      clearTimeout(timeout);
      setTimeout(checkConnection, 10000);
    }
  }
  checkConnection();
}

if (taskForm) {
  const controlInventory = JSON.parse(document.getElementById("task-data").textContent).inventory;
  document.querySelectorAll("[data-control-mapping]").forEach(group => {
    const mode = group.querySelector("[data-control-mode]");
    const device = group.querySelector("[data-control-device]");
    const commands = group.querySelectorAll("[data-control-command]");
    const clickMapping = group.querySelector('[data-click-mapping]');
    const sourceField = clickMapping?.querySelector('[data-click-sources]');
    const sourceList = clickMapping?.querySelector('[data-click-source-list]');
    const addSource = clickMapping?.querySelector('[data-add-volume-source]');
    const sources = sourceField ? JSON.parse(sourceField.value) : [];
    function saveSources() {
      sourceField.value = JSON.stringify(sources);
      sourceField.dispatchEvent(new Event('change', {bubbles: true}));
    }
    function renderSources() {
      if (!sourceList) return;
      sourceList.replaceChildren();
      sources.forEach((source, index) => {
        const row = document.createElement('fieldset');
        row.className = 'control-mapping';
        const legend = document.createElement('legend');
        legend.textContent = index === 0 ? 'Source 1 · Default' : `Source ${index + 1}`;
        row.append(legend);
        const fields = document.createElement('div');
        fields.className = 'form-grid';
        function field(label, options, value, changed) {
          const wrapper = document.createElement('label');
          wrapper.textContent = label;
          const select = document.createElement('select');
          select.required = true;
          select.replaceChildren(...options.map(([key, text]) => new Option(text, key)));
          select.value = value || '';
          select.addEventListener('change', () => { changed(select.value); saveSources(); });
          wrapper.append(select);
          fields.append(wrapper);
        }
        const devices = Object.entries(controlInventory).filter(([, item]) => item.method === 'ir');
        field('Volume device', [['', 'Choose IR hardware'], ...devices.map(([id, item]) => [id, item.name])], source.device, value => {
          source.device = value;
          const available = controlInventory[value]?.settings?.commands || {};
          source.increase = 'volume_up' in available ? 'volume_up' : '';
          source.decrease = 'volume_down' in available ? 'volume_down' : '';
          renderSources();
        });
        const available = Object.keys(controlInventory[source.device]?.settings?.commands || {});
        for (const direction of ['increase', 'decrease']) {
          field(direction === 'increase' ? 'Volume increase' : 'Volume decrease',
            [['', 'Choose command'], ...available.map(key => [key, key.replaceAll('_', ' ')])],
            source[direction], value => { source[direction] = value; });
        }
        row.append(fields);
        for (const [label, offset] of [['Move up', -1], ['Move down', 1], ['Remove', 0]]) {
          const button = document.createElement('button');
          button.type = 'button'; button.className = 'button small'; button.textContent = label;
          button.setAttribute('aria-label', `${label} source ${index + 1}`);
          button.disabled = offset === 0 ? sources.length === 1 : (index + offset < 0 || index + offset >= sources.length);
          button.addEventListener('click', () => {
            if (offset) [sources[index], sources[index + offset]] = [sources[index + offset], sources[index]];
            else sources.splice(index, 1);
            saveSources(); renderSources();
            const nextRow = sourceList.children[Math.min(index + offset, sources.length - 1)];
            (nextRow?.querySelector('select') || addSource).focus();
          });
          row.append(button);
        }
        sourceList.append(row);
      });
      addSource.disabled = sources.length >= 100;
    }
    function updateClick() {
      if (!clickMapping) return;
      const enabled = mode.value === 'volume';
      clickMapping.hidden = !enabled;
      if (!sources.length) sources.push({device: '', increase: '', decrease: ''});
      sourceField.disabled = !enabled;
      renderSources();
      sourceList.querySelectorAll('select').forEach(input => { input.disabled = !enabled; });
    }
    addSource?.addEventListener('click', () => {
      if (sources.length >= 100) return;
      sources.push({device: '', increase: '', decrease: ''});
      saveSources(); renderSources();
      sourceList.lastElementChild.querySelector('select').focus();
    });
    const hint = document.createElement("p");
    hint.className = "hint";
    group.append(hint);
    function updateCommands() {
      const available = Object.keys(controlInventory[device.value]?.settings?.commands || {});
      commands.forEach(select => {
        const old = select.value;
        const preferred = select.dataset.controlCommand === "increase" ? "volume_up" : "volume_down";
        select.replaceChildren(new Option("Choose command", ""), ...available.map(key => new Option(key.replaceAll("_", " "), key)));
        select.value = available.includes(old) ? old : available.includes(preferred) ? preferred : "";
      });
      hint.textContent = !clickMapping && mode.value === "volume" && device.value &&
        !(available.includes("volume_up") && available.includes("volume_down"))
        ? "Volume commands are not configured under the standard names. In Hardware, use Add volume commands and enter the learned IR keys, or select your existing volume commands here." : "";
    }
    function updateMode() {
      const volumeList = clickMapping && mode.value === "volume";
      group.querySelectorAll("[data-directional-field]").forEach(field => { field.hidden = !!volumeList; });
      const enabled = mode.value !== "" && !volumeList;
      [device, ...commands].forEach(input => { input.disabled = !enabled; input.required = enabled; });
      updateClick();
    }
    device.addEventListener("change", updateCommands);
    mode.addEventListener("change", () => { updateMode(); updateCommands(); });
    updateCommands(); updateMode();
  });
}

// Overview controls deep-link to the saved input settings, including advanced fields.
function revealControlInput() {
  const id = window.location.hash.slice(1);
  if (!["dial-input", "slider-input"].includes(id)) return;
  const target = document.getElementById(id);
  if (!target) return;
  target.querySelectorAll("details").forEach(details => { details.open = true; });
  target.focus({preventScroll: true});
  target.scrollIntoView({block: "start"});
}
window.addEventListener("hashchange", revealControlInput);
revealControlInput();

const developerConsole = document.getElementById("developer-console");
if (developerConsole) {
  const get = id => document.getElementById(`console-${id}`);
  let events = [], cursor = 0, clearedThrough = 0, paused = false;
  const filtered = () => events.filter(event => {
    const query = get("search").value.trim().toLowerCase();
    const level = get("level").value;
    return (!query || JSON.stringify(event).toLowerCase().includes(query)) &&
      (!get("category").value || event.category === get("category").value) &&
      (!level || event.level === level || (level === "warning" && event.level === "error"));
  });
  function render() {
    const visible = filtered();
    get("count").textContent = `${visible.length} shown · ${events.length} loaded`;
    // Keep expanded rows open while new events arrive.
    const opened = new Set([...get("events").querySelectorAll("details[open]")].map(row => row.dataset.id));
    get("events").replaceChildren(...visible.slice().reverse().map(event => {
      const time = new Date(event.time * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false, fractionalSecondDigits: 3});
      const detail = event.details;
      const context = [Array.isArray(detail.names) ? detail.names.join(" / ") : detail.names,
        detail.action, detail.scene, detail.control, detail.direction, detail.step?.device,
        detail.step?.command, detail.step?.input, detail.step?.port ? `port ${detail.step.port}` : null,
        detail.step?.db !== undefined ? `${detail.step.db} dB` : null, detail.reason,
        detail.target?.remote, detail.target?.path, detail.target?.device_id].filter(Boolean).join(" · ");
      const summary = node("summary", {}, node("time", {title: new Date(event.time * 1000).toISOString()}, time),
        node("span", {class: `console-severity ${event.level}`}, event.level),
        node("span", {class: "console-category"}, event.category),
        node("strong", {}, event.message, context ? node("span", {class: "console-context"}, context) : null),
        node("span", {class: "console-mode"}, event.details.live === true ? "LIVE" : event.details.live === false ? "DRY RUN" : ""));
      const row = node("details", {class: "console-event", "data-id": String(event.id), open: opened.has(String(event.id))}, summary);
      if (event.trace) row.append(button(`Trace ${event.trace.slice(0, 8)} ↗`, () => {
        get("search").value = event.trace;
        get("category").value = "";
        get("level").value = "";
        render();
      }, "text-button console-trace"));
      row.append(node("pre", {}, JSON.stringify(event.details, null, 2)));
      return row;
    }));
    if (!visible.length) get("events").append(node("p", {class: "console-empty"}, events.length ?
      "No events match these filters." : "No events yet. Press a key on the configured numpad while its listener is running."));
  }
  function listeners(data) {
    const rows = data.expected_inputs.map(path => {
      const listener = data.listeners.find(item => item.path === path);
      const state = !listener ? "Not reporting" : listener.stale ? "Stale — listener may be stopped" : listener.state;
      return node("div", {class: "console-listener"}, node("strong", {}, state),
        node("span", {}, listener ? `${listener.live ? "LIVE" : "DRY RUN"} · last report ${Math.floor(listener.age)}s ago` : "Start or restart the controller with the shared configuration"),
        node("code", {}, path || "No input selected"), listener?.error ? node("span", {class: "console-listener-error"}, listener.error) : null);
    });
    for (const listener of data.listeners.filter(item => !item.stale && item.state !== "stopped" && !data.expected_inputs.includes(item.path))) {
      rows.push(node("div", {class: "notice"}, `Listener is using another input: ${listener.path}. Restart it to apply the saved selection.`));
    }
    get("listeners").replaceChildren(...rows);
  }
  async function poll() {
    try {
      const response = await fetch(`${developerConsole.dataset.eventsUrl}?after=${cursor}`, {cache: "no-store", signal: AbortSignal.timeout(8000)});
      if (response.redirected) throw new Error("Session expired. Sign in again to view events.");
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "Console connection failed.");
      listeners(data);
      if (!paused) {
        const changed = data.cursor !== cursor;
        if (data.cursor < cursor) { events = []; clearedThrough = 0; }
        const known = new Set(events.map(event => event.id));
        events.push(...data.events.filter(event => event.id > clearedThrough && !known.has(event.id)));
        events = events.slice(-2000);
        cursor = data.cursor;
        get("gap").hidden = get("gap").hidden && !data.gap;
        if (changed) render();
      }
      get("connection").textContent = paused ? "Paused · listener status still updates" : "Connected · refreshing every second";
    } catch (error) {
      get("connection").textContent = `${error.message} Retrying…`;
    } finally {
      window.setTimeout(poll, 1000);
    }
  }
  for (const id of ["search", "category", "level"]) get(id).addEventListener("input", render);
  get("pause").addEventListener("click", () => {
    paused = !paused;
    get("pause").textContent = paused ? "Resume" : "Pause";
    get("pause").setAttribute("aria-pressed", String(paused));
    get("connection").textContent = paused ? "Paused · listener status still updates" : "Resuming…";
  });
  get("clear").addEventListener("click", () => {
    clearedThrough = cursor; events = []; get("gap").hidden = true; render();
  });
  get("export").addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(filtered(), null, 2)], {type: "application/json"});
    const url = URL.createObjectURL(blob);
    const link = node("a", {href: url, download: "desk-console.json"});
    document.body.append(link); link.click(); link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  render(); poll();
}

// Native form submission keeps the bounded capture usable without JavaScript.
for (const form of document.querySelectorAll("[data-ir-learn]")) {
  form.addEventListener("submit", () => {
    form.querySelector("button").disabled = true;
    form.querySelector("[data-learning-status]").textContent = "Listening… briefly press the remote button now. This can take up to 12 seconds.";
  });
}

// Give immediate feedback while a read-only task probe waits for the network.
for (const form of document.querySelectorAll('[data-connection-check]')) {
  const button = form.querySelector('button');
  const progress = form.querySelector('[data-check-progress]');
  form.addEventListener('submit', () => {
    button.disabled = true;
    button.textContent = 'Checking connections…';
    form.setAttribute('aria-busy', 'true');
    progress.hidden = false;
    progress.textContent = 'Reading connection status. SmartThings may take several seconds; no hardware commands are being sent.';
  });
  window.addEventListener('pageshow', event => {
    if (!event.persisted) return;
    button.disabled = false;
    button.textContent = 'Check connections';
    form.removeAttribute('aria-busy');
    progress.hidden = true;
  });
}

// Link task cards and physical key mappings on hover and keyboard focus.
// This is a visual preview only; it never sends a controller command.
const mappingFocus = document.getElementById('mapping-focus');
const mappingSelector = '[data-linked-task], [data-command-task]';
const mappingTask = element => element.dataset.linkedTask || element.dataset.commandTask;
let hoveredTask = null;
let focusedTask = null;
function updateTaskHighlight() {
  const linkedTasks = Array.from(document.querySelectorAll(mappingSelector));
  const source = hoveredTask || focusedTask;
  for (const element of linkedTasks) {
    element.classList.toggle('is-linked', Boolean(source && mappingTask(element) === mappingTask(source)));
  }
  if (mappingFocus) {
    if (!source) mappingFocus.textContent = 'Hover or focus a task or key to see its mapping.';
    else if (source.dataset.keyCommand) {
      mappingFocus.textContent = `${source.dataset.taskLabel} · Key ${source.textContent.trim()} · ${source.dataset.keyCommand}`;
    } else if (!mappingTask(source)) {
      mappingFocus.textContent = `Key ${source.textContent.trim()} · Unassigned`;
    } else {
      const keys = linkedTasks.filter(element => element.classList.contains('key') && element.dataset.linkedTask === mappingTask(source));
      const commands = linkedTasks.filter(element => element.dataset.commandTask === mappingTask(source));
      mappingFocus.textContent = `${source.dataset.taskLabel} · ${keys.length ? 'Key ' + keys.map(key => key.textContent.trim()).join(', ') : 'No key assigned'}${commands.length ? ' · Keyboard: ' + commands.map(key => key.textContent.trim()).join(', ') : ''}`;
    }
  }
}
// Delegate so cards refreshed in the background retain their mapping previews.
for (const type of ['pointerover', 'pointerout', 'focusin', 'focusout']) {
  document.addEventListener(type, event => {
    const element = event.target.closest(`${mappingSelector}, [data-virtual-key]`);
    if (!element || element.contains(event.relatedTarget)) return;
    if (type === 'pointerover') hoveredTask = element;
    if (type === 'pointerout') hoveredTask = null;
    if (type === 'focusin') focusedTask = element;
    if (type === 'focusout') focusedTask = null;
    updateTaskHighlight();
  });
}
document.addEventListener('overview-updated', () => {
  if (!hoveredTask?.isConnected) hoveredTask = null;
  focusedTask = document.activeElement?.closest(`${mappingSelector}, [data-virtual-key]`);
  document.querySelectorAll('.is-linked').forEach(element => element.classList.remove('is-linked'));
  updateTaskHighlight();
});
document.addEventListener('numpad-mappings-updated', () => {
  document.querySelectorAll('.is-linked').forEach(element => element.classList.remove('is-linked'));
  updateTaskHighlight();
});
// Reveal collapsed settings when browser validation needs an input inside them.
document.addEventListener('invalid', event => {
  for (let parent = event.target.parentElement; parent; parent = parent.parentElement) {
    if (parent.tagName === 'DETAILS') parent.open = true;
  }
}, true);

// Raw slider discovery follows the selected numpad rather than a hidraw number.
for (const fieldset of document.querySelectorAll('.control-mapping')) {
  const mode = fieldset.querySelector('select[name$="_input_mode"]');
  if (!mode) continue;
  const renderInput = () => {
    const raw = mode.value === 'gmmk_raw';
    for (const field of fieldset.querySelectorAll('[data-control-device], [data-control-code]')) {
      field.hidden = raw;
      field.querySelector('input').disabled = raw;
    }
    const note = fieldset.querySelector('[data-raw-slider-note]');
    if (note) note.hidden = !raw;
  };
  mode.addEventListener('change', renderInput);
  renderInput();
}
