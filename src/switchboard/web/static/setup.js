// switchboard: choosing how you'll sign in (setup.html; DESIGN.md §32.4).
//
// GET /api/setup/state says who this is: the admin setting the broker up (a ceremony the
// sign-in with the log's one-time password started; or the log's link, whose #t=<password> is
// read once, removed from the address bar, and sent once, in the body of that same sign-in),
// or someone on a one-time password the admin gave them. Either way: a password of their own
// (POST /api/setup/password for the admin, /api/me/password for anyone else), or a passkey
// instead (window.SBWebAuthn: the claim's own ceremony, or an added passkey), then the app.
(function () {
  'use strict';

  const W = window.SBWebAuthn;
  function $(id) { return document.getElementById(id); }

  const match = /^#t=([0-9A-Za-z -]{16,40})$/.exec(decodeURIComponent(location.hash || ''));
  const token = match ? match[1] : null;
  if (location.hash) history.replaceState(null, '', location.pathname);

  let mode = null;  // 'claim' (the admin) or 'reset' (anyone on a one-time password)

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

  function busy(on) {
    $('password-btn').disabled = on;
    $('passkey-btn').disabled = on;
  }

  // a default name for the passkey: the platform, when the browser says
  function platformName() {
    const p = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || '';
    if (/mac/i.test(p)) return 'Mac';
    if (/win/i.test(p)) return 'Windows PC';
    if (/iphone|ipad|ios/i.test(p)) return 'iPhone';
    if (/android/i.test(p)) return 'Android phone';
    if (/linux/i.test(p)) return 'Linux machine';
    return 'passkey';
  }

  async function savePassword(ev) {
    ev.preventDefault();
    error(null);
    const pw = $('new-password').value;
    if (pw !== $('new-password-2').value) {
      error('The two passwords are not the same.');
      $('new-password-2').focus();
      return;
    }
    busy(true);
    $('password-btn').textContent = 'Saving…';
    try {
      await W.post(mode === 'claim' ? '/api/setup/password' : '/api/me/password', { password: pw });
      location.replace('/');
    } catch (e) {
      busy(false);
      $('password-btn').textContent = 'Save and sign in';
      if (e.status === 403 && (e.code === 'bad_claim' || e.code === 'no_ceremony')) {
        show('step-bad');
        return;
      }
      error(String(e.message || e));
    }
  }

  async function usePasskey() {
    error(null);
    busy(true);
    const label = $('passkey-btn').querySelector('span');
    label.textContent = 'Waiting for your passkey…';
    try {
      if (mode === 'claim') {
        const res = await W.claim(null, platformName());
        $('backup-lead').textContent = 'This switchboard is set up (passkey "' + res.name + '"), and this browser is signed in.';
        show('step-backup');
        return;
      }
      await W.addPasskey(platformName());
      location.replace('/');
    } catch (e) {
      busy(false);
      label.textContent = 'Use a passkey instead';
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
    btn.disabled = true;
    btn.textContent = 'Waiting for your passkey…';
    try {
      await W.addPasskey(name);
      location.replace('/');
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Add a backup passkey';
      error(String(e.message || e));
    }
  }

  async function state() {
    const r = await fetch('/api/setup/state', { credentials: 'same-origin', cache: 'no-store' });
    return r.ok ? r.json() : { mode: 'none' };
  }

  async function load() {
    let st = await state().catch(function () { return { mode: 'none' }; });
    if (st.mode === 'link') {
      // the log's link: its one-time password starts the same ceremony a sign-in with it does
      if (!token) { show('step-bad'); return; }
      try {
        await W.post('/api/signin/password', { name: 'admin', password: token });
      } catch (e) {
        show('step-bad');
        return;
      }
      st = await state().catch(function () { return { mode: 'none' }; });
    }
    if (st.mode !== 'claim' && st.mode !== 'reset') {
      if (token) { show('step-bad'); return; }
      location.replace('/');
      return;
    }
    mode = st.mode;
    $('setup-user').value = st.email || st.human || '';  // what a password manager saves: who signs in
    $('choose-lead').textContent = mode === 'claim'
      ? 'You’re the admin of this switchboard, signed in as ' + st.human + '. Choose your own password, or a passkey instead.'
      : 'Hi ' + st.human + '. Your one-time password worked. Choose your own password, or a passkey instead.';
    const passkeys = !!(st.passkeys_work && W.supported());
    for (const id of ['choose-or', 'choose-alt', 'choose-fine']) $(id).classList.toggle('hidden', !passkeys);
    $('password-form').addEventListener('submit', savePassword);
    $('passkey-btn').addEventListener('click', usePasskey);
    $('backup-form').addEventListener('submit', backup);
    show('step-choose');
    $('new-password').focus();
  }

  document.addEventListener('DOMContentLoaded', load);
})();
