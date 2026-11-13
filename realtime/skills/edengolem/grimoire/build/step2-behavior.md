# Stage 2: Build the complete playable loop

Outcome: the retained scene is playable end to end -- real inputs drive the game, the brief's
progression runs, and a round can be won, lost, stopped and restarted.
Use after the static scene passes.

## Workflow

Use the retained static scene from Stage 1. Add only capabilities implied by the brief and recorded
in `step2.json`: input and controls, camera follow or aiming, actions/turns, navigation, animation,
collision and interaction resolution. Menu, board, puzzle and stationary games may need only
discrete input and local transitions; action games may need continuous movement and physics.
Never impose a genre checklist.

Catalog camera, combat and resource modules are candidates, not required systems. When the card
declares `continuous_motion`, `aiming`, `resources` or `autonomous_agents`, import one matching
module from [camera](../../assets/catalogs/camera.md), [gameplay](../../assets/catalogs/gameplay.md)
or [behavior](../../assets/catalogs/behavior.md), or record it in `implementation.catalog_skips`
with the path considered and why it was skipped. The behavior gate proves that import from the
run page's static import graph, not from the card's `modules` provenance list. Sibling modules
in the same family are not all required. Discrete menu and board games that never declare those
capabilities need no skip list.

Keep scene construction and authoritative state in their existing owners. Wire controllers to
existing factory actions and collision hints.

When the brief needs camera-relative WASD and a persistent third-person orbit (look input turns
the camera, releasing it leaves the orbit, optional lock-on), pair
[`OrbitFollowCameraRig`](../../assets/camera/OrbitFollowCameraRig.js) with
[`CameraRelativeCharacterMotionController`](../../assets/actor-motion/character/CameraRelativeCharacterMotionController.js).
The other catalog pairings do not cover that view:

| Brief needs | Pair | Why the nearby module is not it |
| --- | --- | --- |
| View turns with the actor | `PoseFollowCameraRig` + `HeadingRelativeCharacterMotionController` | Offset lives in the actor frame |
| Temporary peek that recenters | `LookOffsetCameraRig` | Yaw and pitch return when look input stops |
| Mouse look that turns the body | `MouseLookCharacterMotionController` + `FirstPersonCameraRig` or `PoseFollowCameraRig` | Look yaw is written into the actor |
| Fixed world azimuth | `PositionFollowCameraRig` + `WorldCardinalCharacterMotionController` | The player cannot orbit |

Tune distance, `anchorHeight`, planar look-at offset, pitch range and lock interpolation on the
rig. The look-at is a body-height aim point, not a rule that the actor sits in the middle of the
frame. The game owns lock target selection and actor facing (`faceTarget` on the motion
controller). Discrete menu and board games skip this pairing.

### Close the loop

This stage owns the whole playable cycle, not just individual verbs. Implement:

- the deterministic local rules that make actions observable (selecting a card, placing a tower,
  moving a piece, firing a shot),
- the brief's in-round progression between waves, rooms, phases or encounters (one continuous
  round; a boss after clearing enemies is fine, a level-select of independent stages is not),
- its win and loss conditions with their exact prerequisites and precedence, a stable
  `outcomeReason`, and exactly one terminal event per round,
- a terminal state that actually stops play, and a real same-page restart that begins a fresh round,
- the functional feedback a player needs to make those decisions (remaining health, resources,
  current objective, whose turn it is, timing cues).

Use [restart ownership](../patterns/loop-restart.md) for transient state and reset ownership, and the
[playthrough contract](../review/playthrough-contract.md) for the read-only observation adapter.
Both terminal branches must be implemented, even though closure evidence exercises only one.

Functional feedback is whatever the mechanic requires to be playable -- a number, a bar, a
highlighted cell, a visible cue. Do not generate audio, music, sound-effect or haptic assets;
keep timing and impact readable without sound or vibration. Do not add gamepad, joystick or
touch-layout controls; keyboard and mouse/pointer drive the loop. Final composition, styling
and result presentation belong to Stage 3; do not build the finished result card or decorative
menus here.

