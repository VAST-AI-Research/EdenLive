// Where the page connects, and how it authenticates there.
//
// DEV and PROD: the visitor types their own Reactor API key. The browser exchanges it for a
// short-lived JWT at <endpoint>/tokens (header `Reactor-API-Key`), scoped with
// `authorization_details` to this one model, the same request the SDK's README and the
// public demos' token route make server-side. The key goes only to that endpoint; the JWT is
// what the SDK uses for the session. Local: the SDK talks to a model server directly, no auth.
//
// The key is kept in sessionStorage (gone when the tab closes), or in localStorage when the
// visitor ticks "remember on this device". Nothing here logs it or puts it in a URL.

export const TARGETS = {
  dev: { label: "DEV", url: "https://api.rea.live" },
  prod: { label: "PROD", url: "https://api.reactor.inc" },
  local: { label: "Local" },
};
export const DEFAULT_MODEL = "eden-live";

const SETTINGS = "edenlive.connection";
const keySlot = (target) => `edenlive.apiKey.${target}`;

// Storage can be missing or throw (private windows, blocked site data); the page must still work.
function read(store, name) {
  try {
    return store?.getItem(name) ?? null;
  } catch {
    return null;
  }
}
function write(store, name, value) {
  try {
    if (value == null) store?.removeItem(name);
    else store?.setItem(name, value);
  } catch {
    // not persisted; the value still lives in the page's state
  }
}

/** Non-secret choices (target, local URL, model), remembered per browser. */
export function loadSettings(defaults, local = globalThis.localStorage) {
  let saved = {};
  try {
    saved = JSON.parse(read(local, SETTINGS) || "{}") || {};
  } catch {
    saved = {};
  }
  const target = TARGETS[saved.target] ? saved.target : defaults.target;
  return {
    target,
    localUrl: typeof saved.localUrl === "string" && saved.localUrl ? saved.localUrl : defaults.localUrl,
    model: typeof saved.model === "string" && saved.model ? saved.model : DEFAULT_MODEL,
  };
}
export function saveSettings({ target, localUrl, model }, local = globalThis.localStorage) {
  write(local, SETTINGS, JSON.stringify({ target, localUrl, model }));
}

/** The stored key for `target` and whether it came from "remember on this device". */
export function loadKey(target, session = globalThis.sessionStorage, local = globalThis.localStorage) {
  const remembered = read(local, keySlot(target));
  if (remembered) return { key: remembered, remember: true };
  return { key: read(session, keySlot(target)) || "", remember: false };
}
/** Keep `key` in exactly one place: localStorage if `remember`, sessionStorage otherwise. */
export function saveKey(target, key, remember, session = globalThis.sessionStorage, local = globalThis.localStorage) {
  const value = key ? key : null;
  write(remember ? local : session, keySlot(target), value);
  write(remember ? session : local, keySlot(target), null);
}

/** An error whose message is already written for the person using the page. */
export class ConnectionError extends Error {}

const normalizeUrl = (url) => String(url || "").trim().replace(/\/+$/, "");

/** Check a Local server URL; returns it normalised, or throws a ConnectionError. */
export function checkLocalUrl(url, pageProtocol = globalThis.location?.protocol) {
  const value = normalizeUrl(url);
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new ConnectionError(`"${value || "(empty)"}" is not a URL. Enter the model server's address, e.g. http://localhost:8080.`);
  }
  if (!/^https?:$/.test(parsed.protocol))
    throw new ConnectionError(`The server URL must start with http:// or https://.`);
  // An https page may not call a plain-http server (mixed content); browsers exempt only localhost.
  const loopback = /^(localhost|127\.0\.0\.1|\[::1\])$/.test(parsed.hostname);
  if (pageProtocol === "https:" && parsed.protocol === "http:" && !loopback)
    throw new ConnectionError(
      `This page is served over https, so the browser will not connect to the plain-http server ${value}. Use an https URL for the server, tunnel it to http://localhost, or open the demo from a local copy (npm run dev).`,
    );
  return value;
}

function expiryMs(expiresAt) {
  if (typeof expiresAt === "number") return expiresAt < 1e12 ? expiresAt * 1000 : expiresAt;
  const t = Date.parse(expiresAt);
  return Number.isFinite(t) ? t : Date.now() + 55 * 60 * 1000;
}

