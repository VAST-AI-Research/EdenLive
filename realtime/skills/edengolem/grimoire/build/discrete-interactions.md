# Discrete and stateful interaction

Outcome: a legal operation commits once, with invalid actions refused and the result visible.
Use for accepted operations, selections, transactions or mode changes. Classify by transition,
not by avatar/cursor labels.

## Workflow

Read only matching APIs in the [interaction catalog](../../assets/catalogs/interaction.md)
and [rules catalog](../../assets/catalogs/gameplay.md).

| Required operation | Asset candidates | Useful boundary case |
| --- | --- | --- |
| Cell movement | GridTokenMotionController; GridPathPlanner for topology | Blocked/reversed/queued move according to the brief |
| Continuous snake turns | SnakeMotionController + SnakePlay | Turn queue, body/wall rule, growth; fatal contact must not become wall refusal |
| Placement | CursorPlacementController | Valid cell, occupied/outside/unaffordable cell, cancel if advertised |
| Falling pieces | FallingPieceController + BlockGridModel | Rotation against occupancy, locking, clearing and top-out |
| Menu choice | MenuSelectionController | Enabled/disabled choice, confirm effect, next turn/focus |
| Carry/process/serve | CarryStationController | Range, accepted ingredient/state, processing completion and serve event |
| Feed/mode/toggle | FeedToggleController or small mode state machine | Correct destination, held/repeated edge behavior and visible consequence |

Keep selection, preview, committed state and timed consequences distinct where they exist. An
invalid action must not spend resources or partially commit state. Cancel/undo is required only
when advertised; otherwise verify refusal or reset. Use meaningful observations, not log-row changes.

Grid topology does not decide time or reversal rules. A maze chase may move continuously between
cells; push-box movement waits for input; falling pieces advance automatically between operations.
Compose [continuous controls](continuous-controls.md) when needed instead of choosing one universal
grid controller. For menu play, Stage 2 includes a minimal actual decision consequence, not only a
moving highlight. For placement, later autonomous outcomes build on a real committed object.
For a loop that crosses interaction modes, apply [mixed-mode ownership](../patterns/mixed-modes.md).

## Verify and record

Exercise a committed success and the applicable boundary from the table using the
[input contract](../review/input-contract.md). Inspect both state and feedback; record the transition,
repeat policy and observed result in the active stage's evidence. If only a highlight changes or a
held key commits repeatedly, use [input diagnostics](../review/input-contract.md#troubleshooting).
