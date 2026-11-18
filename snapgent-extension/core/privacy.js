// ArteX-TNT privacy envelope builder.
// In LOCAL-ONLY mode the provider page receives ONLY this envelope.
// The full payload is routed over the bridge WebSocket to the A2A
// inbox of the "zed" agent. Nothing from the original text (no bytes
// count, no line count, no digest, no content) is exposed to the
// provider: metadata is encrypted with AES-256-GCM under a key that
// never leaves this browser, and the serialized envelope is padded
// to a constant length so even its size is uninformative.
(function (root) {
  "use strict";

  const SCHEMA = 1;
  const TARGET_LEN = 512; // constant serialized envelope length (chars)

  // FNV-1a 64-bit. Used only locally (never sent to the provider).
  function fnv1a64(str) {
    let h = 0xcbf29ce484222325n;
    const prime = 0x100000001b3n;
    const mask = 0xffffffffffffffffn;
    for (let i = 0; i < str.length; i++) {
      h ^= BigInt(str.charCodeAt(i));
      h = (h * prime) & mask;
    }
    return h;
  }

  function toHex(n) {
    return n.toString(16).padStart(16, "0");
  }

  function randomBytes(n) {
    const out = new Uint8Array(n);
    if (typeof crypto !== "undefined" && crypto.getRandomValues) {
      crypto.getRandomValues(out);
    } else {
      for (let i = 0; i < n; i++) out[i] = Math.floor(Math.random() * 256);
    }
    return out;
  }

  function randomHex(n) {
    const b = randomBytes(n);
    let s = "";
    for (let i = 0; i < b.length; i++) s += b[i].toString(16).padStart(2, "0");
    return s;
  }

  function randomPad(len) {
    const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
    let s = "";
    for (let i = 0; i < len; i++) s += chars[Math.floor(Math.random() * chars.length)];
    return s;
  }

  function b64(buf) {
    let bin = "";
    const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return (typeof btoa === "function" ? btoa(bin) : Buffer.from(bin, "binary").toString("base64"));
  }

  function fromHex(hex) {
    const out = new Uint8Array(hex.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    return out;
  }

  // Synchronous envelope: no plaintext metadata, opaque random token,
  // padded to a constant serialized length. Safe fallback when crypto
  // is unavailable.
  function buildEnvelope(input) {
    input = input || {};
    const tool = input.tool || "unknown";
    const ok = input.ok !== false;
    const base = {
      _privacy: "local_only",
      schema: SCHEMA,
      tool: tool,
      ok: ok,
      token: randomHex(32), // 64 hex chars, opaque, no metadata
      images: null,
      raw: null,
      routed_to: "bridge.a2a.local_zed",
      note: "Full payload routed to local A2A inbox 'zed'. Provider sees only this padded, encrypted envelope.",
    };
    // Pad to constant serialized length.
    let json = JSON.stringify(base);
    if (json.length < TARGET_LEN) {
      base.pad = randomPad(TARGET_LEN - json.length - 12);
      json = JSON.stringify(base);
    }
    return base;
  }

  // Asynchronous envelope: encrypts the real metadata (bytes, lines,
  // digest) under AES-256-GCM so the provider cannot read or infer
  // anything about the payload. The ciphertext is base64 and fixed
  // length; plaintext is padded to 128 bytes before encryption.
  async function encryptMeta(meta, keyHex) {
    const subtle = (typeof crypto !== "undefined" && crypto.subtle)
      ? crypto.subtle
      : (typeof globalThis !== "undefined" && globalThis.crypto && globalThis.crypto.subtle
        ? globalThis.crypto.subtle
        : null);
    if (!subtle || !keyHex) return null;
    try {
      const keyBytes = fromHex(keyHex);
      const key = await subtle.importKey("raw", keyBytes, { name: "AES-GCM" }, false, ["encrypt"]);
      const iv = randomBytes(12);
      const plaintext = new TextEncoder().encode(JSON.stringify(meta));
      // Pad plaintext to 128 bytes.
      const padded = new Uint8Array(128);
      padded.set(plaintext);
      // Fill remainder with random bytes.
      const rem = randomBytes(128 - plaintext.length);
      padded.set(rem, plaintext.length);
      const ct = await subtle.encrypt({ name: "AES-GCM", iv: iv }, key, padded);
      const out = new Uint8Array(iv.length + ct.byteLength);
      out.set(iv, 0);
      out.set(new Uint8Array(ct), iv.length);
      return b64(out);
    } catch (e) {
      return null;
    }
  }

  async function buildEnvelopeAsync(input, keyHex) {
    input = input || {};
    const tool = input.tool || "unknown";
    const ok = input.ok !== false;
    const text = input.text || "";
    const meta = {
      tool: tool,
      ok: ok,
      bytes: text.length,
      lines: text === "" ? 0 : text.split(/\n/).length,
      digest: toHex(fnv1a64(text)),
    };
    const token = await encryptMeta(meta, keyHex);
    const base = {
      _privacy: "local_only",
      schema: SCHEMA,
      tool: tool,
      ok: ok,
      token: token || randomHex(32),
      images: null,
      raw: null,
      routed_to: "bridge.a2a.local_zed",
      note: "Full payload routed to local A2A inbox 'zed'. Provider sees only this padded, encrypted envelope.",
    };
    let json = JSON.stringify(base);
    if (json.length < TARGET_LEN) {
      base.pad = randomPad(TARGET_LEN - json.length - 12);
      json = JSON.stringify(base);
    }
    return base;
  }

  const api = {
    fnv1a64: fnv1a64,
    toHex: toHex,
    buildEnvelope: buildEnvelope,
    envelopeFor: buildEnvelope,
    buildEnvelopeAsync: buildEnvelopeAsync,
    encryptMeta: encryptMeta,
    TARGET_LEN: TARGET_LEN,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  if (typeof window !== "undefined") {
    window.SnapgentPrivacy = api;
  }
  if (typeof globalThis !== "undefined") {
    globalThis.SnapgentPrivacy = api;
  }
})(this);
