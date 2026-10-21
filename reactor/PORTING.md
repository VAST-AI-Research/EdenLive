# EdenLive — port notes

## What the model is

EdenLive 5B (VAST-AI, built on FlashDreams) is a causal draft-to-video model.
Each chunk it reads three draft frames through a DINO encoder, denoises in four
steps against a cache opened from a reference image and a prompt, and decodes
9 frames (first chunk) or 12 with LightTAE. Upstream serves it behind its own
WebRTC stack, driven by code games (`game2v`) or a camera (`cam2v`).

## Where the port started

Ruixing's port (`reactor-team/reactor-edenlive@e11bca2`, imported as 0.1.0)
was already a `ReactorApp` in two halves on runtime 3.8.3:

- `edenlive_model.py` / `edenlive_backend.py` / `edenlive_attention.py`: the
  model half. Upstream's 5B pipeline behind `load` / `generate` / `reset`,
  one `EdenInput` in and one `EdenResult` out, nothing from
  `reactor_runtime`, run in its own process by `DistributedRunner`. A new
  `world_id` opens a new world; the result reports `frames_wanted`.
- `edenlive_app.py` / `edenlive_game.py`: the application half. The game is
  simulated and rendered headless on the server, and keys are sampled once per
  captured frame from a latest-value mailbox.
- Refusals already raised `CommandError`.

## What 1.0.0 changed, and why

**A client video source next to the game.** The model only ever sees three
draft frames per chunk, and the game's draft is its own render at chunk-local
frames 0, 4, 8 (upstream `GameTimeline` samples chunk-locally on purpose). So
any RGB video can drive it. The port gained a `source_video` input track and a
`set_source` command, and the game and the client became two implementations
of one draft-source protocol (`prepare` / `take` / `set_keys` / `set_paused` /
`close`) returning a `Draft`. The step derives the condition frames, the
prompt, the paired output, and the outcome from the `Draft` alone, so there is
one path for both sources. The condition stride moved into the model half
(`CONDITION_STRIDE`, `condition_frames`). The game's separate condition stack
is gone; `EdenGame.take()` checks that the native flags match the shared slice.

**A switch keeps the world.** `set_source` is a hand-written command, not a
public state field, because a switch during a world has to close one source
and open the other between steps. It does not bump `world_id`: the model keeps
its cache and the look carries over, and the new draft takes over gradually.
Switching to the game opens it from the start, because a game's simulation
cannot resume where a client stream left off. An outcome belongs to the game,
so switching away clears `ended`.

**`client` mode needs a reference, not a game.** `start` asks for a reference
(`image_required`), and `set_image` skips the game requirement. `select_game`
still works in `client` mode as a quick way to borrow a game's cover and
prompt as the look.

**The contract was polished** (`polish-reactor-model-schema`): every command
says what, when, what it emits, and which codes it fails with. The snapshot
message lost the model prefix (`eden_status` to `state_update`) and gained
`source`. That rename is the breaking change behind the major bump.

**Kept as is.** The app still resolves the model's execution flags
(`attention_backend`, `cuda_graph`, `compile_model`) from `edenlive.yaml` and
passes them to `load()` as plain values; the model half reads no config.
`@disconnected` still releases held keys, because keys are the only thing a
client holds, and only for the client that holds them.

## What came after 1.0.0, and why

These shipped as `edenlive` 1.0.1 to 1.0.3 and are folded into `eden-live`
1.0.0 (see "The rename" below). None of them moves the schema.

