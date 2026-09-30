// switchboard: the sign-in page (login.html; DESIGN.md §31.4).
//
// The page shows `switchboard login` until the broker says passkeys apply here (a hosted
// broker with a passkey): then one button, Sign in with a passkey. While a hosted broker is
// unclaimed it points at the claim link in the log. GET /api/auth/state needs no session and
// says nothing about the owner. The ceremony itself is window.SBWebAuthn.signIn (webauthn.js).
(function () {
  'use strict';

  const W = window.SBWebAuthn;
  function $(id) { return document.getElementById(id); }

  function error(text) {
    const e = $('login-error');
    e.textContent = text || '';
    e.classList.toggle('hidden', !text);
  }

  async function signIn() {
    error(null);
    const btn = $('passkey-btn');
    btn.disabled = true;
    btn.textContent = 'Waiting for your passkey…';
    try {
      await W.signIn();
      location.replace('/');
    } catch (e) {
      btn.disabled = false;
      btn.textContent = 'Sign in with a passkey';
      error(String(e.message || e));
    }
  }

  async function load() {
    let st;
    try {
      const r = await fetch('/api/auth/state', { credentials: 'same-origin', cache: 'no-store' });
      st = r.ok ? await r.json() : null;
    } catch (e) {
      st = null;
    }
    if (!st || !st.hosted) return;  // the desktop: `switchboard login` in your terminal
    if (st.passkeys && W.supported()) {
      $('passkey-btn').addEventListener('click', signIn);  // before it shows: a click is never lost
      $('login-cli').classList.add('hidden');
      $('login-passkey').classList.remove('hidden');
      $('passkey-btn').focus();
      return;
    }
    if (st.claim) {
      $('login-cli').classList.add('hidden');
      $('login-claim').classList.remove('hidden');
      return;
    }
    // hosted, claimed, but passkeys can't work in this browser (or none is set up): the shell
    $('login-hosted').classList.remove('hidden');
  }

  document.addEventListener('DOMContentLoaded', load);
})();
