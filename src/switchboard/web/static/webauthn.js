// switchboard: passkeys in the browser (DESIGN.md §31.3, §31.4), shared by the claim page
// (setup.js), the sign-in page (login.js) and the app (app.js).
//
// window.SBWebAuthn does the three ceremonies against the broker's routes: claim (a claim token
// and a new passkey), signIn (an assertion: a new session, or a fresh passkey check for a
// signed-in browser) and addPasskey (a registration for a session that passed a check). It
// turns the broker's JSON options into what navigator.credentials takes and the credential
// back into JSON (base64url both ways), and posts with the same-origin rules app.js uses
// (credentials: 'same-origin', X-Switchboard: 1). No HTML, no links, no storage: the lint in
// tests/unit/test_web_static_lint.py applies to this file too.
(function () {
  'use strict';

  function b64uToBuf(s) {
    let t = String(s).replace(/-/g, '+').replace(/_/g, '/');
    while (t.length % 4) t += '=';
    const bin = atob(t);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out.buffer;
  }

  function bufToB64u(buf) {
    const bytes = new Uint8Array(buf);
    let bin = '';
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  function withIds(list) {
    return (list || []).map(function (c) { return Object.assign({}, c, { id: b64uToBuf(c.id) }); });
  }

  // the broker's creation options (fido2's JSON) -> CredentialCreationOptions
  function creationOptions(json) {
    const pk = Object.assign({}, json.publicKey);
    pk.challenge = b64uToBuf(pk.challenge);
    pk.user = Object.assign({}, pk.user, { id: b64uToBuf(pk.user.id) });
    if (pk.excludeCredentials) pk.excludeCredentials = withIds(pk.excludeCredentials);
    return { publicKey: pk };
  }

  function requestOptions(json) {
    const pk = Object.assign({}, json.publicKey);
    pk.challenge = b64uToBuf(pk.challenge);
    if (pk.allowCredentials) pk.allowCredentials = withIds(pk.allowCredentials);
    return { publicKey: pk };
  }

  // a PublicKeyCredential -> the JSON the broker verifies (what toJSON() gives, built by hand
  // so a browser without toJSON works too)
  function credentialJSON(cred) {
    const r = cred.response;
    const out = {
      id: cred.id,
      rawId: bufToB64u(cred.rawId),
      type: cred.type,
      clientExtensionResults: typeof cred.getClientExtensionResults === 'function' ? cred.getClientExtensionResults() : {},
      response: { clientDataJSON: bufToB64u(r.clientDataJSON) },
    };
    if (r.attestationObject) {
      out.response.attestationObject = bufToB64u(r.attestationObject);
      if (typeof r.getTransports === 'function') out.response.transports = r.getTransports();
    } else {
      out.response.authenticatorData = bufToB64u(r.authenticatorData);
      out.response.signature = bufToB64u(r.signature);
      if (r.userHandle) out.response.userHandle = bufToB64u(r.userHandle);
    }
    if (cred.authenticatorAttachment) out.authenticatorAttachment = cred.authenticatorAttachment;
    return out;
  }

  async function post(path, body) {
    const r = await fetch(path, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Switchboard': '1' },
      body: JSON.stringify(body || {}),
    });
    let data;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) {
      const err = new Error((data && data.message) || r.statusText || ('HTTP ' + r.status));
      err.status = r.status;
      err.code = data && data.error;
      throw err;
    }
    return data;
  }

  function supported() {
    return typeof window.PublicKeyCredential === 'function' && !!(navigator.credentials && navigator.credentials.create);
  }

  // the authenticator said no (the user cancelled, or a passkey exists already): plain words
  function explain(e) {
    if (e && e.name === 'NotAllowedError') return 'cancelled, or the browser did not allow it: try again';
    if (e && e.name === 'InvalidStateError') return 'this authenticator already holds a passkey for this switchboard';
    if (e && e.name === 'SecurityError') return 'the browser refused: passkeys need https (or localhost)';
    return String((e && e.message) || e);
  }

  async function create(json) {
    let cred;
    try {
      cred = await navigator.credentials.create(creationOptions(json));
    } catch (e) {
      // The raw WebAuthn error may contain browser details; only expose the safe explanation.
      // eslint-disable-next-line preserve-caught-error
      throw new Error(explain(e));
    }
    if (!cred) throw new Error('no passkey was created');
    return credentialJSON(cred);
  }

  async function get(json) {
    let cred;
    try {
      cred = await navigator.credentials.get(requestOptions(json));
    } catch (e) {
      // The raw WebAuthn error may contain browser details; only expose the safe explanation.
      // eslint-disable-next-line preserve-caught-error
      throw new Error(explain(e));
    }
    if (!cred) throw new Error('no passkey was used');
    return credentialJSON(cred);
  }

  // Claim an unclaimed broker: the token from the claim link, and a new passkey named `name`.
  async function claim(token, name) {
    const begun = await post('/api/setup/begin', { token: token });
    const credential = await create(begun.options);
    return post('/api/setup/finish', { credential: credential, name: name });
  }

  // Sign in with a passkey. From a signed-in page, the same ceremony is a fresh passkey check
  // for that session (the broker answers reauth: true) rather than a new session.
  async function signIn() {
    const begun = await post('/api/passkey/begin', {});
    const credential = await get(begun.options);
    return post('/api/passkey/finish', { credential: credential });
  }

  // Add a passkey to the signed-in session. A session without a passkey check in the last
  // five minutes is refused (403 reauth): then one check first, and one retry.
  async function addPasskey(name) {
    let begun;
    try {
      begun = await post('/api/passkeys/begin', {});
    } catch (e) {
      if (e.code !== 'reauth') throw e;
      await signIn();
      begun = await post('/api/passkeys/begin', {});
    }
    const credential = await create(begun.options);
    return post('/api/passkeys', { credential: credential, name: name });
  }

  window.SBWebAuthn = {
    supported: supported, post: post, claim: claim, signIn: signIn, addPasskey: addPasskey,
    creationOptions: creationOptions, requestOptions: requestOptions, credentialJSON: credentialJSON,
    b64uToBuf: b64uToBuf, bufToB64u: bufToB64u,
  };
})();