**Load pays for compilation (`warmup: true`).** The first chunks of the first
world used to compile for about a minute in front of a client. `EdenModel.load()`
now generates throwaway worlds with random input, past the CUDA graph capture
point and through a prompt change and a fresh world, then calls `reset()`. The
warmup lives in the model half because it only drives the model's own
`generate()`; the application passes the flag through like the other execution
flags. The same flag then has the application play a throwaway session of every
game through its own commands and step calls (`EdenLive._warm_up`), because the
costs a fresh process pays in the application (browser launch, each game's first
captures, the runner's shared-memory growth) lie outside the model half.

**Chunk timing is measured on the application side** (`edenlive_timing.py`).
Only the application sees the whole cycle (the blocking `emit()`, refused
steps, `generate()`, the source's capture), so the timer lives there and the
model half stays unaware of it. A summary covers one measurement run: a pause,
`start`, `set_source`, and a new session all start a new run and drop the
samples not yet summarized, so a summary never mixes worlds, sources, or
sessions.

**The game renders on the GPU, and each chunk is captured on demand.** On
SwiftShader the game took 0.62-0.65 s per 12-frame chunk at 1280x704, more than
the 0.5 s a chunk plays for, and it slowed further under CPU contention.
Chromium could not reach the GPU because the image lacked the GLVND EGL loader
NVIDIA's driver plugs into and the container did not ask for the graphics
driver capability. With `libegl1` and `libgles2` installed,
`NVIDIA_DRIVER_CAPABILITIES` including `graphics`, and `EDEN_GAME_GPU=1`, the
game renders on the model's B200 and a chunk captures in about 0.2 s at the
full size. `game_capture` stays a setting: if a deployment falls back to
SwiftShader (the renderer logs which one it chose), `[832, 458]` keeps a
software capture faster than real time, and `EdenGame.take()` scales it to the
1280x704 contract inside the source, never past `take()`. With the capture that
fast, the application no longer prepares the next chunk while `generate()`
runs: each chunk is captured when its step asks for it, so a key accepted
between steps reaches the next chunk. Key to received frame dropped from
1.24-1.32 s to 0.69-0.86 s, and the serial cycle still fits 24 fps. A future
port keeps both: render on the GPU, and capture on demand unless the capture
cannot fit beside `generate()` in one chunk's playout time.

**The draft and the generated frames are two tracks.** `main_video` carries
the generated frames and `draft_video` the draft each was made from, both
1280x704, emitted together with the same timestamps and metadata. One
2560x704 frame forced every client to decode and display both halves, doubled
the width the encoder and the network carry, and could not be shown apart.

**Nothing on the event loop waits for the browser (1.1.1).** On runtime 3.8.3
every command handler and lifecycle hook runs on the model's event loop under
the step lock, and `@session_ended` is synchronous. `EdenGame.close()` used
to call `GameCaptureWorker.close()` inline, which waits for the single capture
thread, which in turn waits inside Playwright for Chromium. One wedged browser
(REA-7232: Chromium alive hours after its session, `page.evaluate` never
answering) therefore parked the whole model loop: every later session got no
`state_update` and no command reply, while the runner still answered
`invalid_command` and logged session transitions. `close()` now returns at
once and hands the browser to its own thread, bounded by `CLOSE_TIMEOUT_S`;
past it, the Chromium processes under the game's Playwright driver are killed
and the blocked call fails on its own. The driver is deliberately left alive:
killing it stops the loop Playwright's sync bridge spins on, and the capture
thread then busy-loops forever on `browser.close()`. A capture that has not
advanced a frame for `CAPTURE_TIMEOUT_S` of unpaused time raises
`CaptureStalled` from `take()` (paused time is excluded, because a pause holds
the capture mid-chunk on purpose);
`process_input()` replaces the game with a fresh one from its opening (as a
`set_source` back to `game` does) and refuses the step, so a wedged game costs
a restart, never the session or the pod. Both were reproduced on a B200 by
`SIGSTOP`ping Chromium mid-game. Command entry, step refusals, and session
teardown now log at info so the next such hang is visible in production logs.

**Start does not wait for a browser or a garbage collection (2.0.1).** Served
on a B200, Start to the world's first generated chunk took 1.6-2.0 s: about
0.7-0.9 s to launch Chromium and load the game page (0.27 s of it starting the
Playwright driver, 0.29 s for the page's game modules), 0.25-0.8 s for the
upstream `initialize_cache`, then the eager first chunk. Of the cache build,
about 0.21 s was a full `gc.collect()` the upstream pipeline runs after
re-encoding the opening prompt through a recording wrapper, a path meant for
releasing the text encoder, which this port keeps loaded. Two changes:

- The game is launched ahead. `EdenGame.launch()` launches the browser and
  loads the page on the capture thread; the app keeps one such standby for the
  selected game, launched at `select_game` and again after each chunk, and
  `_open_source()` takes it. The page is idle at its opening until its first
  chunk, so captures are byte-identical to a game launched by that chunk
  (7 games x 16 chunks). Since a standby and the playing game run at once,
  each game now records its Playwright driver as it starts it, under a
  process-wide lock, instead of scanning for new child processes later.
- `EdenBackend.start` records the opening prompt's embeddings first
  (`anchor_embeddings`, kept for the process), so `initialize_cache` takes its
  reuse branch: no re-encode, no `gc.collect()`. Outputs, the recorded
  embeddings and every block's text K/V are bit-identical to before.

Served, Start to first generated chunk is 0.54-0.74 s when Start comes 2 s
after the selection or restarts a world. Still open: the first chunk itself
runs eager (about 0.35 s, a graph would need a per-token-timestep variant for
the denoise passes and a scalar one for the finalize pass), and a chunk reaches
the client 0.1-1.0 s after it is generated, a spread measured before these
changes too.

**The client source plays at the client's own rate (2.1.0).** A `play_fps`
for every source broke video-to-video: a game source's frames are made when
the app asks for them, so it can play them at any rate it sustains, but a
client's frames arrive at the client's rate. A 24 fps client delivers 12 new
frames every 0.5 s while a chunk played at 40 fps lasts 0.3 s, so every chunk
held its last frame for about 0.2 s (11.6 s of 28 s, served on a B200). The
draft protocol now carries an optional rate: `Draft.fps` is set by
`ClientSource` from the frames the `source_video` track has received between
chunks (smoothed, clamped to 10-60 fps, 24 until measured; WebRTC frames
carry no pts in runtime 3.8.3, so arrival is the only clock), and the app
re-paces output through `self.output.fps` only when that rate changes. Game
drafts leave it unset, so the game source keeps the class-pinned `play_fps`.
The rate follows the client rather than the model's throughput: the model
cannot make a chunk before its 12th frame has arrived, and playing faster
than frames arrive only moves the wait onto the screen. Still open: there is
no buffer, so a batch of client frames arriving late shows as a hold of the
same length.

## The rename

The model was imported and released as `reactor/edenlive` (0.1.0 to 1.0.3).
It is now the `eden-live` folder and registers as `reactor/eden-live`,
starting again at 1.0.0 with everything `edenlive` 1.0.3 shipped. The Python
modules keep their `edenlive_` prefix and `runtime.import` stays
`edenlive_app:EdenLive`: the name only has to differ from the upstream
`edenlive` package, which these modules must not shadow.

## Not verified on a GPU

The client source is covered by CPU tests (fitting, latest-frame reads, the
shared step, mid-world switches), not by a live B200 run. The reskin quality of
arbitrary client video, and how fast the look follows a mid-world switch, still
need a session against the deployment.

## The vendored game capture

The adapted renderer and modified host scripts run from `capture/`.
The unchanged `GameTimeline` is imported from the bundle's
`realtime/integrations_v2/eden/apps/game2v/game_timeline.py`, and unchanged
page resources, including `host/html-to-image.js`, come from the bundle's
`realtime/games/`. The application adds `realtime/integrations_v2` to its
import path when opening a game; the model worker resolves its own imports. One
capture serves every game; what is particular to a game stays with that game
in the bundle: its JS and assets, its registry entry (title, base prompt,
cover, controls) and the event sentences its `dynamicPrompt` writes.

Origin: the EdenLive source ("EdenLive-Official"), `realtime/` tree, as
shipped in the eden-live 1.1.0 bundle under `source/realtime/`. That tree is
NVIDIA FlashDreams' EdenLive integration, Apache-2.0 (its `LICENSE`,
`NOTICE` and `REUSE.toml`). `capture/LICENSE` is the Apache-2.0 text and
`capture/NOTICE` is upstream's NOTICE unchanged. The MIT `html-to-image.js`
library is served from upstream, with its upstream attribution.

| Vendored file | Upstream path under `realtime/` | Upstream SHA-256 | Changed |
|---|---|---|---|
| `capture/game_renderer.py` | `integrations_v2/eden/apps/game2v/game_renderer.py` | `9d8328c447c0ee2bfbb0f491a3f0e985075f97949a6b4eba1cefcaedb1ab93fb` | yes, below |
| `capture/host/backend.js` | `games/host/backend.js` | `96133c3601c3319af1e1068e945911f0e15ddc3a0955a7a9ca177b28d72945cb` | yes, below |
| `capture/host/input.js` | `games/host/input.js` | `447d5c41496c00d45887a0cdf553f938e1643e2df75221c331082833af185457` | yes, below |
| `capture/host/demo-runtime.js` | `games/host/demo-runtime.js` | `78c82174af1f8d0c342b204e7bda5ebb26c013dd7e55019a68bcc729965b1acc` | yes, below |
| `capture/host/standalone.js` | `games/host/standalone.js` | `9116342aef4af2dc6181b47cd64e2628aca43f6b16dc76076745f40e80f4a309` | yes, below |

`host/` holds the page scripts the capture path loads: `backend.js` (with
`input.js`) drives a game built on `createRuntime`, as all seven official
games are, and every one of them imports `demo-runtime.js`;
`standalone.js` and upstream's `html-to-image.js` drive an imported standalone game.
The other files in upstream's `games/host/` belong to its live-preview page
and are still served from the bundle if a page asks for them.

The browser demo uses the original seven game directories and covers directly:
Vite maps them to `local-games/` in development and emits them into `dist/`.
The mapping accepts both the original slugs and the renamed upstream
`deepwater-shock`, `kaiju-rampage` and `desert-crossfire` directories.
Only the input and demo-runtime hosts are needed by this frontend's iframe.
Three.js comes from npm, with its license included in the build. None of these
unchanged assets are checked into the adapter.

Changes to `game_renderer.py`:

- `HOST_ROOT` and one branch in `GameRenderer._route`: a request under
  `/host/` is answered from `capture/host/` when the file is there.
- `capture_format` (`GameRenderer`, `GameCaptureWorker`): `jpeg`, the
  default, is the upstream path unchanged. `raw` launches Chromium with a
  persistent profile (under `/dev/shm` when it exists), serves the game from
  `https://eden.local` so the page is a secure context, and calls
  `_render_chunk_raw` instead of the JPEG loop: each frame's RGBA pixels land
  in a slot of the page's origin-private frames file, which this process
  reads with `pread` and flips to top-down RGB on a decoder thread while the
  page produces the next frame.

- `condition_only` (`GameRenderer`, `GameCaptureWorker`; set by
  `draft_video: sparse`, the default): a frame without `capture_condition` (never the first
  of a chunk) goes through `advanceToFrameNoReadback` and reuses the previous
  frame's array as its `ui_frame`, in both the JPEG and the raw loop.

- `launch()` and `_ensure_renderer()` (`GameCaptureWorker`): launch the
  browser and load the page on the capture thread without capturing; the
  first capture launches it as before when nothing did.
- `start_driver` (`GameRenderer`, `GameCaptureWorker`): a callable that starts
  the Playwright driver in place of `sync_playwright().start()`, so the
  caller can record which process it is. Unset, the upstream call runs.

Changes to `host/backend.js`: `advanceToFrameRaw`, `rawOpen` and the worker
that writes the frames file; `advanceToFrameNoReadback`, which injects the
keys and steps the game exactly like `advanceToFrame` but neither renders nor
reads back, and returns only the tick and HUD state. `advanceToFrame` and
`capture()` are unchanged. Skipping the render is safe only while a game's
logic does not read back from its render: none of the seven shipped games'
`game.js` touches three.js or the scene, and `demo-runtime.js` draws each
frame with `scene.render(game.s)` from the game state alone. A game that reads
its render needs `draft_video: full`.

**Sparse is the default.** `edenlive.yaml` sets `draft_video: sparse`, and
`sparse` is also the default when the key is absent. Served on one B200, a
12-frame game chunk captures in 0.044 s (dust2) and 0.039 s (freeze) sparse,
against 0.124 / 0.117 s raw with `full` and 0.214 / 0.203 s as JPEG. The
model's conditioning is byte-identical to `full` for the shipped games; what
changes is `draft_video`, which now repeats each condition frame until the
next. A port whose game logic reads its render, or whose client shows the
draft at 24 fps, sets `draft_video: full`.

Changes for mouse look (`set_look`):

- `host/input.js`: `setLook(yaw, pitch)` and `look()` hold a look rate beside
  the keyboard state. Keyboard events never touch it, and `clear()` keeps it,
  because the host sends it only when it changes.
- `host/backend.js`: `dispatchEvents` takes `{look: [yaw, pitch]}` entries
  next to the key events and hands them to `input.setLook`. `EdenGame._drain`
  sends one only when the held rate changes, so a session that never calls
  `set_look` sends the page exactly what it did before.
- `host/demo-runtime.js`: `controls()` adds `rate * LOOK_MAX` (0.2 rad per
  24 Hz game step, the largest per-step turn any of the seven games accepts)
  to the arrow keys' `lookX` / `lookY` and clamps the sum to ±`LOOK_MAX`. Each
  game's `step()` then clamps to its own limit (±0.15 in godzilla, ±0.2 in the
  others). With a zero rate the keys' value passes through untouched.
- `host/standalone.js`: skips look entries. A standalone game reads its own
  keyboard and pointer events and has no look input to set; driving one would
  mean synthesising `mousemove` events with `movementX` / `movementY` (and
  faking pointer lock) per frame, which only a game that listens for them
  would follow.

## Raw game capture (`capture_format: raw`)

Of the JPEG capture's 0.21 s per 12-frame chunk on a B200, about 0.12 s is
`canvas.toDataURL` on the page's main thread (half of it a blocking GPU
readback, half the JPEG encode) and about 0.04 s is decoding the JPEG here.
`raw` drops both codecs. The pixels still have to leave the page, and every
page-to-Python channel that goes through the browser process (the
Playwright call, a fetch POST or a WebSocket to a local server) carries 10-30
MB/s, so one 3.6 MB RGBA frame would take 110-380 ms. An
origin-private file written by a page worker with a sync access handle does
not go through the browser process: in a profile on `/dev/shm` a frame is
written in about 2 ms and read here in about 0.4 ms. That needs a secure
origin, which `https://eden.local` (served by the same route handler) is.

`readPixels` returns rows bottom-up and with alpha; the flip and the RGB copy
happen in `_read_raw`. A raw frame is byte-identical to a PNG of the same
canvas.

The readback stays synchronous. Reading into a pixel-pack buffer behind a
fence and collecting the previous frame on the next call was measured too:
the fence had nearly always signaled by then, yet `getBufferSubData` still
took as long as `readPixels` (about 5.5 ms for 1280x704), because the cost is
copying the pixels from the GPU process into the page, which happens on the
page's main thread either way. It added a call per chunk and saved nothing.

**Raw is the default.** Raw drafts replace the JPEG drafts as the model's
conditioning (and as `draft_video`). Before the switch, generated output
conditioned on raw drafts was compared side by side with output conditioned on
JPEG drafts of the same game run, on two games, with a rerun of the same input
as the noise reference: the differences are pixel-level only, with no visible
change in what the model does. `capture_format: jpeg` restores the previous
frames exactly.
