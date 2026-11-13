# Systems, knobs and composition

Outcome: a small implementation note identifies the systems the game needs, their ownership and
the parameters that can meaningfully change play. Use when several systems share state or a
prototype has real tuning choices. A simple interaction can use a short paragraph; omit the knobs
table when no values need tuning. Do not invent resources, modes or settings to fill this outline.

## Workflow

Inspect the brief, current artifact and selected APIs first. Add these sections to the game's
existing implementation notes; no new schema or separate planning artifact is required.

### Systems

List only the systems required for the bounded core loop. For each, name the behavior it owns,
its relevant state and an observable success/boundary. Separate rules from input and presentation
where they have different responsibilities. A board may need occupancy and turn resolution; a
service game may need carry/process/serve; neither inherits combat systems from an example.

### Knobs

Use actual runtime parameter keys. For each tunable, record its effect, unit, constraints or
tradeoffs, the reason for the starting choice, and what must be rechecked. A useful table is:

| Parameter | Effect and constraints | Starting choice | Recheck |
| --- | --- | --- | --- |
| Actual runtime key, with unit | What changes for the player; dependencies and valid range | Brief requirement or a stated assumption; link current values | Observable success/boundary and affected cues or outcomes |

Preserve explicit brief values unless the requested revision authorizes changing them.
For an unspecified value, choose from the actual geometry, interaction cadence or display needs,
record the assumption and validate it. Avoid universal balance numbers. A camera distance can be
a presentation knob; a mode selector or unused constructor option is not automatically a tuning axis.

Keep current values in one runtime-consumed JSON source when tunables are shared. Generate the
current-values block in the notes with [parameter synchronization](revisions.md#current-parameters-and-original-intent).
Explain effects in prose without maintaining another editable copy of the values. If no tunables
exist, keep the authoritative rules source and skip parameter generation.

### Composition

Describe the actual flow: input -> accepted operation -> shared rules/state -> feedback. Name the
selected modules or custom adapters and who owns the relevant clock, resources, mode transitions
and reset. State which objects/views read the same data and which event causes the next system to
act. Two modules with similar counters must not become competing owners of one resource.

For example, placement commits occupancy and spends from one resource pool; spawned agents read
that committed board; their resolved outcomes update the round; preview/UI read those owners.
Include only the parts the game actually has. Use [mixed modes](../patterns/mixed-modes.md) when
ownership changes across phases and [restart](../patterns/loop-restart.md) for the reset boundary.

## Verify and record

Check that each named parameter is consumed by the runtime and that notes match the current source.
Synchronization proves matching text, not runtime integration. For a tuning change, record the
intended effect, observed result and affected regression evidence; preserve earlier values/evidence
as history. Recheck coupled behavior together, such as timing with cues or attack reach with camera
framing. Stable terminal outcomes and real-input replay remain required by the stage/revision gates.

For interacting systems, a systems table can clarify ownership and observable behavior; include a
knob table only for actual tunables. Describe update order or precedence only where it affects play.
A simple interaction needs only the short paragraph allowed above. Explain each parameter's effect
in this game rather than borrowing a different game's meaning.
