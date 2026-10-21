// Modified by Reactor Technologies, Inc.; the changes are listed in
// eden-live/PORTING.md ("The vendored game capture").
/**
 * host/input.js — keyboard state, repo-owned replacement for the per-game
 * KeyState the adapted games used to vendor from PrototypeShell.js.
 *
 * One input object is shared by host and game (game contract, docs/):
 * the game declares its key bindings with `input.bind({...})` mapping
 * KeyboardEvent.code values to action names, then reads the state inside
 * `update(dt, input)` via `input.isDown(action)`. The host owns the
 * listeners, `clear()` on tab visibility changes, and the reserved
 * `restart` action (host handles the restart lifecycle end to end).
 *
 * Actions are abstract strings, never keycodes, so a game never needs to
 * know the physical layout — rebinding is a one-line bind() change.
 */

const RESERVED_ACTIONS = new Set(['restart']);

export function createInput() {
  const map = Object.create(null);     // event.code -> action
  const down = Object.create(null);    // action -> boolean held state
  const pressed = Object.create(null); // action -> one-shot press to consume
  const codes = new Set();             // event.code keys held right now

  let attached = false;

  // Semantics follow the legacy KeyState the adapted games were written
  // against: several physical keys may map to one action (W and ArrowUp
  // both 'throttle'), the action stays held until the LAST of its keys is
  // released, auto-repeat never re-arms the one-shot latch, and a second
  // key of an already-held action does not count as a new press.
  function handler(event, isDown) {
    const action = map[event.code];
    if (action === undefined) return;
    if (!isDown) {
      codes.delete(event.code);
      let still = false;
      for (const code of codes) if (map[code] === action) { still = true; break; }
      if (!still) down[action] = false;
      return;
    }
    if (event.repeat || codes.has(event.code)) return;
    codes.add(event.code);
    if (!down[action]) pressed[action] = true;
    down[action] = true;
  }
  const onKeyDown = (event) => handler(event, true);
  const onKeyUp = (event) => handler(event, false);

  /** Declare the game's bindings, e.g. {KeyW: 'forward', Space: 'jump'}. */
  function bind(bindings) {
    Object.assign(map, bindings);
  }

  function attach() {
    if (attached) return;
    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    attached = true;
  }

  function detach() {
    if (!attached) return;
    window.removeEventListener('keydown', onKeyDown);
    window.removeEventListener('keyup', onKeyUp);
    attached = false;
  }

  /** Held-state query, e.g. input.isDown('forward'). */
  function isDown(action) { return down[action] === true; }

  /** One-shot query: returns true once per physical press. */
  function consumePress(action) {
    if (pressed[action] !== true) return false;
    pressed[action] = false;
    return true;
  }

  /** Drop all held and pending state (tab switches, restarts). */
  function clear() {
    codes.clear();
    for (const action in down) down[action] = false;
    for (const action in pressed) pressed[action] = false;
  }

  // Look rate held by the client (mouse look), [yaw, pitch] each in -1..1.
  // Set by the host, never by keyboard events, and kept across clear(): the
  // host sends it only when it changes.
  let lookRate = [0, 0];

  /** Hold a look rate until the next call; [0, 0] is none. */
  function setLook(yaw, pitch) {
    lookRate = [Number(yaw) || 0, Number(pitch) || 0];
  }

  /** The held look rate, [yaw, pitch]. */
  function look() { return lookRate; }

  /** Bound action names, unique. */
  function actions() {
    const out = [];
    const seen = Object.create(null);
    for (const code in map) {
      const action = map[code];
      if (action !== undefined && seen[action] !== true) {
        seen[action] = true;
        out.push(action);
      }
    }
    return out;
  }

  return Object.freeze({ bind, attach, detach, isDown, consumePress, clear, actions, setLook, look });
}
