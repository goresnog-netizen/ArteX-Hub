"use strict";
const assert = require("assert");
const P = require("./core/privacy.js");

let passed = 0, failed = 0;
function ok(name) { passed++; console.log("  PASS:", name); }
function fail(name, e) { failed++; console.error("  FAIL:", name, "->", e && e.message ? e.message : e); }

async function run() {
  console.log("privacy.js tests");

  // 1
  try {
    assert.strictEqual(typeof P.buildEnvelope, "function");
    assert.strictEqual(typeof P.envelopeFor, "function");
    assert.strictEqual(typeof P.buildEnvelopeAsync, "function");
    assert.strictEqual(typeof P.encryptMeta, "function");
    assert.strictEqual(typeof P.fnv1a64, "function");
    ok("module exports");
  } catch (e) { fail("module exports", e); }

  // 2
  try {
    assert.strictEqual(P.toHex(P.fnv1a64("")), "cbf29ce484222325");
    ok("fnv1a64 empty vector");
  } catch (e) { fail("fnv1a64 empty vector", e); }

  // 3
  try {
    const e = P.buildEnvelope({ tool: "bash", ok: true, text: "SECRET PAYLOAD XYZ", images: null });
    assert.ok(e && typeof e === "object");
    assert.strictEqual(e._privacy, "local_only");
    assert.strictEqual(e.images, null);
    assert.strictEqual(e.raw, null);
    assert.ok(!("bytes" in e));
    assert.ok(!("lines" in e));
    assert.ok(!("digest" in e));
    assert.strictEqual(typeof e.token, "string");
    assert.ok(e.token.length >= 32);
    ok("no plaintext metadata fields");
  } catch (e) { fail("no plaintext metadata fields", e); }

  // 4
  try {
    const a = P.buildEnvelope({ tool: "t", ok: true, text: "x" });
    const b = P.buildEnvelope({ tool: "t", ok: true, text: "x" });
    assert.notStrictEqual(a.token, b.token);
    ok("token varies between calls");
  } catch (e) { fail("token varies between calls", e); }

  // 5
  try {
    const e1 = P.buildEnvelope({ tool: "bash", ok: true, text: "short" });
    const e2 = P.buildEnvelope({ tool: "bash", ok: true, text: "a".repeat(5000) });
    const l1 = JSON.stringify(e1).length;
    const l2 = JSON.stringify(e2).length;
    assert.ok(l1 >= P.TARGET_LEN - 50, "too short: " + l1);
    assert.ok(l2 >= P.TARGET_LEN - 50, "too short: " + l2);
    ok("padded constant-ish length");
  } catch (e) { fail("padded constant-ish length", e); }

  // 6
  try {
    const secret = "SUPER_SECRET_PAYLOAD_12345";
    const path = "C:\\secret\\path\\file.txt";
    const e = P.buildEnvelope({ tool: "read", ok: true, text: secret + "\n" + path, images: null });
    const json = JSON.stringify(e);
    assert.ok(!json.includes(secret));
    assert.ok(!json.includes("secret"));
    assert.ok(!json.includes("file.txt"));
    ok("raw payload / path segments not leaked");
  } catch (e) { fail("raw payload / path segments not leaked", e); }

  // 7
  try {
    const e = P.buildEnvelope({ tool: "t", ok: true, text: "x", images: [{ b64: "AAAA" }] });
    assert.strictEqual(e.images, null);
    assert.ok(!JSON.stringify(e).includes("AAAA"));
    ok("images not leaked");
  } catch (e) { fail("images not leaked", e); }

  // 8
  try {
    const e = P.envelopeFor({ tool: "x", ok: false, text: "y" });
    assert.strictEqual(e.ok, false);
    assert.strictEqual(e._privacy, "local_only");
    ok("envelopeFor alias");
  } catch (e) { fail("envelopeFor alias", e); }

  // 9
  try {
    const r = await P.encryptMeta({ bytes: 5 }, null);
    assert.strictEqual(r, null);
    ok("encryptMeta null without key");
  } catch (e) { fail("encryptMeta null without key", e); }

  // 10
  try {
    const key = "0".repeat(64);
    const r = await P.encryptMeta({ bytes: 5, digest: "abc" }, key);
    assert.ok(typeof r === "string" && r.length > 0);
    const buf = Buffer.from(r, "base64");
    assert.ok(buf.length >= 12 + 16);
    ok("encryptMeta base64 ciphertext");
  } catch (e) { fail("encryptMeta base64 ciphertext", e); }

  // 11
  try {
    const key = "1".repeat(64);
    const e = await P.buildEnvelopeAsync({ tool: "bash", ok: true, text: "hello world" }, key);
    assert.ok(e && typeof e === "object");
    assert.ok(!("bytes" in e) && !("lines" in e) && !("digest" in e));
    assert.strictEqual(e.images, null);
    assert.strictEqual(e.raw, null);
    assert.ok(typeof e.token === "string" && e.token.length > 32);
    assert.ok(!JSON.stringify(e).includes("hello world"));
    ok("buildEnvelopeAsync encrypted");
  } catch (e) { fail("buildEnvelopeAsync encrypted", e); }

  // 12
  try {
    const e = await P.buildEnvelopeAsync({ tool: "t", ok: true, text: "x" }, null);
    assert.ok(typeof e.token === "string" && e.token.length >= 32);
    assert.ok(!("bytes" in e));
    ok("buildEnvelopeAsync fallback without key");
  } catch (e) { fail("buildEnvelopeAsync fallback without key", e); }

  console.log("passed:", passed, "failed:", failed);
  if (failed > 0) process.exitCode = 1;
}

run().catch((e) => { console.error(e); process.exitCode = 1; });
