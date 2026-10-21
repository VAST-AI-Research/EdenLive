# EdenLive — model behaviour

EdenLive 5B reskins a **draft video** into a generated world, chunk by chunk.
The client stages an appearance reference and a prompt, picks where the draft
comes from, and steers it. Every chunk returns the generated frames on
`main_video` and the draft frames they were made from on `draft_video`, frame
for frame.

The draft has two sources, switched with one command:

- **`game`**: one of the seven official code games, simulated and rendered
  headless on the server. The client steers it with `set_keys`. This is the
  native EdenLive setting.
- **`client`**: the client's own `source_video` track (a webcam, a screen
  capture, a three.js canvas, a file). The model reskins whatever the client
  sends.

Both sources feed the model through the same path (see "The shared draft path"
below). The model does not know which source drew a chunk.

---

## Tracks

| Direction | Name | Type | Rate | Format |
|---|---|---|---|---|
| In | `source_video` | Video | any steady rate, 10-60 fps (24 typical) | `(H, W, 3)` uint8, any size; read only while the source is `client` |
| Out | `main_video` | Video | `play_fps` (40); with source `client`, the client's rate | `(N, 704, 1280, 3)` uint8: the generated frames |
| Out | `draft_video` | Video | same as `main_video` | `(N, 704, 1280, 3)` uint8: the draft each `main_video` frame was made from; under the default `draft_video: sparse`, a game draft repeats each condition frame (every fourth) until the next |

`N` is 9 for a world's first chunk and 12 after it. The two output tracks are
emitted together with the same timestamps, and each frame carries the same
metadata on both, so a client pairs them by `frame_id` or timestamp. Each
output frame carries metadata: `world_id`, `chunk`, `source`, and per source either `frame_id`,
`simulation_tick`, `keys` (game) or `pts` (client).

Client frames are scaled to cover 1280x704 and center-cropped. A chunk reads
the **latest** frames the client sent, never a backlog, so the draft follows
the live video.

**Pre-roll.** A new WebRTC connection's bandwidth estimate starts near 6 Mbps
and grows only while video flows, while a world's opening frames need about
9 Mbps, so without help the first world on every connection queues frames in
the sender for up to 2 s and the browser drops and freezes (1.3-2.5 s frozen in
the first 10 s, served on a B200 over Tailscale). From a client's connect until
`start`, and again when `select_game`, `set_image` or a reset stages a new
reference, the app plays the staged reference with light per-frame noise on
both tracks at 40 fps, so the estimate has grown when the world begins (frozen
under 0.3 s, no drops). It stops at `start`, at the last disconnect, at
session end, or after `preroll_s` seconds (edenlive.yaml, 20; 0 turns it off).
Every connected viewer receives it; it does not change generated frames.

**Output rate.** A game chunk is 0.5 s of the game's 24 fps clock. With source
`game`, output plays at `play_fps` from edenlive.yaml, 40 by default: a chunk
plays sooner after it is made, so a key shows up sooner, and the game runs 1.7
times faster than real time. `play_fps: 24` plays the game at its real speed.

With source `client`, output plays at the rate the client's frames arrive, not
at `play_fps`. A client chunk is the latest 9 or 12 frames, and the next chunk
needs as many new ones, so a chunk played at the client's own rate lasts just
as long as the client takes to send the next: the output never holds a frame
waiting for the client's video, and no sent frame is skipped. The WebRTC
transport gives inbound frames no `pts`, so the rate is measured on the server:
frames received on `source_video` over the server's clock, from one chunk to
the next, smoothed, clamped to 10-60 fps, and 24 fps until the first
measurement. Switching the source back to `game` plays at `play_fps` again.
Measured served on a B200 with the SDK client on the same host: a client
sending 24 fps gets output at 24 fps, one 12-frame chunk every 0.50 s; at 40
fps, output at 40 fps, a chunk every 0.30 s. A client faster than generation (about 40 new fps on a B200)
plays at its own rate, with the screen holding between chunks; the client
source does not resample by timestamp.

---

## Session lifecycle

