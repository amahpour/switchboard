// switchboard: the consent page for an agent that signs in through the browser (oauth.html;
// DESIGN.md §49.9, #386).
//
// The agent sent this browser to /oauth/authorize with its request in the address bar. This page
// sends that same query to the broker (GET /api/oauth/consent, which checks it again and says who
// is signed in), and shows one step: why the request can't continue; a way to sign in first (the
// broker brings a signed-out browser back here, and this page also notices a sign-in made in
// another tab); or the consent card. Approve and Deny are POSTs that carry the same query, so what
// the person saw is what the broker re-checks. The answer is the agent's own loopback address, with
// a one-time code or an access_denied, and the page opens it: that is how the agent hears. A hosted
// broker wants a password or passkey check in the last five minutes first (window.SBWebAuthn, as
// the app's Confirm it's you). No HTML is built from text: every string goes in through textContent.
(function () {
  'use strict';

  const W = window.SBWebAuthn;
  const QUERY = location.search.replace(/^\?/, '');
  // the only place the broker ever sends the browser on: the agent's own machine
  const LOOPBACK = /^http:\/\/(localhost|127\.0\.0\.1|\[::1\]):[0-9]{1,5}\/[^\s]*$/;
  const POLL_MS = 3000;

  let needsCheck = false;  // a hosted broker still wants a password or passkey check
  let pollTimer = null;

  function $(id) { return document.getElementById(id); }

  function show(step) {
    ['step-error', 'step-signin', 'step-consent'].forEach(function (id) {
      $(id).classList.toggle('hidden', id !== step);
    });
    $('oauth-loading').classList.add('hidden');
  }

  function title(text) { $('oauth-title').textContent = text; }

  function consentError(text) {
    const e = $('consent-error');
    e.textContent = text || '';
    e.classList.toggle('hidden', !text);
  }

  function busy(on) {
    $('consent-approve').disabled = on;
    $('consent-deny').disabled = on;
    $('consent-passkey').disabled = on;
    $('consent-approve').textContent = on ? 'Approving…' : 'Approve';
  }

  async function getJSON(path) {
    const r = await fetch(path, { credentials: 'same-origin', cache: 'no-store' });
    let data;
    try { data = await r.json(); } catch (e) { data = null; }
    return { status: r.status, data: data };
  }

  function goBack(url) {
    // belt and braces: the broker only builds this from a redirect the agent registered
    if (typeof url !== 'string' || !LOOPBACK.test(url)) throw new Error('the broker sent an address this page won’t open');
    title('Back to the agent');
    show('step-error');
    $('step-error').querySelector('.fine').textContent = 'You can close this tab.';
    $('error-text').textContent = 'The agent has its answer.';
    location.assign(url);
  }

  function showConsent(v) {
    needsCheck = !!(v.hosted && !v.fresh);
    title('Sign in an agent');
    $('consent-lead').textContent = 'An agent is asking to connect to this switchboard.';
    $('consent-person').textContent = v.email ? v.person + ' (' + v.email + ')' : v.person;
    $('consent-client').textContent = v.client_name;
    $('consent-redirect').textContent = 'a program on this computer, at ' + v.redirect_to;
    const confirm = $('consent-confirm');
    confirm.classList.toggle('hidden', !needsCheck);
    $('consent-user').value = v.email || v.person;
    // a person with no password uses their passkey; one with no passkey (or a browser without them) a password
    const passkeys = !!(v.passkeys && W && W.supported());
    $('consent-passkey').classList.toggle('hidden', !passkeys);
    const usePassword = !!v.password || !passkeys;
    $('consent-password').classList.toggle('hidden', !usePassword);
    confirm.querySelector('label').classList.toggle('hidden', !usePassword);
    show('step-consent');
    $('consent-device').focus();
  }

  function showSignIn() {
    title('Sign in to approve');
    show('step-signin');
    // a hosted broker has its own sign-in page, which brings the browser back here; a desktop one
    // has `switchboard login` in a terminal (the page can't tell them apart without asking)
    getJSON('/api/auth/state').then(function (res) {
      const hosted = !!(res.data && res.data.hosted && res.data.password);
      $('signin-hosted').classList.toggle('hidden', !hosted);
      $('signin-desktop').classList.toggle('hidden', hosted);
    });
    schedule();
  }

  function schedule() {
    if (pollTimer !== null) return;
    pollTimer = setTimeout(function () {
      pollTimer = null;
      if (document.hidden) { schedule(); return; }
      load();
    }, POLL_MS);
  }

  async function load() {
    let res;
    try {
      res = await getJSON('/api/oauth/consent?' + QUERY);
    } catch (e) {
      title('Can’t reach switchboard');
      $('error-text').textContent = 'This page couldn’t reach switchboard. Is it running?';
      show('step-error');
      return;
    }
    if (res.status === 200) { showConsent(res.data); return; }
    if (res.status === 401) { showSignIn(); return; }
    title('This sign-in can’t continue');
    $('error-text').textContent = (res.data && res.data.message) || 'The request isn’t one switchboard can answer.';
    show('step-error');
  }

  async function approve(ev) {
    ev.preventDefault();
    consentError(null);
    const device = $('consent-device').value.trim();
    if (!device) { consentError('Name this device, for example work-laptop.'); $('consent-device').focus(); return; }
    busy(true);
    try {
      if (needsCheck) {
        const typed = $('consent-password').value;
        if (!typed) throw new Error('Type your password to confirm it’s you, or use a passkey.');
        await W.post('/api/signin/check', { password: typed });
        needsCheck = false;
      }
      goBack((await W.post('/api/oauth/approve', { query: QUERY, device: device })).redirect);
    } catch (e) {
      busy(false);
      if (e.code === 'reauth') {  // the five minutes ran out in between
        needsCheck = true;
        $('consent-confirm').classList.remove('hidden');
        $('consent-password').value = '';
        $('consent-password').focus();
        consentError('Confirm it’s you, then approve again.');
        return;
      }
      if (e.code === 'bad_password') $('consent-password').value = '';
      consentError(String(e.message || e));
    }
  }

  async function withPasskey() {
    consentError(null);
    busy(true);
    try {
      await W.signIn();
      needsCheck = false;
      $('consent-confirm').classList.add('hidden');
      busy(false);
      $('consent-form').requestSubmit();
    } catch (e) {
      busy(false);
      consentError(String(e.message || e));
    }
  }

  async function deny() {
    consentError(null);
    busy(true);
    try {
      goBack((await W.post('/api/oauth/deny', { query: QUERY })).redirect);
    } catch (e) {
      busy(false);
      consentError(String(e.message || e));
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('consent-form').addEventListener('submit', approve);
    $('consent-deny').addEventListener('click', deny);
    $('consent-passkey').addEventListener('click', withPasskey);
    load();
  });
})();
