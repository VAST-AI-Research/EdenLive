// One official game run inside this page, for the "Game in your browser" source.
//
// Vite serves /local-games/ from upstream's realtime/games and emits those assets into
// dist/ for static deployment. Three.js comes from npm. The game runs in a
// same-origin iframe because its modules import "three" and "/host/..." by bare or root
// path, which only an import map in the game's own document can resolve. The page reads
// the game's canvas (rendered with preserveDrawingBuffer, so it can be copied at any time)
// and forwards the player's keys and mouse-look rate into the game's window. Every key that
// window receives, typed into the game or forwarded by the page, is reported back, so the
// page's control panel shows exactly what the game sees.

// Resolved against the page, so the build works from any static host or sub-path; absolute,
// because the game's own document is an about:srcdoc iframe.
export const LOCAL_GAMES = new URL(`${import.meta.env.BASE_URL}local-games`, document.baseURI).href;

const gamePage = (slug) => `<!doctype html>
<html><head><meta charset="utf-8">
<script type="importmap">${JSON.stringify({
  imports: {
    three: `${LOCAL_GAMES}/node_modules/three-demo/build/three.module.js`,
    "/host/": `${LOCAL_GAMES}/host/`,
  },
})}</script>
<style>html,body,#app{margin:0;width:100%;height:100%;overflow:hidden;background:#11120f}
/* The games size their renderer with an inline 1280x704 px style; the pane shows the whole
   frame scaled to fit, letterboxed like the generated video beside it. */
canvas{display:block;width:100%!important;height:100%!important;object-fit:contain}</style>
</head><body><div id="app"><canvas id="film" width="1280" height="704"></canvas></div>
<script type="module">
// Codes held in this window, from real keys or the page's forwarded ones.
const held = new Set();
let report = () => {};
window.onGameKey = (fn) => {
  report = fn;
  for (const code of held) fn(code, true);
};
const track = (down) => (e) => {
  if (held.has(e.code) === down) return;
  down ? held.add(e.code) : held.delete(e.code);
  report(e.code, down);
};
addEventListener("keydown", track(true), true);
addEventListener("keyup", track(false), true);
window.press = (code, down) => {
  if (held.has(code) !== down) dispatchEvent(new KeyboardEvent(down ? "keydown" : "keyup", {code}));
};
window.releaseAll = () => {
  for (const code of [...held]) dispatchEvent(new KeyboardEvent("keyup", {code}));
};
// Mouse look: a held [yaw, pitch] rate, each -1..1, applied on every game step on top of
// the arrow keys, as the server applies set_look. A rate of 1 is LOOK_MAX radians per step,
// the largest per-step turn the games accept; each game clamps again to its own limit.
const LOOK_MAX = 0.2;
let lookRate = [0, 0];
const aim = (keys, rate) =>
  rate ? Math.max(-LOOK_MAX, Math.min(LOOK_MAX, (keys || 0) + rate * LOOK_MAX)) : keys;
window.ready = (async () => {
  const {createInput} = await import("${LOCAL_GAMES}/host/input.js");
  const input = createInput();
  input.attach();
  window.setLook = (yaw, pitch) => {
    lookRate = [Number(yaw) || 0, Number(pitch) || 0];
    input.setLook?.(...lookRate);
  };
  // A host whose input holds a look rate applies it itself. Otherwise the rate is added to
  // the controls each step hands the game (the module main.js imports, so the same class).
  if (!input.setLook) {
    const {Game} = await import("${LOCAL_GAMES}/${slug}/adapter.js");
    const step = Game.prototype.step;
    Game.prototype.step = function (controls = {}) {
      const [yaw, pitch] = lookRate;
      if (!yaw && !pitch) return step.call(this, controls);
      return step.call(this, {...controls, lookX: aim(controls.lookX, yaw), lookY: aim(controls.lookY, pitch)});
    };
  }
  addEventListener("blur", () => {
    releaseAll();
    input.clear();
  });
  const {createRuntime} = await import("${LOCAL_GAMES}/${slug}/main.js");
  const runtime = await createRuntime({input, mode: "play", restart: () => runtime.reset()});
  runtime.reset();
  // A fresh game waits at its opening frame until the page starts it.
  runtime.handleUiAction("pause");
  // The model's own frame size, whatever the size of the pane showing it.
  runtime.layout(1280, 704);
  let last = performance.now();
  const frame = (now) => {
    const dt = Math.min(0.05, Math.max(0, (now - last) / 1000));
    last = now;
    runtime.update(dt, input);
    runtime.render(dt);
    requestAnimationFrame(frame);
  };
  requestAnimationFrame(frame);
  return {runtime, input};
})();
</script></body></html>`;

// The demo's key names -> KeyboardEvent.code, which is what the games bind.
const CODES = {
  " ": "Space",
  shift: "ShiftLeft",
  arrowup: "ArrowUp",
  arrowdown: "ArrowDown",
  arrowleft: "ArrowLeft",
  arrowright: "ArrowRight",
};
const codeOf = (key) => CODES[key] || `Key${key.toUpperCase()}`;
// KeyboardEvent.code -> the demo's key name, or "" for a key the demo does not show.
const nameOf = (code) =>
  code === "Space"
    ? " "
    : /^Shift/.test(code)
      ? "shift"
      : /^Arrow/.test(code)
        ? code.toLowerCase()
        : /^Key[A-Z]$/.test(code)
          ? code.slice(3).toLowerCase()
          : "";

/**
 * Load game `slug` into `iframe`; resolves once it renders. `onKeys` receives the demo key
 * names the game's window holds, after every change.
 */
export function mountLocalGame(iframe, slug, onKeys = () => {}) {
  return new Promise((resolve, reject) => {
    iframe.onload = async () => {
      const win = iframe.contentWindow;
      // A load of the previous, emptied document; ours sets `ready` before its load event.
      if (!win.ready) return;
      iframe.onload = null;
      try {
        const { runtime, input } = await win.ready;
        const codes = new Set();
        win.onGameKey((code, down) => {
          down ? codes.add(code) : codes.delete(code);
          onKeys([...new Set([...codes].map(nameOf).filter(Boolean))]);
        });
        resolve({
          canvas: runtime.canvas,
          /** Press or release one of the demo's keys (w, a, " ", shift, arrowup, ...). */
          key: (name, down) => win.press(codeOf(name), down),
          /** Hold a mouse-look rate, [yaw, pitch] each -1..1; [0, 0] is none. */
          look: (yaw, pitch) => win.setLook(yaw, pitch),
          /** Release held keys and the look rate. */
          release() {
            win.releaseAll();
            input.clear();
            win.setLook(0, 0);
          },
          /** Game state as the game's HUD sees it: state, health, outcome, paused. */
          read: () => runtime.read(),
          setPaused(paused) {
            if (runtime.read().paused !== paused) runtime.handleUiAction("pause");
          },
          /** Back to the opening frame, as freshly loaded; the pause state is kept. */
          reset() {
            const paused = runtime.read().paused;
            win.releaseAll();
            win.setLook(0, 0);
            runtime.reset();
            if (paused) runtime.handleUiAction("pause");
          },
          destroy() {
            runtime.destroy?.();
            iframe.srcdoc = "";
          },
        });
      } catch (e) {
        reject(
          Error(
            `The ${slug} game did not load from ${LOCAL_GAMES}/: ${e.message || e}`,
          ),
        );
      }
    };
    setTimeout(() => reject(Error(`The ${slug} game did not load within 20 s.`)), 20000);
    iframe.srcdoc = gamePage(slug);
  });
}
