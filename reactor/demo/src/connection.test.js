import { test } from "node:test";
import assert from "node:assert/strict";
import {
  ConnectionError,
  checkLocalUrl,
  describeError,
  loadKey,
  loadSettings,
  mintToken,
  saveKey,
  tokenResolver,
} from "./connection.js";

const memory = () => {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), m };
};
const reply = (status, body) => async () => ({ status, ok: status < 300, statusText: "", text: async () => JSON.stringify(body) });

test("key lives in sessionStorage unless remembered, and only in one place", () => {
  const session = memory(), local = memory();
  saveKey("dev", "rk_a", false, session, local);
  assert.deepEqual(loadKey("dev", session, local), { key: "rk_a", remember: false });
  assert.equal(local.m.size, 0);
  saveKey("dev", "rk_a", true, session, local);
  assert.deepEqual(loadKey("dev", session, local), { key: "rk_a", remember: true });
  assert.equal(session.m.size, 0);
  assert.deepEqual(loadKey("prod", session, local), { key: "", remember: false });
});

test("storage that throws does not break the page", () => {
  const broken = { getItem() { throw Error("blocked"); }, setItem() { throw Error("blocked"); }, removeItem() { throw Error("blocked"); } };
  saveKey("dev", "rk_a", true, broken, broken);
  assert.deepEqual(loadKey("dev", broken, broken), { key: "", remember: false });
  assert.deepEqual(loadSettings({ target: "dev", localUrl: "http://x:8080" }, broken), { target: "dev", localUrl: "http://x:8080", model: "eden-live" });
});

test("mintToken sends the key only as a header to the chosen endpoint, scoped to the model", async () => {
  let seen;
  const fetchImpl = async (url, init) => {
    seen = { url, init };
    return reply(200, { jwt: "j", expires_at: 2_000_000_000 })();
  };
  const t = await mintToken({ target: "dev", apiKey: " rk_x ", model: "eden-live", fetchImpl });
  assert.equal(seen.url, "https://api.rea.live/tokens");
  assert.equal(seen.init.headers["Reactor-API-Key"], "rk_x");
  assert.ok(!seen.url.includes("rk_x") && !seen.init.body.includes("rk_x"));
  assert.deepEqual(JSON.parse(seen.init.body).authorization_details[0].resources, { models: { match: ["eden-live"] } });
  assert.deepEqual(t, { jwt: "j", expiresAt: 2_000_000_000_000 });
});

test("mintToken explains a bad key, an unavailable model and a missing key", async () => {
  await assert.rejects(mintToken({ target: "dev", apiKey: "rk_bad", model: "eden-live", fetchImpl: reply(401, { error: "Invalid API key" }) }), (e) => e instanceof ConnectionError && /DEV rejected this API key/.test(e.message) && !e.message.includes("rk_bad"));
  await assert.rejects(mintToken({ target: "prod", apiKey: "rk_x", model: "nope", fetchImpl: reply(403, { error: "requested model is not available to this API key" }) }), /nope is not available to this API key on PROD/);
  await assert.rejects(mintToken({ target: "dev", apiKey: "  ", model: "eden-live", fetchImpl: assert.fail }), /Enter your DEV API key/);
  await assert.rejects(mintToken({ target: "dev", apiKey: "rk_x", model: "eden-live", fetchImpl: async () => { throw TypeError("Failed to fetch"); } }), /Could not reach DEV/);
});

test("the resolver refreshes near expiry with a token bound to the running session", async () => {
  let t = 0;
  const calls = [];
  const mint = async (args) => (calls.push(args), { jwt: "second", expiresAt: t + 3_600_000 });
  const jwt = tokenResolver({ jwt: "first", expiresAt: 600_000 }, { target: "dev", apiKey: "k", model: "eden-live", sessionId: () => "s1", mint, now: () => t });
  assert.equal(await jwt(), "first");
  t = 590_000;
  assert.equal(await jwt(), "second");
  assert.equal(calls[0].bindSessionId, "s1");
});

test("SDK errors become sentences that name the platform", () => {
  const e = (code, message) => Object.assign(Error(message), { code });
  assert.match(describeError(e("RATE_LIMITED", "no available capacity: no available servers"), { target: "dev", model: "eden-live" }), /^No capacity: DEV has no free eden-live instance/);
  assert.equal(
    describeError(e("RATE_LIMITED", 'unexpected HTTP status 429 from create session: {"error":"no available capacity: x"}\n'), { target: "dev", model: "eden-live" }),
    "No capacity: DEV has no free eden-live instance right now. It may not be deployed on DEV, or every instance is busy; try again later. (no available capacity: x)",
  );
  assert.match(describeError(e("NOT_FOUND", "x"), { target: "prod", model: "eden-live" }), /not found on PROD/);
  assert.equal(describeError(undefined, { target: "dev" }), "");
});

test("checkLocalUrl", () => {
  assert.equal(checkLocalUrl(" http://h:8080/ "), "http://h:8080");
  assert.throws(() => checkLocalUrl("h:8080x y"), ConnectionError);
  assert.throws(() => checkLocalUrl("ftp://h"), /http/);
  assert.throws(() => checkLocalUrl("http://box.ts.net:8080", "https:"), /mixed|plain-http/);
  assert.equal(checkLocalUrl("http://localhost:8080", "https:"), "http://localhost:8080");
  assert.equal(checkLocalUrl("http://box.ts.net:8080", "http:"), "http://box.ts.net:8080");
});
