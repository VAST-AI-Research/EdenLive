// SPDX-License-Identifier: Apache-2.0
// Modified by Reactor Technologies, Inc.; the changes are listed in
// eden-live/PORTING.md ("The vendored game capture").
import {createInput} from './input.js';

const SIM_HZ = 60;
const input = createInput();
input.bind({KeyR: 'restart'});
input.attach();
const {createRuntime} = await import(`/${window.gameSlug}/main.js`);
let encodeUiState = (state) => state;
try {
  const transport = await import(`/${window.gameSlug}/hud/transport.mjs`);
  if (typeof transport.encode === 'function') encodeUiState = transport.encode;
} catch {
  // Games without a HUD transport just forward runtime.read() snapshots.
}
const runtime = await createRuntime({input, mode: 'stream', restart: () => runtime.reset()});
runtime.reset();
runtime.layout(window.innerWidth, window.innerHeight);
let tick = 0;
let renderedTick = 0;

// The client forwards KeyboardEvent.key lowercased, so named keys arrive as
// 'shift' / 'escape' rather than a code. Either Shift maps to ShiftLeft; the
// games bind both sides to the same action.
const NAMED_CODES = {' ': 'Space', shift: 'ShiftLeft', escape: 'Escape', enter: 'Enter', tab: 'Tab'};

function toCode(action) {
  if (NAMED_CODES[action]) return NAMED_CODES[action];
  if (action.length === 1 && /[a-z]/i.test(action)) return `Key${action.toUpperCase()}`;
  if (action.startsWith('arrow')) return `Arrow${action.slice(5, 6).toUpperCase()}${action.slice(6)}`;
  return action;
}

function dispatchEvents(events) {
  for (const event of events || []) {
    // {look: [yaw, pitch]}: a held look rate, applied by the game runtime on
    // every step until the next one.
    if (event.look) {
      input.setLook(event.look[0], event.look[1]);
      continue;
    }
    const {action, phase} = event;
    if (phase === 'down' && runtime.handleUiAction?.(action)) continue;
    window.dispatchEvent(new KeyboardEvent(phase === 'down' ? 'keydown' : 'keyup', {code: toCode(action)}));
  }
}

function simulateTo(targetTick) {
  if (!Number.isSafeInteger(targetTick) || targetTick < tick) throw Error('Invalid target tick');
  while (tick < targetTick) {
    if (input.consumePress('restart')) runtime.reset();
    runtime.update(1 / SIM_HZ, input);
    tick++;
  }
}

// The JPEG is the raw scene canvas, rendered once per model frame and used
// by both consumers: the client's game window, and the model conditioning on
// the latent-aligned subset. The HUD snapshot comes from the game's own
// transport module and is drawn as HTML over the generated video, so it never
// enters the JPEG.
function readState() {
  try {
    return encodeUiState(runtime.read());
  } catch {
    return null;
  }
}

function capture() {
  // Visual interpolation covers the sim time between captured model frames,
  // which GameTimeline already spaces at the model's frames_per_second.
  // A 16 fps model therefore gets dt ≈ 1/16, not a hardcoded 1/24.
  runtime.render((tick - renderedTick) / SIM_HZ);
  renderedTick = tick;
  return {
    tick,
    width: runtime.canvas.width,
    height: runtime.canvas.height,
    ui: runtime.canvas.toDataURL('image/jpeg', 0.8),
    state: readState(),
  };
}

// Raw capture (capture_format: raw): the canvas's RGBA pixels, read back with no
// image encode, handed to a worker that writes them into an origin-private file
// the Python side reads directly. Each frame of a chunk has its own slot
// (`offset`), so Python can read frame k while frame k+1 is produced.
const RAW_WORKER = `
let handle = null;
onmessage = async ({data}) => {
  try {
    if (!handle) {
      const root = await navigator.storage.getDirectory();
      const file = await root.getFileHandle('frames.bin', {create: true});
      handle = await file.createSyncAccessHandle();
    }
    const bytes = data.token ? new TextEncoder().encode(data.token) : new Uint8Array(data.pixels);
    handle.write(bytes, {at: data.token ? 0 : data.offset});
    handle.flush();
    postMessage(null);
  } catch (error) {
    postMessage(String(error));
  }
};`;
let rawWorker = null;

function rawWrite(message, transfer = []) {
  rawWorker ??= new Worker(URL.createObjectURL(new Blob([RAW_WORKER], {type: 'text/javascript'})));
  return new Promise((resolve, reject) => {
    rawWorker.onmessage = ({data}) => (data === null ? resolve() : reject(Error(data)));
    rawWorker.postMessage(message, transfer);
  });
}

function captureRaw(offset, png) {
  runtime.render((tick - renderedTick) / SIM_HZ);
  renderedTick = tick;
  const canvas = runtime.canvas;
  const gl = canvas.getContext('webgl2');
  const pixels = new Uint8Array(canvas.width * canvas.height * 4);
  gl.readPixels(0, 0, canvas.width, canvas.height, gl.RGBA, gl.UNSIGNED_BYTE, pixels);
  // `png` is a lossless copy of the same canvas, for checking the raw path only.
  const frame = {
    tick,
    width: canvas.width,
    height: canvas.height,
    state: readState(),
    png: png ? canvas.toDataURL('image/png') : null,
  };
  return rawWrite({pixels: pixels.buffer, offset}, [pixels.buffer]).then(() => frame);
}

window.gameBackend = {
  dispatch(events) { dispatchEvents(events); },
  // One model frame: inject whatever arrived since the last call, step the
  // 60 Hz sim up to that frame's tick, and capture. The Python worker paces
  // these calls across the last generate duration so keys can land mid-chunk.
  advanceToFrame({events, targetTick}) {
    dispatchEvents(events);
    simulateTo(targetTick);
    return capture();
  },
  // advanceToFrame for capture_format: raw. The pixels land in the frames file
  // at `offset`; the reply carries only the tick, the size and the HUD state.
  advanceToFrameRaw({events, targetTick, offset, png = false}) {
    dispatchEvents(events);
    simulateTo(targetTick);
    return captureRaw(offset, png);
  },
  // A frame whose pixels are not needed (draft_video: sparse): the same keys
  // and ticks as advanceToFrame, but no render, readback or encode. The next
  // rendered frame is unchanged only if the game's logic never reads back
  // from its render, which holds for every shipped game. renderedTick is left
  // alone, so the next render sees the whole interval as its dt.
  advanceToFrameNoReadback({events, targetTick}) {
    dispatchEvents(events);
    simulateTo(targetTick);
    return {tick, state: readState()};
  },
  // Create the frames file and stamp it with `token` so Python can find it.
  rawOpen(token) {
    return rawWrite({token});
  },
  // Dialog pause: take dismissing input and redraw at the current tick without
  // advancing the 60 Hz simulation, so an outcome or help card can be closed
  // while nothing is being generated. R-restart is normally consumed inside the
  // sim loop, so it is driven here explicitly; Z-undo lives in render().
  probe(events) {
    dispatchEvents(events);
    if (input.consumePress('restart')) runtime.reset();
    runtime.render(0);
    return {tick, state: readState()};
  },
  // Burst the whole chunk with no wall-clock pacing. Tests and the first
  // (unpipelined) chunk use this path.
  advanceChunk({events, frames}) {
    dispatchEvents(events);
    const captured = frames.map((frame) => {
      simulateTo(frame.targetTick);
      return capture();
    });
    return {tick, frames: captured};
  },
  destroy() { runtime.destroy(); input.detach(); },
};
