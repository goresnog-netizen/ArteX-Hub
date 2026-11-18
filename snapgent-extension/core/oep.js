/**
 * Opaque Envelope Protocol (OEP) - extension side.
 *
 * Mirrors oep.py on the bridge. Uses WebCrypto (X25519, HKDF-SHA256,
 * AES-256-GCM). Chrome 121+ and Edge 121+ expose X25519 via WebCrypto.
 * Older browsers fall back to TweetNaCl-style X25519 via SubtleCrypto's
 * ECDH with curve25519 - which Chrome does NOT expose; in that case OEP
 * is disabled and the extension stays in Reasoned mode.
 *
 * Two providers see this output:
 *   - the local bridge (it decrypts)
 *   - nobody else (the LLM sees [ENV:v1:...] ciphertext)
 */
(function (root) {
  "use strict";

  const ENV_RE = /\[ENV:v1:([A-Za-z0-9+/=]+)\]/;
  const RES_RE = /\[RES:v1:([A-Za-z0-9+/=]+)\]/;
  const NONCE_BYTES = 12;
  const INFO = new TextEncoder().encode("snapgent-oep-v1");

  const enc = new TextEncoder();
  const dec = new TextDecoder();

  function b64encode(bytes) {
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin);
  }

  function b64decode(text) {
    const bin = atob(text);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }

  class OEP {
    constructor() {
      this._priv = null;
      this._key = null;
    }

    static async supported() {
      if (!globalThis.crypto || !globalThis.crypto.subtle) return false;
      try {
        await globalThis.crypto.subtle.generateKey({ name: "X25519" }, false, [
          "deriveBits",
        ]);
        return true;
      } catch (_) {
        return false;
      }
    }

    async _ensureKeyPair() {
      if (this._priv) return this._priv;
      this._priv = await globalThis.crypto.subtle.generateKey(
        { name: "X25519" },
        false,
        ["deriveBits"]
      );
      return this._priv;
    }

    async publicKeyB64() {
      const pair = await this._ensureKeyPair();
      const raw = await globalThis.crypto.subtle.exportKey("raw", pair.publicKey);
      return b64encode(new Uint8Array(raw));
    }

    async establish(peerPubB64, sessionIdB64) {
      const pair = await this._ensureKeyPair();
      const peerRaw = b64decode(peerPubB64);
      const peerKey = await globalThis.crypto.subtle.importKey(
        "raw",
        peerRaw,
        { name: "X25519" },
        false,
        []
      );
      const sharedBits = await globalThis.crypto.subtle.deriveBits(
        { name: "X25519", public: peerKey },
        pair.privateKey,
        256
      );
      const baseKey = await globalThis.crypto.subtle.importKey(
        "raw",
        sharedBits,
        { name: "HKDF" },
        false,
        ["deriveKey"]
      );
      const salt = b64decode(sessionIdB64);
      this._key = await globalThis.crypto.subtle.deriveKey(
        { name: "HKDF", hash: "SHA-256", salt, info: INFO },
        baseKey,
        { name: "AES-GCM", length: 256 },
        false,
        ["encrypt", "decrypt"]
      );
    }

    _requireKey() {
      if (!this._key) throw new Error("OEP session not established");
      return this._key;
    }

    async _wrap(prefix, payload) {
      const nonce = globalThis.crypto.getRandomValues(new Uint8Array(NONCE_BYTES));
      const pt = enc.encode(JSON.stringify(payload));
      const ct = new Uint8Array(
        await globalThis.crypto.subtle.encrypt(
          { name: "AES-GCM", iv: nonce },
          this._requireKey(),
          pt
        )
      );
      const blob = new Uint8Array(nonce.length + ct.length);
      blob.set(nonce, 0);
      blob.set(ct, nonce.length);
      return `[${prefix}:v1:${b64encode(blob)}]`;
    }

    async _unwrap(text, regex) {
      const m = regex.exec(text || "");
      if (!m) return null;
      const blob = b64decode(m[1]);
      if (blob.length < NONCE_BYTES + 16) throw new Error("envelope too short");
      const nonce = blob.slice(0, NONCE_BYTES);
      const ct = blob.slice(NONCE_BYTES);
      const pt = await globalThis.crypto.subtle.decrypt(
        { name: "AES-GCM", iv: nonce },
        this._requireKey(),
        ct
      );
      return JSON.parse(dec.decode(pt));
    }

    wrapEnvelope(payload) {
      return this._wrap("ENV", payload);
    }

    unwrapEnvelope(text) {
      return this._unwrap(text, ENV_RE);
    }

    wrapResult(payload) {
      return this._wrap("RES", payload);
    }

    unwrapResult(text) {
      return this._unwrap(text, RES_RE);
    }
  }

  root.SnapgentOEP = { OEP, ENV_RE, RES_RE };
})(typeof self !== "undefined" ? self : globalThis);
