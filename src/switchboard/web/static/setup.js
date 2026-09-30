// switchboard: the claim page (setup.html; DESIGN.md §31.3).
//
// The token after # in the claim link is read once and removed from the address bar, then
// sent only in the body of the request that registers the first passkey. Then the backup
// step: a second passkey, or Skip. Both go through window.SBWebAuthn (webauthn.js).
(function () {
  'use strict';

  const W = window.SBWebAuthn;
  function $(id) { return document.getElementById(id); }

  const match = /^#t=([A-Za-z0-9_-]{20,256})$/.exec(location.hash || '');
  const token = match ? match[1] : null;
  if (location.hash) history.replaceState(null, '', location.pathname);

  function show(id) {
    for (const s of document.querySelectorAll('.setup-step')) s.classList.toggle('hidden', s.id !== id);
    const h = document.querySelector('#' + id + ' h1');
    if (h) h.setAttribute('tabindex', '-1');
    if (h) h.focus();
  }

  function error(text) {
    const e = $('setup-error');
    e.textContent = text || '';
    e.classList.toggle('hidden', !text);
  }

  function busy(button, on, label) {
    button.disabled = on;
    button.textContent = on ? 'Waiting for your passkey…' : label;
  }

  // a default name for the passkey: the platform, when the browser says
  function platformName() {
    const p = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || '';
    if (/mac/i.test(p)) return 'Mac';
    if (/win/i.test(p)) return 'Windows PC';
    if (/iphone|ipad|ios/i.test(p)) return 'iPhone';
    if (/android/i.test(p)) return 'Android phone';
    if (/linux/i.test(p)) return 'Linux machine';
    return '';
  }

  async function claim(ev) {
    ev.preventDefault();
    error(null);
    const btn = $('claim-btn');
    const name = ($('claim-name').value || '').trim() || 'passkey';
    busy(btn, true, 'Create a passkey');
    try {
      const res = await W.claim(token, name);
      $('backup-lead').textContent = 'This switchboard is yours (passkey "' + res.name + '"), and this browser is signed in.';
      show('step-backup');
    } catch (e) {
      busy(btn, false, 'Create a passkey');
      if (e.status === 403 && (e.code === 'bad_claim' || e.code === 'no_ceremony')) {
        show('step-bad');
        return;
      }
      error(String(e.message || e));
    }
  }

  async function backup(ev) {
    ev.preventDefault();
    error(null);
    const btn = $('backup-btn');
    const name = ($('backup-name').value || '').trim() || 'backup passkey';
    busy(btn, true, 'Add a backup passkey');
    try {
      await W.addPasskey(name);
      location.replace('/');
    } catch (e) {
      busy(btn, false, 'Add a backup passkey');
      error(String(e.message || e));
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    if (!token) {
      show('step-bad');
      return;
    }
    if (!W.supported()) {
      show('step-unsupported');
      return;
    }
    $('claim-name').value = platformName();
    $('claim-form').addEventListener('submit', claim);
    $('backup-form').addEventListener('submit', backup);
    show('step-claim');
  });
})();
