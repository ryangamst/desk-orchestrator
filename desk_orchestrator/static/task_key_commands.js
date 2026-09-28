"use strict";
(() => {
  const editor = document.getElementById('key-command-editor');
  if (!editor) return;
  const buttons = Array.from(editor.querySelectorAll('[data-command-key]'));
  const panels = Array.from(editor.querySelectorAll('[data-command-panel]'));
  if (editor.hasAttribute('data-readonly')) {
    for (const button of buttons) {
      button.addEventListener('click', () => {
        for (const key of buttons) key.setAttribute('aria-pressed', String(key === button));
        for (const panel of panels) panel.hidden = panel.dataset.commandPanel !== button.dataset.commandKey;
      });
    }
    return;
  }
  const launchKey = document.querySelector('#task-form select[name="key"]');
  const commandOptions = JSON.parse(document.getElementById('macro-command-options').textContent);
  const inventory = JSON.parse(document.getElementById('task-data').textContent).inventory;
  const macros = new Map();
  let selected = 'KEY_NUMLOCK';

  function element(tag, text, attributes = {}) {
    const item = document.createElement(tag);
    if (text) item.textContent = text;
    for (const [key, value] of Object.entries(attributes)) item.setAttribute(key, value);
    return item;
  }
  for (const panel of panels) {
    const irDevice = panel.querySelector('[data-ir-device]');
    const irCommand = panel.querySelector('[data-ir-command]');
    function irCommands(saved = '') {
      irCommand.replaceChildren(new Option('Choose command', ''));
      const device = inventory[irDevice.value];
      if (device?.method === 'ir') {
        for (const key of Object.keys(device.settings.commands || {})) {
          irCommand.add(new Option(key.replaceAll('_', ' '), key));
        }
      }
      if (saved && !Array.from(irCommand.options).some(option => option.value === saved)) {
        irCommand.add(new Option(`Missing command: ${saved}`, saved));
      }
      irCommand.value = saved;
    }
    irCommands(irCommand.dataset.savedCommand);
    irDevice.addEventListener('change', () => { irCommands(); render(); });
    irCommand.addEventListener('change', render);
    const value = panel.querySelector('[data-macro-value]');
    const name = panel.querySelector('[data-macro-label]');
    const list = panel.querySelector('[data-macro-steps]');
    const add = panel.querySelector('[data-add-macro-step]');
    let macro;
    try { macro = JSON.parse(value.value); } catch (_) { macro = null; }
    if (!macro || typeof macro !== 'object' || !Array.isArray(macro.steps)) macro = {label: '', steps: [{shortcut: ''}]};
    macro = {label: typeof macro.label === 'string' ? macro.label : '', steps: macro.steps};
    macros.set(panel.dataset.commandPanel, macro);
    name.value = macro.label;

    function save() {
      value.value = JSON.stringify(macro);
      value.dispatchEvent(new Event('change', {bubbles: true}));
      render();
    }
    function rows() {
      list.replaceChildren();
      macro.steps.forEach((step, index) => {
        const row = element('fieldset', '', {class: 'macro-step'});
        row.append(element('legend', `Step ${index + 1}`));
        const type = element('select', '', {'aria-label': `Step ${index + 1} type`});
        for (const [key, label] of [['shortcut', 'Keyboard shortcut'], ['wait', 'Delay']]) type.add(new Option(label, key));
        const kind = step && typeof step === 'object' && 'command' in step ? 'command' :
          step && typeof step === 'object' && 'wait' in step ? 'wait' : 'shortcut';
        if (kind === 'command') type.add(new Option('Saved legacy command', 'command'));
        type.value = kind;
        const field = element('label', kind === 'shortcut' ? 'Shortcut' : kind === 'wait' ? 'Seconds' : 'Command');
        const input = element(kind === 'command' ? 'select' : 'input');
        if (kind === 'command') {
          input.add(new Option(commandOptions[step.command]?.[0] || 'Unknown command', step.command));
        } else if (kind === 'wait') {
          input.type = 'number'; input.min = '0'; input.max = '60'; input.step = '0.01';
        } else {
          input.type = 'text'; input.maxLength = 200; input.placeholder = 'e.g. Win+R or Cmd+Space';
          input.setAttribute('list', 'shortcut-key-names');
        }
        input.value = step?.[kind] ?? '';
        input.addEventListener('input', () => {
          macro.steps[index] = {[kind]: kind === 'wait' ? (input.value === '' ? null : Number(input.value)) : input.value};
          save();
        });
        // Select changes must also work with keyboard and assistive input.
        input.addEventListener('change', () => {
          macro.steps[index] = {[kind]: kind === 'wait' ? (input.value === '' ? null : Number(input.value)) : input.value};
          save();
        });
        field.append(input);
        type.addEventListener('change', () => {
          macro.steps[index] = type.value === 'wait' ? {wait: .5} : {shortcut: ''};
          rows(); save();
        });
        const actions = element('div', '', {class: 'macro-step-actions'});
        for (const [text, label, offset] of [['↑', 'Move up', -1], ['↓', 'Move down', 1], ['Remove', 'Remove', 0]]) {
          const button = element('button', text, {type: 'button', class: 'button small', 'aria-label': `${label} step ${index + 1}`});
          button.disabled = offset === -1 && index === 0 || offset === 1 && index === macro.steps.length - 1;
          button.dataset.edgeDisabled = String(button.disabled);
          button.addEventListener('click', () => {
            if (!offset) macro.steps.splice(index, 1);
            else [macro.steps[index], macro.steps[index + offset]] = [macro.steps[index + offset], macro.steps[index]];
            rows(); save();
          });
          actions.append(button);
        }
        row.append(type, field, actions);
        list.append(row);
      });
      add.dataset.edgeDisabled = String(macro.steps.length >= 100);
    }
    add.addEventListener('click', () => { if (macro.steps.length < 100) { macro.steps.push({shortcut: ''}); rows(); save(); } });
    name.addEventListener('input', () => { macro.label = name.value; save(); });
    rows();
  }

  function render() {
    for (const button of buttons) {
      const code = button.dataset.commandKey;
      const panel = panels.find(item => item.dataset.commandPanel === code);
      const select = panel.querySelector('select');
      const reason = button.dataset.reservedReason ||
        (launchKey?.value === code ? 'Selected to launch this task' : '');
      // Keep a conflicting saved value editable so it can be cleared explicitly.
      select.disabled = Boolean(reason && !select.value);
      for (const option of select.options) option.disabled = Boolean(reason && option.value);
      const custom = select.value === 'custom';
      const ir = select.value === 'ir';
      const irEditor = panel.querySelector('[data-ir-editor]');
      const irDevice = panel.querySelector('[data-ir-device]');
      const irCommand = panel.querySelector('[data-ir-command]');
      irEditor.hidden = !ir;
      // Values on other key panels must still be submitted when hidden.
      for (const input of irEditor.querySelectorAll('select')) input.disabled = !ir || Boolean(reason);
      const irLabel = `IR · ${inventory[irDevice.value]?.name || irDevice.value || 'Choose device'}: ${irCommand.value || 'Choose command'}`;
      const macroEditor = panel.querySelector('[data-macro-editor]');
      macroEditor.hidden = !custom;
      for (const input of macroEditor.querySelectorAll('input:not([type="hidden"]), select, button')) {
        input.disabled = !custom || code !== selected || Boolean(reason) || input.dataset.edgeDisabled === 'true';
      }
      const command = ir ? irLabel : custom ? macros.get(code).label.trim() || macros.get(code).steps.map(step => step?.shortcut || (step && 'wait' in step ? `Wait ${step.wait ?? '?'}s` : commandOptions[step?.command]?.[0] || 'Empty shortcut')).join(' → ') || 'Keyboard sequence' : select.selectedOptions[0]?.textContent || 'Unassigned';
      const output = panel.querySelector('[data-command-output]');
      if (output) {
        const unsupported = new Set();
        const steps = ir ? [] : custom ? macros.get(code).steps : select.value ? [{command: select.value}] : [];
        const descriptions = steps.map(step => {
          if (step?.command) {
            const details = commandOptions[step.command];
            if (details?.[1] === 'consumer') {
              unsupported.add(details[0]);
              return details[0];
            }
            return details?.[0] || 'Unknown command';
          }
          if (step && 'wait' in step) return `Wait ${step.wait ?? '?'} seconds`;
          return step?.shortcut || 'Empty shortcut';
        });
        output.textContent = ir ? `${irLabel}. Sent through the Pi’s IR transmitter.` : unsupported.size ?
          `Requires USB Consumer Control: ${Array.from(unsupported).join(', ')}. The CH9328 bridge cannot send these media keys. Replace each saved media step with the shortcut configured on this computer, or clear the mapping. No part of this command will be sent.` :
          descriptions.length ? `Serial output: ${descriptions.join(' → ')}` : '';
        output.classList.toggle('notice', unsupported.size > 0);
      }
      panel.hidden = code !== selected;
      panel.querySelector('[data-key-reservation]').textContent = reason ?
        `${reason}. ${select.value ? 'Choose Unassigned to remove the conflicting command.' : 'This key is reserved.'}` :
        'Choose what this key sends while this task is active.';
      button.classList.toggle('mapped', Boolean(reason || select.value));
      button.setAttribute('aria-pressed', String(code === selected));
      button.title = reason || command;
      button.setAttribute('aria-label', `${button.dataset.keyLabel}: ${reason || command}`);
    }
  }
  for (const button of buttons) {
    button.addEventListener('click', () => { selected = button.dataset.commandKey; render(); });
  }
  for (const panel of panels) panel.querySelector('select').addEventListener('change', render);
  launchKey?.addEventListener('change', render);
  render();
})();
