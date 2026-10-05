// switchboard: the sign-in page (login.html; DESIGN.md §32.4).
//
// On a desktop the page shows `switchboard login`. On a hosted broker (GET /api/auth/state,
// which needs no session and says nothing about anyone) it shows the three ways in: a name and
// a password, a passkey (when this browser and address can use one, and someone has one), and
// Sign in with Google once it's set up (#70, §38; otherwise shown as coming soon). A password sign-in that needs its person to choose their own
// first (the admin's one-time password from the log, or anyone's one-time password from the
// admin) goes on to /setup. The passkey ceremony is window.SBWebAuthn.signIn (webauthn.js).
(function () {
  'use strict';

  const W = window.SBWebAuthn;
  function $(id) { return document.getElementById(id); }

  function error(text) {
    const e = $('login-error');
    e.textContent = text || '';
    e.classList.toggle('hidden', !text);
  }

  function busy(on) {
    $('password-btn').disabled = on;
    $('passkey-btn').disabled = on;
    $('password-btn').textContent = on ? 'Signing in…' : 'Sign in';
  }

  async function withPassword(ev) {
    ev.preventDefault();
    error(null);
    busy(true);
    try {
      const res = await W.post('/api/signin/password', {
        name: $('signin-name').value.trim(),
        password: $('signin-password').value,
      });
      location.replace(res.next === 'setup' ? '/setup' : '/');
    } catch (e) {
      busy(false);
      $('signin-password').value = '';
      $('signin-password').focus();
      error(String(e.message || e));
    }
  }

  async function withPasskey() {
    error(null);
    busy(true);
    const label = $('passkey-btn').querySelector('span');
    label.textContent = 'Waiting for your passkey…';
    try {
      await W.signIn();
      location.replace('/');
    } catch (e) {
      busy(false);
      label.textContent = 'Sign in with a passkey';
      error(e.code === 'no_passkeys'
        ? 'Nobody here has a passkey yet. Sign in with your password, then add one from the key button.'
        : String(e.message || e));
    }
  }

  // why a Google sign-in came back here (#70): a code from /auth/oidc/callback, never its detail
  const SSO_FAILED = {
    not_allowed: 'That Google account isn’t set up here. Ask your admin to add it to your name.',
    denied: 'Google sign-in was cancelled.',
    expired: 'That sign-in took too long or started in another browser. Try again.',
    unverified: 'Google hasn’t verified that account’s email address.',
    no_email: 'Google didn’t share an email address for that account.',
    token: 'Google’s answer couldn’t be checked. Try again, or ask your admin.',
    provider: 'Google couldn’t be reached. Try again, or ask your admin.',
  };

  async function load() {
    let st;
    try {
      const r = await fetch('/api/auth/state', { credentials: 'same-origin', cache: 'no-store' });
      st = r.ok ? await r.json() : null;
    } catch (e) {
      st = null;
    }
    if (!st || !st.hosted || !st.password) return;  // the desktop: `switchboard login` in your terminal
    // listeners before the form shows: a click or Enter is never lost
    $('password-form').addEventListener('submit', withPassword);
    $('passkey-btn').addEventListener('click', withPasskey);
    // the three ways in (a password, a passkey, Google when it's set up); a passkey once it's set up
    const passkeys = !!(!st.claim && st.passkeys_work && W.supported());
    if (st.sso === 'google' || st.sso === 'sso') {
      const b = $('sso-btn');
      b.disabled = false;
      b.removeAttribute('aria-describedby');
      $('sso-soon').classList.add('hidden');
      $('sso-label').textContent = st.sso === 'google' ? 'Sign in with Google' : 'Sign in with SSO';
      b.addEventListener('click', function () { location.assign('/auth/oidc/start'); });
    }
    const why = SSO_FAILED[new URLSearchParams(location.search).get('sso') || ''];
    if (why) error(why);
    $('passkey-btn').classList.toggle('hidden', !passkeys);
    $('login-first').classList.toggle('hidden', !st.claim);
    $('login-forgot').classList.toggle('hidden', !!st.claim);
    $('login-cli').classList.add('hidden');
    $('login-choose').classList.remove('hidden');
    if (st.claim) $('signin-name').value = 'admin';
    $(st.claim ? 'signin-password' : 'signin-name').focus();
  }

  document.addEventListener('DOMContentLoaded', load);
})();
