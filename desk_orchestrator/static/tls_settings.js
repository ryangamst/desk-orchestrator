/* Refresh only certificate progress, preserving edits elsewhere in Settings. */
(() => {
  const region = document.querySelector('[data-tls-operation]');
  if (!region) return;
  let lastHTML = region.innerHTML;
  async function refresh() {
    try {
      // Do not disrupt selecting/copying a TXT value or using its form.
      if (!region.contains(document.activeElement) && !window.getSelection()?.toString()) {
        const response = await fetch(region.dataset.statusUrl, {headers: {'Accept': 'text/html'}, cache: 'no-store'});
        if (response.ok && !response.redirected) {
          const html = await response.text();
          if (html.includes('data-tls-progress') && html !== lastHTML) {
            region.innerHTML = html;
            lastHTML = html;
          }
        }
      }
    } catch (_) { /* The page's connection indicator reports connectivity. */ }
    window.setTimeout(refresh, 3000);
  }
  window.setTimeout(refresh, 3000);
})();