For that feedback, read only the
[decision feedback](../../assets/catalogs/ui.md#decision-feedback) section of the UI catalog, and the
[feedback API](../../docs/api/game-ui-factory.md#decision-feedback) if you
use the factory. Making the Stage 1 play surface operable uses its
[core play surface](../../assets/catalogs/ui.md#core-play-surface) entries. An observable outcome and
an operable restart are required, but a plain notice and a working restart control satisfy them; the
finished result card is Stage 3's, and it must not add a second restart owner.
Extend the existing scene's interface rather than composing a parallel one.

The copied `game-loop.mjs` supports `discrete` inputs or `fixed` updates; select the applicable mode
in `main.js`, supply the real input adapter and implement its owner in `game.mjs`. Direct DOM/pointer
handlers may call the owner without a keyboard adapter. See [clock and input wiring](../review/input-contract.md#runtime-preflight-and-clock).

Advancement pre-fills `implementation` (including an empty `catalog_skips` array), `capability_checks`, an input case and a signature row in
`step2.json`, plus a `strategy.json` closure draft. Complete or replace these drafts; blank rows
deliberately fail readiness. Existing authored values and an existing strategy are retained.
While editing, the packet's individual gate commands are diagnostics;
`python <skill-root>/forge/validate_run_meta.py <run>/step2.json` is the local schema check.
`--help` lists its flags.

Declare `input_checks` that exercise the meaningful capability prefix and assert a changed field in `window.__gameTest.read()`. Link each declared capability to existing checks through `capability_checks`, and defining brief actions through `signature_actions` (see the [input contract](../review/input-contract.md)). The behavior gate drives the real browser inputs. `python <skill-root>/forge/check_rounds.py` is an optional deterministic rules diagnostic when a shared rules harness is useful; do not build a second harness for simple behavior. A capability that is inapplicable is recorded as such with a reason; it is not faked.

### Prepare the closure strategy

Complete the `strategy.json` draft with input-only rules for two scenarios: one representative
terminal outcome (`WON` or `LOST`, whichever the brief reaches more directly), then `RUNNING` after
a real restart, proving the new round still accepts an effective input. Rules, actions, conditions
and observation fields are the same format Stage 3 uses, and the authored file travels with the run,
so acceptance extends it instead of starting over. See
[strategy authoring](../review/playthrough-contract.md#strategy-authoring).

Run the packet's `preflight` (or `python <skill-root>/forge/check_step3.py --validate-only --scope
closure --project-root <output> --strategy <project-relative-strategy>`) before the formal batch;
it checks syntax without a browser, evidence directory or attempt. Keep each scenario within the
brief's validation allowance; untimed games need no gameplay timer. Unsupported automation follows
the equivalent manual/input-only route in
[gate recovery](../review/gate-recovery.md#manual-records), with evidence of the required outcome.

Use the game's real rules and advertised inputs. Do not lower an enemy count, add a developer win
key or introduce a countdown to make a scenario reachable; if automation cannot drive it, record
that honestly and supply the equivalent manual evidence.

## Verify and record

Close with one formal `python <skill-root>/forge/finish.py --project-root <output> --check
--strategy <project-relative-strategy>` after edits, then an explicit experience/performance review.
Prefer the packet's `closeout` commands.

The batch runs two checks with different scopes. The behavior check proves declared capability
inputs and their persistent effects, and that locomotion, aiming, resources or autonomous agents
imported a catalog module (reachable from the run page) or recorded the skip. Listing a path in
`modules` is not that proof. The closure check replays the strategy for one terminal
outcome, a real same-page restart and a fresh round that still accepts input; its report names that
scope. The other terminal branch and the final-UI integration remain unevaluated here, so record
them as such rather than implying full acceptance.

The stage must retain a runnable artifact under `runs/step2/<name>/` and a source manifest pointing
to the static scene.

## Troubleshooting

| Symptom | Likely cause | Remedy and recheck |
| --- | --- | --- |
| The terminal outcome is reached but play continues | The terminal state is recorded without stopping simulation, input or scoring | Give the transition one owner that halts updates and clears pending input; recheck that the terminal observation stays stable and emits exactly one event. |
| Restart appears to work but the next round misbehaves | Round state, held keys or queued actions survived the reset | Reset through the single round owner in [restart ownership](../patterns/loop-restart.md); recheck the fresh round's ID, cleared events and an effective input. |
| The closure scenario cannot reach any terminal outcome | The strategy drives inputs the game does not advertise, or the win condition needs state the observation never exposes | Compare the rule's `when` fields against the real snapshot and inspect `unresolvedRules`; expose the real decision field instead of weakening the rule or the game. |
| Closure passes but the other terminal branch is missing | The narrower proof was mistaken for complete implementation | Implement and review both branches in this stage. Record the unexercised branch as unevaluated; acceptance will drive both. |
| A campaign request becomes a level-select menu | Independent stages were treated as extra UI or mixed modes | Check the agreed intake slice before replacing independent stages; resolve an explicit scope conflict through intake, then recheck restart within the agreed round. |
| Timing or impact feels incomplete without sound or vibration | Audio or haptics were added as feedback | Keep visible pose, text or geometry cues; generated audio and haptics are out of scope. Recheck the cue from the normal view. |
| A stick, touch overlay or phone layout appears | Non-keyboard/mouse input was treated as a UI extra | Map play to keyboard and mouse/pointer; drop gamepad, joystick, trackpad and touch surfaces. Recheck advertised controls against the input contract. |
| The behavior check asks for a catalog camera, combat or resource module | `continuous_motion`, `aiming`, `resources` or `autonomous_agents` was declared without a runtime import or `catalog_skips`, or `modules` listed a candidate the page never imports | Import one matching module from the run page, or record the considered path and why the game owns that rule instead. Do not treat `modules` as the import. |
| WASD walks in world axes, or the orbit snaps back when the mouse stops | `WorldCardinal` / `PoseFollow` / `LookOffset` / `MouseLookCharacter` was used for a camera-relative persistent orbit | Pair `OrbitFollowCameraRig` with `CameraRelativeCharacterMotionController`; pass the rig yaw into `planMovement({ cameraYaw })`. Recheck that releasing look leaves yaw and that forward follows the camera. |
