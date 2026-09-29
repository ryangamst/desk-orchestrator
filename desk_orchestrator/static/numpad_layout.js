"use strict";
(() => {
  function render() {
    for (const board of document.querySelectorAll('[data-custom-layout]')) {
      const layout = JSON.parse(board.dataset.customLayout);
      const unit = Math.min(56, Math.max(28, ((board.parentElement.clientWidth || 300) - 32) / layout.width));
      board.style.width = `${layout.width * unit}px`;
      board.style.height = `${layout.height * unit}px`;
      for (const key of layout.keys) {
        const button = Array.from(board.children).find(b => (b.dataset.virtualKey || b.dataset.commandKey) === key.id);
        if (!button) continue;
        Object.assign(button.style, {left: `${key.x * unit + 2}px`, top: `${key.y * unit + 2}px`, width: `${key.w * unit - 4}px`, height: `${key.h * unit - 4}px`, fontSize: '13px'});
        while ((button.scrollHeight > button.clientHeight || button.scrollWidth > button.clientWidth) && parseFloat(button.style.fontSize) > 8) {
          button.style.fontSize = `${parseFloat(button.style.fontSize) - 1}px`;
        }
      }
      for (const control of layout.controls || []) {
        const element = Array.from(board.children).find(child => child.dataset.layoutControl === control.id);
        if (element) Object.assign(element.style, {left:`${control.x * unit + 2}px`, top:`${control.y * unit + 2}px`, width:`${control.w * unit - 4}px`, height:`${control.h * unit - 4}px`});
      }
    }
  }
  render();
  window.addEventListener('resize', render);
})();