The runtime drives one step at a time: `process_input()` (the application
decides whether a step can happen and builds the step input), `generate()`
(one chunk on the GPU), `process_output()` (pairing, metadata, `state_update`).

```
  connect ─────► state_update to the joiner
     │
  ┌──▼──────────────────────────────────────────────────────────────┐
  │ STAGING (paused, no world)                                       │
  │ set_source, select_game, set_image, set_prompt                   │
  │ process_input refuses: "Press Start, or resume the paused world." │
  └──┬──────────────────────────────────────────────────────────────┘
     │ start(seed)   game: needs select_game; client: needs a reference
  ┌──▼──────────────────────────────────────────────────────────────┐
  │ FIRST CHUNK                                                      │
  │ source delivers 9 draft frames, then the step carries the anchor │
  │ (reference, prompt, seed); the model opens a new world            │
  └──┬──────────────────────────────────────────────────────────────┘
  ┌──▼──────────────────────────────────────────────────────────────┐
  │ STEADY CHUNKS (12 frames)                                        │
  │ set_prompt, set_keys, set_source, set_paused apply between chunks │
  │ game: each chunk is captured when its step asks for it           │
  │ client: each chunk reads the latest 12 client frames             │
  └──┬──────────────────────────┬───────────────────────────────────┘
     │ set_paused(true)          │ game reaches an outcome
  ┌──▼────────────────┐   ┌──────▼─────────────────────────────────────┐
  │ PAUSED            │   │ ENDED (paused)                              │
  │ resume continues  │   │ set_paused(false) fails `game_ended`;        │
  │ the same world    │   │ start, or set_source away from the game     │
  └───────────────────┘   └─────────────────────────────────────────────┘
```

`select_game` and `set_image` need a paused world and end it; `start` begins
the next. `start` during a world replaces it. `@session_ended` closes the
source and resets the model. A client leaving releases the held keys only if it is the client that
set them; a viewer leaving changes nothing.

**The game is launched ahead.** With source `game`, `select_game` launches
the selected game's browser and loads its page (about 0.7 s on a B200), and
replies once the page is loaded, so a client that enables Start on that reply
never has a player wait for a browser; `set_source('game')` before a world does
the same. `start` takes that page instead of launching one. After
every chunk, another is launched for the same game if none is waiting, so a
`start` that replaces a world finds one too. This standby is a game nobody has
played: its page sits at the game's opening, untouched, so its first chunk is
exactly the one a game launched by that chunk would capture (frames, HUD
states, keys and events byte-identical on all seven games). At most one
standby exists. It is closed when another game is selected, when the source
switches to `client`, and when the session ends, so no browser outlives its
session. A `start` that comes before the launch finishes waits for the rest of
it inside the first capture, as before.

A game whose browser stops answering is replaced, not waited on. A capture
that has not advanced a frame for `CAPTURE_TIMEOUT_S` (60 s; a healthy chunk
captures in about 0.04-0.2 s) while the world runs is a stalled game. The clock
starts at the chunk's submission and restarts at every captured frame, and
stops while the world is paused: a pause holds the capture mid-chunk, so a
pause of any length resumes the same game. A stalled game's step is refused, the game restarts
from its opening in the same world, as `set_source` back to `game` does, and
the stalled browser is closed on its own thread and killed if it does not exit
within `CLOSE_TIMEOUT_S` (10 s). Chunks keep counting; nothing on the wire
reports the restart. The restart and the kill are logged at error and warning.

---

## Commands

Every refusal raises `CommandError(code, message)`: the caller's own call
fails with the code, and no other client hears of it. Every accepted command
replies with `state_update` and broadcasts it.

