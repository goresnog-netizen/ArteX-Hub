// Node test for snapgent-extension/core/oep.js
// Verifies the extension-side OEP round-trips against a synthetic peer.

import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import vm from "node:vm";

// Node 24 has globalThis.crypto built in (with X25519). Only polyfill
// btoa/atob, which a browser service worker exposes but Node does not.
if (typeof globalThis.btoa !== "function") {
  globalThis.btoa = (s) => Buffer.from(s, "binary").toString("base64");
  globalThis.atob = (s) => Buffer.from(s, "base64").toString("binary");
}

const src = readFileSync("snapgent-extension/core/oep.js", "utf8");
const ctx = {
  TextEncoder,
  TextDecoder,
  console,
  crypto: globalThis.crypto,
  btoa: globalThis.btoa,
  atob: globalThis.atob,
};
ctx.globalThis = ctx;
// The oep.js IIFE uses `self` as its root; point it at this vm context so
// SnapgentOEP lands on ctx rather than Node's outer global.
ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(src, ctx, { filename: "oep.js" });

const { OEP, ENV_RE, RES_RE } = ctx.SnapgentOEP;

async function b64(bytes) {
  return Buffer.from(bytes).toString("base64");
}

async function main() {
  const supported = await OEP.supported();
  if (!supported) {
    console.log("X25519 not supported in this node build - SKIP");
    process.exit(0);
  }
  console.log("X25519 supported");

  const alice = new OEP();
  const bob = new OEP();
  const session = Buffer.from("snapgent-js-test-session");

  // Exchange public keys and derive the shared key.
  const alicePub = await alice.publicKeyB64();
  const bobPub = await bob.publicKeyB64();
  const sessionB64 = session.toString("base64");
  await alice.establish(bobPub, sessionB64);
  await bob.establish(alicePub, sessionB64);
  console.log("shared key established on both sides");

  // Encrypt a tool call, ensure the wire form carries no plaintext.
  const payload = {
    name: "read_file",
    arguments: { path: "snapgent-extension/manifest.json" },
    timeout_ms: 5000,
  };
  const env = await alice.wrapEnvelope(payload);
  for (const needle of ["read_file", "manifest.json", "arguments"]) {
    if (env.includes(needle)) throw new Error("plaintext leaked: " + needle);
  }
  console.log("envelope length:", env.length);

  // Peer (bob) decrypts.
  const got = await bob.unwrapEnvelope(env);
  if (JSON.stringify(got) !== JSON.stringify(payload)) {
    throw new Error("round-trip mismatch: " + JSON.stringify(got));
  }
  console.log("envelope round-trip OK");

  // Result path.
  const res = { ok: true, text: "secret body", duration_ms: 12 };
  const renv = await bob.wrapResult(res);
  for (const needle of ["secret body", "duration_ms"]) {
    if (renv.includes(needle)) throw new Error("result leaked: " + needle);
  }
  const rgot = await alice.unwrapResult(renv);
  if (rgot.ok !== true || rgot.text !== "secret body") {
    throw new Error("result mismatch: " + JSON.stringify(rgot));
  }
  console.log("result round-trip OK");

  // Tamper must fail.
  let tampered = env.slice(0, -3) + (env.endsWith("AA]") ? "BB]" : "AA]");
  let tamperedOk = false;
  try {
    await bob.unwrapEnvelope(tampered);
    tamperedOk = true;
  } catch (e) {
    console.log("tamper rejected:", e.name);
  }
  if (tamperedOk) throw new Error("tampered envelope accepted");

  // Wrong key must fail.
  const eve = new OEP();
  await eve.establish(alicePub, sessionB64);
  let wrongOk = false;
  try {
    await eve.unwrapEnvelope(env);
    wrongOk = true;
  } catch (e) {
    console.log("wrong-key rejected:", e.name);
  }
  if (wrongOk) throw new Error("wrong-key envelope accepted");

  console.log("PASS");
}

main().catch((e) => {
  console.error("FAIL:", e);
  process.exit(1);
});
