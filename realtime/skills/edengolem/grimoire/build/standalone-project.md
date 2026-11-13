# Deliver a standalone project

Outcome: the user's output directory contains the runnable game, stage artifacts and evidence.
Read at intake/setup; return only for runtime copying or launch. Use
[recovery](standalone-recovery.md) for conflicts, manual merging, service cleanup and relocation diagnosis.

## Select the output root

Use the user's explicit path, resolving a relative path from the agent's starting directory.
Without a path, use that starting directory itself; capture it before inspecting the skill.
Keep the brief, state and generated files there. Resume the existing project unless asked to move.
Read tools/references from the skill; copy runtime files into the output. Inspect existing files,
merge compatible settings and preserve conflicts for deliberate resolution.

## Initialize the runtime files

Initialize only after the brief is in scope and schema-valid. Run from the skill checkout, or use
the tool's absolute path; generated projects have no `forge/`.

```sh
python <skill-root>/forge/scaffold.py init --project-root /path/to/game --name scene --brief /path/to/brief.json
```

`init` validates the brief, creates `runs/step1/scene/` with HTML, browser wiring, `game.mjs`,
`game-loop.mjs`, draft metadata and a manifest row for each theme element, and prepares `runs/step2`
and `runs/step3` for every game. It
copies shell/time foundations, server and launcher; installs pinned Three.js 0.161.0; and retains
project manifest/lockfile, brief, state and receipts. Add `--rapier` only when needed (0.14.0).
`--lock-only` defers installation; run `npm ci` before launch. Conflicts fail before copying.

The skeleton has no invented gameplay or passing signature checks. `init` sets intake
`in_progress` with schema-only evidence; it does not pass intake. Complete intake's
[closeout](../review/gates.md#stage-closeout), then compose the static scene in `main.js`. In
behavior, implement the rules, progression, outcomes and restart in `game.mjs` and wire browser
input; acceptance completes presentation and repairs what it changes. Select the clock with the
[input contract](../review/input-contract.md#runtime-preflight-and-clock).
Live closeout argv for the current stage comes from
`python <skill-root>/forge/next.py /path/to/game/.state.json --task-packet`.

## Copy selected runtime dependencies

```sh
python <skill-root>/forge/scaffold.py assets --project-root /path/to/game --asset assets/math/RandomUtils.js
```

Repeat `--asset` for entry files. The copier follows literal imports, fetches, workers, `new URL`
and CSS resources, preserving paths and accompanying top-level notices; receipts retain hashes.
For computed references, add the actual files with `--extra` and explain them with `--dynamic-note`.
Inspect required package notices/data; install newly needed pinned packages in the output first.
Use the [asset router](../../assets/summary.md) for current-stage resources and selected APIs for
runtime copy lists. No catalog or whole-library copy is needed. Unsupported packages/loaders and
manual merges use [recovery](standalone-recovery.md#manual-initialization).

## Retain stage artifacts

Normal [closeout](../review/gates.md#stage-closeout) records the reviewed pass and prepares the next
run under `runs/stepX/<name>/`, preserving source implementation/evidence and adding provenance.
Step 2 receives behavior-card drafts and a closure strategy draft; Step 3 inherits that authored
strategy and extends it to the full sequence. Either stage receives a draft only when no strategy
exists; an authored file is carried forward and never overwritten, and only an untouched draft is
redrawn for the new scope. Complete
the blank fields before checking; serving and task-packet queries remain available while editing.
The new run receives no inherited pass; closeout may reuse matching individual checks. Keep HTML,
JS, metadata, rules and needed notes/parameters/strategy in that run, with reports in `evidence/`.
Add artifacts only as needed. Use `brief.json` and `.state.json` in the output root; state paths are
output-relative and brief paths in run cards are run-relative (for example `../../../brief.json`).
Keep shared assets stable or version changed behavior so historical runs remain reproducible.
For a manual transition, use [recovery](standalone-recovery.md#manual-stage-transition).

## Launch and check the actual output

From the output root:

```sh
./launch.sh step1                # intermediate stage
./launch.sh                      # latest populated step1/step2/step3
./launch.sh step3 --port 9000
```

The launcher changes to its own directory, so `/path/to/game/launch.sh step3` works from elsewhere.
Open its printed URL and select the run. Serving requires the copied `serve_step.py`, local npm
runtime dependencies, Python 3.10+ and a browser; it has no skill-installation dependency.
`npm ci` restores locked dependencies after moving the project.

Authoring gates stay in the skill checkout. Use their absolute paths or run from that checkout,
passing `--project-root <output>`; run, report and strategy CLI paths resolve under that root and
linked brief paths from the run. Evidence and symlink targets must remain inside the project.
Prefer the current task packet for gate argv. For a bounded launch diagnostic,
`python <skill-root>/forge/run_project.py --project-root <output> --stage step3
--only <name> --check --output <project-relative-report.json>` manages an owned server and free port.
See [recovery](standalone-recovery.md#background-service-ownership) only for background previews.

## Verify and record

Acceptance closeout includes independent relocation: a copied path with spaces, locked reinstall,
local resource checks and actual meaningful input. Retain that report; it does not replace the
full playthrough. Repeat relocation during a revision only when packaging changes or a diagnostic
requires it; details and manual-input
alternatives are in [relocation diagnostics](standalone-recovery.md#relocation-diagnostics).
At handoff, use [pre-ship](../review/pre-ship.md) and include output path, reviewed run, launch command,
controls, evidence and remaining limitations.