| Command | Valid | Effect | Fails with |
|---|---|---|---|
| `set_source(source)` | any time | `game` or `client`. Before `start`, picks what `start` uses. During a world, the next chunk draws from the new source and the world keeps its history; switching to `game` starts the game from its opening; switching away from an ended game lets resume work. | `game_required` (switch a started world to `game` before `select_game`) |
| `select_game(game)` | paused | Stage the game's cover as the reference and its prompt as `prompt`; ends the world. With source `game`, builds the game's prompt bank (below) while the game loads, and replies once both are done (about 1.5 s served on a B200, against 0.7 s for the load alone), so `start` after the reply never waits for the browser and its chunks find their usual prompts encoded. With source `client`, the cover and prompt style the client's video, and there is no bank. | `pause_required` |
| `set_image(image)` | paused | Stage an uploaded reference; ends the world. | `pause_required`, `game_required` (source `game`, no game yet), `invalid_image` |
| `start(seed)` | any time | New world from the reference, `prompt`, `seed`, and the current source; resumes. | `game_required` (source `game`), `image_required` (source `client`), `prompt_required` |
| `set_prompt(prompt)` | any time | Next chunk's prompt; history stays, so the look shifts gradually. The current text again changes nothing and replies at once. New text rebuilds the prompt bank: before a world, the reply waits for it (about 1.5 s); during a world, play goes on, the bank fills in the background, and until a prompt's entry is ready a chunk that needs it encodes it as before. | `prompt_required` |
| `set_keys(keys)` | any time | Keys held in the game; each uncaptured game frame reads the latest. Ignored while paused or with source `client`. | none |
| `set_look(yaw, pitch)` | any time | Look rate held in the game, each -1..1 (positive turns right / tilts up); each uncaptured game frame applies the latest until it is replaced, so (0, 0) stops it. Adds to the arrow keys' look, limited to the game's own per-step turn. Ignored while paused or with source `client`; reset with the keys. | none (out-of-range values fail validation) |
| `set_paused(paused)` | any time | Hold or resume the source and generation; releases keys and look. | `start_required`, `game_ended` |

## Messages

| Message | When | Payload |
|---|---|---|
| `state_update` | connect (joiner only), every accepted command, every chunk | `source`, `game`, `image_name`, `prompt`, `seed`, `keys`, `look`, `paused`, `started`, `ended`, `game_state`, `outcome`, `health`, `health_max`, `events`, `world_id`, `chunks`, `seconds` |

There is no `command_error` message: a refusal fails the call itself.

The game's status rides on the same message, so a client can show what the
game is doing without decoding the video:

- `game_state`, `outcome`, `health`, `health_max` are the game's HUD after
  the last frame of the chunk counted by `chunks` (`edenlive_game.hud_status`):
  `RUNNING` / `WON` / `LOST`, the result card once it ends (result, reason,
  advice), and player health where the game has it. They are empty or null
  before a world's first chunk, with source `client`, and after `start`,
  `select_game`, `set_image` and `set_source`.
- `events` are the event sentences that chunk added to the prompt
  (`event_lines`), the same text the model conditions on. A `state_update`
  sent by a command between chunks repeats them, so a client appends them
  once per new `chunks` value.

When a chunk ends the game, the server logs `game reached an outcome` at info
with the game, world, chunk, state, card and health.

---

## The shared draft path

A **draft source** is anything with the calls below; the application holds
exactly one in `self._draft_source`:

```python
kind: str                                  # "game" or "client"
def prepare(self, frames: int) -> None      # ask for the next chunk
async def take(self) -> Draft | None        # the chunk, or None while not ready
def set_keys(self, held) -> None            # a source without keys ignores it
def set_look(self, yaw, pitch) -> None      # a source without a camera ignores it
def set_paused(self, paused) -> None
def close(self) -> None             # returns at once; teardown finishes elsewhere
```

Every call runs on the model's event loop under the step lock, from a
command handler, the step, or `@session_ended`. None of them may wait on
another thread or process without a deadline: a source that blocks there
parks every command and lifecycle hook of every later session.

