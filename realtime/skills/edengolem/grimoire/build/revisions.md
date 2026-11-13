# Revising a passed game

Outcome: a verified correction with current implementation notes and intact historical evidence.
Use for a user-requested change to a passed game; unfinished stages use their existing refinement loop.

## Workflow

A `complete` packet means the previous game passed; a new change still enters this workflow.
Before editing, copy the affected run to a distinct name under its stage directory and append
one revision record using the fields below. Keep prior stage records unchanged. Set
`parameter_source` to the revised run's actual rules or parameter file and name the revised run
in its evidence note. Query the packet again to validate the active record and obtain its
revision references. Revision checks and assessments use the manual record workflow; `finish.py`
only closes unfinished stages and rejects active revisions.

Inspect the request, latest artifact, current parameter/rules source and affected evidence first.
Retain the existing output root and [standalone layout](standalone-project.md). Keep the revised
run under the applicable stage directory with a distinct name/version; retain earlier artifacts.
A user-requested correction starts a new bounded revision; it does not erase completed stage
history or silently keep its old evidence as proof of the changed game. Preserve prior artifacts
and append a `revisions` entry in `.state.json`. Existing state files without this optional field
remain valid. Only one revision is active at a time; `python <skill-root>/forge/next.py <output>/.state.json --task-packet` reports it even if
`current_stage` is done. Use the shared [bounded refinement](../review/gate-recovery.md#bounded-refinement)
rules for its correction budget and next action.

Keep the existing game's assets and local state owners. Read factory references when the revision
changes their integration. Recheck affected scene presentation, behavior and final acceptance on
the revised artifact; a running game need not become static again to review a visual revision.

Required fields: id, request, affected_stages, status (in_progress/passed), loop_count, evidence,
parameter_source and regressions. Evidence is nonempty before passed. Complete the existing
[assessment record](../review/gate-recovery.md#manual-records) before marking a revision passed.
Retain historical evidence; missing assessment summaries must be backed by actual evidence/review
before a passed record validates. Do not invent reviews to fill them.
Use `python <skill-root>/forge/next.py <output>/.state.json --task-packet` to validate the record and report the next action in one call.

| Change | Relevant regression |
| --- | --- |
| Input, selection, placement | Accept/reject, bounds, repeated/held input, cancel/release, focus and committed result |
| Timing or automatic motion | Early/on-time/late, event ordering, pause/reset and cue agreement; omit when untimed |
| Rules, turns, resources | Win/loss, exhausted or stranded progress, same-tick precedence and replay |
| Camera | Normal/boundary scene views, projection and control mapping |
| UI presentation | Inspect changed states at the desktop play viewport; real-input checks for changed focus/actions/hit areas; [acceptance](step3-acceptance.md) for outcome/restart changes |
| Scene assets or animation | Identity/readability, fit to mechanical space, state-driven poses and effects, restart and disposal |
| Space, board or autonomous agents | Occupancy/collision where relevant, valid operations, reachability or progress through the affected layout |
| Mixed modes | Entry/exit, ownership of input/state, held-input leakage and transitions to terminal/restart |

A request spanning several rows needs their combined regressions. Use the matching
[conditional pattern](../patterns/_index.md) for ownership and troubleshooting.

### Current parameters and original intent

When systems share state or the change involves tunable behavior, update the existing
[systems, knobs and composition notes](prototype-design.md). Record which owner or parameter
changed, why the new choice fits the request, and which dependent behavior needs rechecking.

Keep the original brief as the initial design. Choose one JSON parameter file consumed by the
runtime when there are shared tunable values; include only parameters the game actually uses.
For a revision without tunables, name its authoritative rules source in `parameter_source` and
skip parameter generation. Mark historical brief values as original intent, not current facts. Generate a current
section in the game's implementation notes from that same file:

```sh
python <skill-root>/forge/sync_parameters.py <runtime-parameters.json> <implementation-notes.md>
python <skill-root>/forge/sync_parameters.py <runtime-parameters.json> <implementation-notes.md> --check
```

The tool inserts/replaces its marked section and SHA256; surrounding prose stays unchanged.
A stale section fails --check. This proves document/source synchronization, not that the runtime
actually imports the file: verify that integration and inspect observed values. Avoid editable
copies of tuning constants in strategy, brief and code. Strategy conditions should read observed
state such as cue/ready instead of reproducing the game's timer or capacity.

Keep each game's notes describing its own runtime parameters, system ownership and composition.
Balance numbers and camera preferences belong to that game; no value here is a library-wide
requirement, and a name reused across games can mean something different in each.

## Verify and record

For behavior changes run affected Stage 2 input and closure checks and the Stage 3 acceptance check on
the changed artifact, not a full rebuild of unchanged snapshots. A changed strategy, key binding,
condition or click target invalidates evidence bound to the previous one. Check parameter synchronization when used. Cite actual
commands, measured before/after values when available and screenshot states. Never claim a
change is verified because a prior version passed. Update the revision's evidence and assessments,
validate state, and apply the [pre-ship checklist](../review/pre-ship.md#pre-ship-checklist) before final handoff.
