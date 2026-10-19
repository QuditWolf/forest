/* Shared "installed app" behaviour for all forest pages:
   - service worker (fast start, offline notice)
   - offline banner when the server can't be reached
   - on phones: hide bottom bars while the on-screen keyboard is up
   - on phones: the back gesture closes open sheets / dialogs / the drawer instead of leaving the page */
(function () {
  'use strict';
  const PHONE_Q = '(max-width: 760px), (max-height: 520px) and (pointer: coarse)';
  const isPhone = () => matchMedia(PHONE_Q).matches;

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});

  // ── offline banner ──
  const banner = document.createElement('div');
  banner.id = 'offline-banner';
  banner.textContent = 'offline: changes are not saved until the connection is back';
  banner.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:100;padding:6px 10px;text-align:center;' +
    'font:12px Menlo,Consolas,monospace;background:#b00;color:#fff;display:none';
  const setOffline = off => { banner.style.display = off ? 'block' : 'none'; };
  document.addEventListener('DOMContentLoaded', () => document.body.appendChild(banner));
  addEventListener('offline', () => setOffline(true));
  addEventListener('online', () => setOffline(false));
  const _fetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    try {
      const r = await _fetch(...args);
      setOffline(false);
      return r;
    } catch (e) {
      setOffline(true);
      throw e;
    }
  };

  // ── keyboard: give the content the space ──
  const typing = el => el && (el.tagName === 'TEXTAREA' || el.isContentEditable ||
    (el.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'submit', 'file', 'date'].includes(el.type)));
  document.addEventListener('focusin', e => { if (isPhone() && typing(e.target)) document.body.classList.add('kbd-open'); });
  document.addEventListener('focusout', () => setTimeout(() => {
    if (!typing(document.activeElement)) document.body.classList.remove('kbd-open');
  }, 50));

  // ── back gesture closes the top sheet (forest UI: .overlay.open, drawer, outline panel) ──
  const topOpen = () => document.querySelector('.overlay.open') || document.body.classList.contains('drawer-open') ||
    document.getElementById('toc-panel');
  let wasOpen = false, ignorePops = 0;
  window.__navClosing = false;   // set by navTo: closing because we're navigating, don't go back
  const watch = () => {
    const open = !!topOpen();
    if (isPhone()) {
      if (open && !wasOpen) {
        history.pushState(Object.assign({}, history.state, { sheet: 1 }), '');
      } else if (!open && wasOpen && history.state && history.state.sheet) {
        // navigating from the sheet: the page we open will take over this entry (no duplicate)
        if (window.__navClosing) history.replaceState(Object.assign({}, history.state, { sheet: 0, reuse: 1 }), '');
        else { ignorePops++; history.back(); }
      }
    }
    wasOpen = open;
  };
  document.addEventListener('DOMContentLoaded', () =>
    new MutationObserver(watch).observe(document.body, { subtree: true, attributes: true, attributeFilter: ['class'], childList: true }));
  addEventListener('popstate', e => {
    if (ignorePops) { ignorePops--; e.stopImmediatePropagation(); return; }
    if (wasOpen && topOpen()) {
      e.stopImmediatePropagation();
      wasOpen = false;   // the entry was already popped by the gesture
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
      // nested sheets (e.g. drawer under a dialog): keep one entry per remaining sheet
      setTimeout(() => { if (topOpen()) { wasOpen = true; history.pushState(Object.assign({}, history.state, { sheet: 1 }), ''); } }, 0);
    }
  }, true);

  window.forestApp = { isPhone, PHONE_Q };
})();