`EdenGame` (`edenlive_game.py`) and `ClientSource` (`edenlive_draft.py`) are
the two implementations. A `Draft` carries `frames` (RGB uint8
`[T,704,1280,3]`), per-frame `metadata`, prompt `events` for this chunk,
`ended`, the source's own `hud` (the game's state, outcome and health;
`None` for the client source), and `fps`, the rate its frames advance at
(the client's measured rate; `None` for the game, which plays at `play_fps`).
The application sets the output's live rate from it before the chunk is
emitted.

From `take()` on, one code path serves both sources:

1. `process_input()` derives the model's condition with
   `condition_frames(draft.frames)`, the draft's chunk-local frames 0, 4, 8
   (`CONDITION_STRIDE`, owned by the model half), and the prompt as
   `prompt + draft.events`.
2. `generate()` forwards `EdenInput` to the model half, which never learns
   the source.
3. `process_output()` pairs `draft.frames` with the generated frames, merges
   `draft.metadata`, and pauses the world when `draft.ended`.

The game's native condition frames are its own render at chunk-local frames
0, 4, 8; `EdenGame.take()` refuses any other layout, so the shared slice
reproduces the native pipeline exactly.

## Maintaining this behaviour

Agents and reviewers keep these invariants. A change that breaks one is wrong
even if it runs.

- **One draft path.** No `if source == ...` past `take()`. A difference
  between sources lives inside the source class, behind the protocol above.
  A new source (a recorded clip, a second game engine) is a new class with
  the same calls and one branch in `_open_source()`, nothing else.
- **The model half never learns the source.** `EdenInput` carries condition
  frames, prompt, world id, and anchor only. The stride lives in
  `edenlive_model.py`, and both sources reach it through `condition_frames`.
- **A source switch is not a new world.** `set_source` changes what the next
  chunk is drawn from; only `start` bumps `world_id` and sends an anchor.
- **Refusals raise `CommandError` with a stable code** named in the command's
  description, and change no state. No `command_error` message is published.
- **Nothing on the event loop waits for the browser.** Handlers, the step,
  and the lifecycle hooks run on one loop under one lock. A source closes on
  its own thread with a deadline and a kill; a capture has a deadline too;
  a standby game launches on its own capture thread. A new blocking call
  there is the REA-7232 hang again.
- **A standby game is untouched until `start` takes it.** Nothing sends it
  keys, look, pause, or a capture; only `launch()` runs on it. Each game
  records its own Playwright driver when it starts it (under a process-wide
  lock), since a standby and a playing game run side by side and a kill must
  reach only the wedged one.
- **Every state change ends in `state_update`.** A new observable field goes
  on `StateUpdate` and into `_state_update()`.
- **Keep this file, `PORTING.md`, and the descriptions in step.** A change to
  a command, a code, a track, or the draft path updates this file in the
  same change.

---

## Deployment

One NVIDIA B200, runtime 3.8.3, image built from `reactor.yaml` (no
Dockerfile). The game runs in headless Chromium (Playwright) in the
application process, rendering on the model's GPU; the model runs in its own process behind the runtime's
`DistributedRunner` with `world_size=1`.

The game timeline and unchanged page assets are loaded from the prepared
upstream source. `capture/` contains only the renderer and host scripts with
Reactor-specific capture or input changes. The demo also reads upstream game
files and covers, packaging them into `dist/` at build time; they are not
duplicated in the integration's source tree.

**Load compiles and warms up** (`warmup: true` in `edenlive.yaml`). After the
weights load, `EdenModel.load()` generates throwaway worlds with random input:
one past the steady-state graph (from chunk 6, after 2 wrapper warmups) and
through a prompt change, then two one-chunk worlds, then a fresh world, then
`reset()`. The DiT keeps one KV cache for the process (`edenlive_graphs.py`): a
new world resets its bookkeeping and writes its own text into the same
buffers, so CUDA graphs stay valid across worlds. A world's first chunk has two
graphs, one for its denoising passes (a per-token timestep: the reference
frame's tokens at t=0) and one for its cache commit (a scalar timestep); the
commit runs once per world, hence the one-chunk worlds that take it past its
2 warmups. Each cache-filling chunk 1 to 4 has its own graph and chunks from 5
on share one; all are captured once, in the warmup, and replayed in every
later world. Against recapturing per world, chunks 1 to 6's graphs cost about
1.1 GiB more device memory; the first chunk's two add 0.44 GiB at capture
(0.97 GiB between two loads, after warmup). Measured on a B200 with an empty compile cache: about 30 s of weights,
about 62 s of warmup (the first chunk alone compiles for about 52 s), 115 s
from container start to available. After it, a world's first chunk takes about
0.18 s including its reference encode (0.35 s when its DiT ran eager), chunks 1 to 6 0.15
to 0.21 s (0.31 to 0.36 s when they ran eager while the cache filled), and
every chunk after 0.21 s, with no compile spike or capture in any later world.

**Then the application plays a warm-up session** (same `warmup` switch,
`EdenLive._warm_up`). A fresh process pays once for what the model's warm-up
never reaches: the first cover read, the first import of the capture module
and Playwright, the first browser launch, each game's first captures (slower
the first time that game is played in the process, even in a new browser),
and the first world's chunks over the runner's shared memory, whose blocks grow
to fit them. Before the runtime binds the application, `load()` runs one
session with no client through the application's own commands and step calls:
for each of the seven games `set_paused`, `select_game`, `start`, then its
first chunk and its first 12-frame chunk through `process_input()`,
`generate()` and `process_output()`, as the step loop would; `send` and
`output` reach nobody yet. Only the first game builds its prompt bank (the
others would repeat that path for about 1.4 s each). The session then ends as
any session does (`@session_ended`: game and standby closed, keys released,
model reset, bank dropped), `load()` waits for every browser it launched to
exit, and the application's session fields are back to their initial values,
so the first real session starts from `select_game` like any later one. What a
finished session leaves in the process stays: the runner's grown shared-memory
blocks and the opening prompts' embeddings (all seven games'). The warm-up
takes about 11.6 s on a B200: container start to available went from 116-117 s
to 126-127 s. Served, two fresh processes each, every game played once per
session: without it, a fresh process's first session replied to its first
`select_game` in 1.73 s (1.47-1.50 s later), captured each game's first chunk
in 0.12-0.33 s (0.12-0.17 s later), took 0.34-0.57 s from `start` to the
world's first chunk (0.33-0.39 s later), and grew the shared-memory blocks 4
times; with it, the first session matches the second on every one (1.48-1.50 s,
0.12-0.17 s, 0.33-0.38 s, no growth). After load the process holds the
`/dev/shm` blocks a session would grow (40 MiB) and no browser. Draft frames,
metadata, events, HUD and generated frames are byte-identical with and
without it.
Served with a game rendering on the same GPU, chunks 1 to 6 measure 0.16 to
0.26 s and later chunks 0.21 to 0.27 s. Output is byte-identical to running
the DiT eagerly. A prompt change costs one chunk about 27 ms on a B200: 23 ms
to encode the text and 4 ms to swap it into the cross-attention caches. A world
keeps the text embeddings of the prompts it has encoded (up to 64, 4 MiB each),
so a chunk whose prompt was already used in that world pays only the swap. A
new world starts with none of those, but the process keeps the embeddings of
the opening prompts it has seen (up to 16, 4 MiB each), so a new world of a
game played before encodes nothing.