/**
 * Exchange `apiKey` for a JWT scoped to `model` at `target`'s /tokens. With `bindSessionId`,
 * the token can only operate that existing session (used to refresh a token mid-session).
 * Resolves to { jwt, expiresAt (ms) }; rejects with a ConnectionError explaining what went wrong.
 */
export async function mintToken({ target, apiKey, model, bindSessionId, maxSessions = 3, fetchImpl = globalThis.fetch }) {
  const t = TARGETS[target];
  if (!t?.url) throw new ConnectionError(`Unknown platform "${target}".`);
  const key = String(apiKey || "").trim();
  if (!key) throw new ConnectionError(`Enter your ${t.label} API key (it starts with rk_).`);
  const resources = { models: { match: [model] } };
  if (bindSessionId) resources.sessions = { bind: [bindSessionId] };
  const body = {
    authorization_details: [
      {
        type: "session",
        resources,
        ...(bindSessionId ? {} : { constraints: { max_sessions: maxSessions } }),
      },
    ],
  };
  let response;
  try {
    response = await fetchImpl(`${t.url}/tokens`, {
      method: "POST",
      cache: "no-store",
      headers: { "Reactor-API-Key": key, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ConnectionError(`Could not reach ${t.label} (${t.url}). Check your network connection and try again.`);
  }
  const text = await response.text().catch(() => "");
  let data = null;
  try {
    data = JSON.parse(text);
  } catch {
    data = null;
  }
  const detail = (data && (data.error || data.message)) || text.slice(0, 200) || response.statusText;
  if (response.status === 401)
    throw new ConnectionError(
      `${t.label} rejected this API key (${detail}). DEV and PROD use different keys; check you entered a ${t.label} key.`,
    );
  if (response.status === 403)
    throw new ConnectionError(
      `${model} is not available to this API key on ${t.label} (${detail}). The model may not be registered there, or this key's account cannot use it.`,
    );
  if (response.status === 429)
    throw new ConnectionError(`${t.label} is rate-limiting token requests (${detail}). Wait a minute and try again.`);
  if (!response.ok || !data?.jwt)
    throw new ConnectionError(`${t.label} could not issue a session token (HTTP ${response.status}: ${detail}).`);
  return { jwt: data.jwt, expiresAt: expiryMs(data.expires_at) };
}

/**
 * A JWT resolver for the SDK: hands out the token minted at Connect and, close to its expiry,
 * mints a replacement bound to the running session so a long session keeps working.
 */
export function tokenResolver(first, { target, apiKey, model, sessionId, mint = mintToken, now = Date.now }) {
  let current = first,
    pending = null;
  return async () => {
    if (current.expiresAt - now() > 120_000) return current.jwt;
    const id = sessionId();
    if (!id) return current.jwt;
    pending ??= mint({ target, apiKey, model, bindSessionId: id }).finally(() => (pending = null));
    current = await pending;
    return current.jwt;
  };
}

/** Turn an SDK error into a sentence for the page, naming the platform and model. */
export function describeError(error, { target, model }) {
  if (!error) return "";
  const where = TARGETS[target]?.label || target;
  // The platform's own reason when the SDK quotes its JSON body, else the whole message.
  const raw = String(error.message || error).trim();
  const message = raw.match(/"error"\s*:\s*"([^"]+)"/)?.[1] || raw;
  const code = error.code || "";
  if (target === "local") {
    if (code === "NETWORK_ERROR")
      return `Could not reach the model server. Check the server URL and that the server is running. (${message})`;
    return message;
  }
  if (code === "RATE_LIMITED" && /capacity/i.test(message))
    return `No capacity: ${where} has no free ${model} instance right now. It may not be deployed on ${where}, or every instance is busy; try again later. (${message})`;
  if (code === "RATE_LIMITED")
    return `${where} is rate-limiting new sessions. Wait a minute and try again. (${message})`;
  if (code === "NOT_FOUND")
    return `${model} was not found on ${where}. It may not be registered or deployed there. (${message})`;
  if (code === "UNAUTHORIZED")
    return `${where} refused the session token. Disconnect and connect again to get a new one; if it persists, check that your key can use ${model}. (${message})`;
  if (code === "NETWORK_ERROR")
    return `Could not reach ${where}. Check your network connection. (${message})`;
  return message;
}