**Prompt bank.** A chunk's prompt is the scene text followed by the event
sentences its frames raised (`chunk_prompt`), so most chunks of a game change
the prompt. `edenlive_bank.json` lists, per game, the 128 event combinations
most often seen in long scripted runs; `select_game` (and `set_prompt` with new
text, and `set_source('game')`) encodes the scene text joined with each, 16 prompts per
encoder call (about 1.4 s for 128 on a B200, 512 MiB of device memory, one
game's bank at a time). A step looks its prompt up in the bank first, then in
the world's own reuse, and encodes only when both miss; each bank entry is
bit-identical to a live encode of the same string, so outputs are unchanged.
A rebuild during a world runs on its own thread and CUDA stream, one prompt at
a time, most frequent first, and only between steps, while the game captures
the next draft. Encoding alongside the steps would slow every one of them and
keep a step's own encode waiting behind the build's; between steps, an encode
that overlaps a step's start costs that step about 10-30 ms. The bank is dropped when
the source leaves `game` and at the session's end. The event space is
open-ended: each frame's sentence combines up to about ten clauses, and a
chunk carries the distinct sentences of its frames in order, so new
combinations keep appearing in long play and a bank cannot hold them all.
Built from half of a set of long scripted runs (random held keys), a 128-entry
bank removes 45-78% of the text encodes, by game, in the other half. Served on
a B200 with keys changing every 2.5 s, 10 worlds of `dust2`, `spiders` and
`freeze`: steady chunks that ran the encoder dropped from about 103 to 21 of
about 330 (`dust2` 3, `spiders` 18, `freeze` 0); the serial cycle (draft wait
plus `generate()`) moved p50 0.281 -> 0.281 s, p90 0.308 -> 0.293 s, p99
0.327 -> 0.316 s. Each chunk's `chunk timing` log line says whether it ran
the encoder (`text_encoded`). Opening a world (`EdenBackend.start`) then
takes about 7 ms, 30 ms with a new prompt: the pipeline's own path re-encoded
the prompt and ran a full `gc.collect()`, about 0.25 s in a loaded process. The
text encoder stays loaded either way. The process holds about 33 GiB of device
memory.

**Start to first frame.** Measured served on a B200 with the SDK client on the
same host, game mode, 7 worlds over 2 sessions and 3 games. Server side, from
`start` to the world's first chunk generated: 0.54-0.74 s when `start` comes
2 s after `select_game` or replaces a running world (the standby page is
ready; 1.6-2.0 s before it and the embedding path), and 1.2-1.6 s when
`start` follows `select_game` at once, which waits for the rest of the launch.
That chunk then reaches the client in 0.1-1.0 s; the same spread was measured
before, and it is not in this model's code. The standby browser holds about
50-120 MiB of device memory and 1.2 MiB of `/dev/shm` while it waits.

**Chunk timing in a live session.** Every 12 chunks the application logs
`chunk timing summary` (`edenlive_timing.py`): new frames per second, and the
median cycle split into `emit_s` (the previous chunk's `emit()`, which blocks
while the one-chunk playout queue is full), `draft_wait_s` (refused steps while
the source had no draft), `generate_s` (`generate()` as the runtime measures
it), and `capture_s` (the game's own capture time). A summary covers one
measurement run: a pause, `start`, `set_source`, and a new session each drop
the samples not yet summarized. Measured through the Python SDK on a B200,
game mode, with the game rendered on SwiftShader and each chunk captured while
the previous one generated:

| Game, input | cycle | new fps | capture | generate | emit |
|---|---|---|---|---|---|
| `freeze`, no keys | 0.55 s | 21 | 0.54 s | 0.22 s | 0 |
| `godzilla`, keys every 0.4 s | 0.52 s | 23 | 0.51 s | 0.25 s | 0 |
| `dust2`, keys every 0.4 s | 0.58-0.63 s | 19-21 | 0.57-0.62 s | 0.25 s | 0 |
| `dust2`, keys every 33 ms (the demo's rate) | 0.63-0.67 s | 18-19 | 0.62-0.65 s | 0.25 s | 0 |
| same, game rendered at 832x458 (`game_capture`) | 0.50-0.52 s | 23-24 | 0.35-0.40 s | 0.25 s | 0.08-0.13 s |

On SwiftShader (the table above), the capture is the bottleneck: it rasterizes each
frame on about 9 CPU cores, and the read-back dominates. A chunk plays in
0.5 s at the pinned 24 fps, so the playout queue drains before the next chunk
and the last frame repeats for 0.05-0.15 s at every chunk boundary. Capture
time scales with pixels, and the model conditions on the draft at 832x480, so
rendering the game at 832x458 (the draft's aspect) and scaling the draft to
1280x704 fits the cycle in 0.5 s; an A/B on `dust2` showed the output following
the draft as well as at full size. That is the software fallback
(`game_capture: [832, 458]`); the default renders on the GPU at full size (see
Deployment). Game events change the prompt on almost every chunk, which
costs about 27 ms of `generate()` for the re-encode and the cache swap; with
scripted keys over 48 chunks of `dust2` and `spiders`, 18 and 20 of the 47
changes went back to a prompt already used in the world and skip the encode. The
runtime paces an unpinned model from `generate()` alone, which here is not the
chunk period, so unpinning would play each chunk at about 50 fps and stall
longer.

**Measured choices.** FA4 + `torch.compile` + CUDA graphs is the fastest
profile (0.21 s per 12-frame chunk, about 57 FPS of model throughput against
24 fps playback). Inductor `max_autotune` was slower (0.227 s) and is not used.
The game renders on the model's GPU through headless Chromium (ANGLE over
NVIDIA EGL): the image installs the GLVND loaders (`libegl1`, `libgles2`), the
container asks for the `graphics` driver capability, and `EDEN_GAME_GPU=1`. A
12-frame chunk then captures in 0.20-0.24 s as JPEG at the full 1280x704, against
0.49-0.67 s on SwiftShader, which is CPU-bound and slows down under contention.
Without the loaders or the capability, Chromium falls back to SwiftShader on
its own; the renderer logs which one it chose (`Game2V capture WebGL
renderer`), and `game_capture: [832, 458]` keeps a software capture faster
than real time. Client mode does not pay any capture cost.

**Raw game frames (`capture_format: raw`, the default).** The game's draft
frames are the canvas pixels exactly: read back with `readPixels`, handed to
this process through an origin-private file in shared memory, and flipped to
top-down RGB here, with no image codec on either side. They replace the JPEG
quality-0.8 frames the capture used to deliver (`capture_format: jpeg` brings
those back) as the model's conditioning and as `draft_video`. Generated
output conditioned on raw drafts was compared side by side with output
conditioned on JPEG drafts, on two games, against a rerun of the same input as
the noise reference: the differences are pixel-level only, with no visible
change in what the model does. On a B200 a 12-frame chunk captures in about
0.11 s raw against 0.20 s as JPEG, where most of the JPEG time was the encode
and its blocking readback in the page and the decode here.

**Sparse draft video (`draft_video: sparse`, the default).** The model
conditions only on every fourth draft frame (0, 4 and 8 of both the 9-frame
and the 12-frame chunk). With `sparse`, only those frames are rendered and
read back from the page. Every frame still drains the key mailbox and
advances the game to its tick, exactly as with `full`; the frames between run
the game logic only, with no three.js render. The condition frames and every
frame's HUD state are byte-identical to `full` because a shipped game's logic
never reads back from its render and its scene draws from the game state
alone. That is the precondition for `sparse`: a game whose logic reads its
render (picking, render-target feedback) must use `full`. In place of the
frames between, `draft_video` repeats the last read-back frame (the
same array, so the track looks like a 6 Hz video), while each frame keeps its
own metadata (`frame_id`, `simulation_tick`, `keys`). The track stays
declared, since every emit carries a payload for each declared track.
`main_video` does not change. On one B200, served, a 12-frame chunk captures
in about 0.04 s sparse against about 0.12 s raw with `full`. Set
`draft_video: full` for a game whose logic reads its render, or when the
client shows the draft video at 24 fps.

**Capture on demand, for input latency.** Each game chunk is captured when
its step asks for it, not while the previous chunk generates. A key accepted
between steps then reaches the very next chunk, instead of the one after an
already-captured chunk. On the GPU a chunk captures in about 0.2 s and
generates in about 0.26-0.31 s, so the serial cycle still fits 24 fps (23.5-24
new fps measured on `dust2` with keys every 33 ms). Measured from `set_keys`
to the first received frame carrying the key, on the host itself (a remote
browser adds its network hop and jitter buffer): 0.69-0.86 s on demand,
against 1.24-1.32 s when the next chunk was captured ahead. The weights bundle holds the
authorized EdenLive source tree (`source/`, games included) and the HF
checkpoints (`weights/EdenLive`); `edenlive.yaml` names both. The default
profile (FA4, `torch.compile`, CUDA graphs) measured about 57.6 FPS of model
throughput on a B200 against 24 fps playback; the first chunk pays for
compilation.
