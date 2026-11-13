# Stage 3: Final presentation and full acceptance

Outcome: a finished game with complete presentation, win/loss/restart proven through real input,
and a separately verified relocated standalone launch.
Use after behavior passes; preserve the existing scene, asset instances, input owners and the
outcome/restart logic that stage already proved.

## Workflow

1. Compose UI appropriate to the game: HUD, menus, board readouts, battle choices, outcomes and any
   requested pause controls. Retain functional controls and feedback from Stage 2; UI reflects state
   and calls existing actions. Read the
   [delivery presentation](../../assets/catalogs/ui.md#delivery-presentation) section of the UI
   catalog, the [decision feedback](../../assets/catalogs/ui.md#decision-feedback) entries whose
   presentation this stage finishes, and the
   [presentation API](../../docs/api/game-ui-factory.md#delivery-presentation);
   the scene shell and play surface are already built. Use the selected API only when helpful:
   reuse `meter` for bounded resources, `banner` for a phase or intro title,
   `tracker` for counted goals, and `dialog` for pause/result instead of a one-off overlay.
   Layout for a desktop browser window (the acceptance checker uses 960x640 CSS pixels at
   `device_scale_factor=2`). Keep the WebGL play canvas CSS box equal to `#app`; the drawing
   buffer may scale with pixel ratio. Do not add a phone
   breakpoint, a touch overlay, a gamepad mapping or invented mobile controls. Do not
   add audio, music, sound-effect or haptic assets, or a level-select of independent stages;
   in-round waves or a boss after clearing enemies may still appear. Mark custom integrated roots with
   `data-game-ui`; `.gbui` and `#hud` are also detected.
   Explicit `ui.mode: none` omits added interface design, but still needs observable outcomes and
   an operable restart. Do not skip acceptance.
2. Repair what the finished presentation changed. Replacing a result surface, adding a pause dialog
   or moving focus can break outcome handling that already worked: `RoundOutcomeOverlay.hide()`
   also resumes simulation time, and `GameUiFactory` does not pause simulation, clear held keys or
   manage pointer lock (see its [migration notes](../../docs/api/game-ui-factory.md)). Keep one
   owner for state and input. Check that the terminal still stops play, that restart runs exactly
   once per request, that keyboard input cannot reach a backgrounded round, and that focus and click
   targets still reach the controls the strategy drives. Do not reimplement outcomes or restart
   here; fix the integration.
3. Extend the inherited `strategy.json` from the behaviour closure shape to the full acceptance
   sequence `WON -> LOST -> WON`: a win, a deliberate loss, a real same-page restart and a second
   win. The behaviour stage authored a two-scenario `<WON|LOST> -> RUNNING` closure draft in the
   same format, and it travels with the run, so preserve its useful rules while adding the missing
   terminal branch and replay win. Do not submit the closure draft unchanged. Existing strategies are
   retained. Run the packet's `preflight` (or
   `python <skill-root>/forge/check_step3.py --validate-only --project-root <output>
   --strategy <project-relative-strategy>`) before the formal
   batch; this checks syntax without a browser, evidence directory or attempt. See
   [strategy authoring](../review/playthrough-contract.md#strategy-authoring) for rules and initialization.
   Keep each scenario within the brief's validation allowance; untimed games need no gameplay timer.
   If the final UI changed keys, click targets or focus, update the strategy to the real controls
   rather than reverting the interface.
   Unsupported automation follows the equivalent manual/input-only route in
   [gate recovery](../review/gate-recovery.md#manual-records), with evidence of every required outcome.
4. Finish edits before the formal batch. Use the optional
   `python <skill-root>/forge/check_rounds.py` diagnostic for
   unresolved rules or `python <skill-root>/forge/ui.py` fixtures for isolated display questions. The latter uses
   `forge/check_ui.py` helper APIs, which have no standalone CLI. Neither diagnostic is a required
   parallel harness or a substitute for actual play.

## Verify and record

Run the packet's `closeout.check`, which includes
`--strategy <project-relative-strategy>` for acceptance.
The acceptance checker verifies the full real-input sequence and integrated UI bounds/focus in the
same session at the desktop play viewport. A behaviour closure pass covered a smaller scope and
never substitutes for it: the final UI can change focus, input delivery and terminal handling, so
the whole sequence runs again here.
The separate relocation check restores pinned dependencies
and launches the copied `serve_step.py` through `launch.sh` from another working directory.
Inspect scene, action feedback and running/terminal UI images. Layout bounds alone do not establish
readability, correct labels or aesthetic quality. Record experience and performance separately,
then submit the printed review. `step3` is the final artifact; deliver its launch path, controls,
evidence and limitations using the [pre-ship checklist](../review/pre-ship.md).

## Troubleshooting

| Symptom | Likely cause | Remedy and recheck |
| --- | --- | --- |
| Play resumes by itself after the new result surface appears | The overlay's `hide()` resumed simulation, or the outcome owner no longer halts updates | Keep pause/terminal policy in the game's own owner and let UI only reflect it; recheck that the terminal observation stays stable with the final UI mounted. |
| One restart request starts two rounds | Both the retained control and the new UI button call restart | Give restart one owner and have the UI dispatch to it; recheck that one input changes `roundId` exactly once. |
| Keys still drive the game behind a pause or result dialog | Held keys were never cleared, or the dialog does not own input focus | Clear held input at the transition and route focus to the dialog; recheck press, hold and release while it is open. |
| The strategy passed at closure but fails here | The final UI moved focus or click targets the rules depend on | Update the strategy to the real controls, not the game's rules; inspect `unresolvedRules` and the input trace for the first divergence. |
| The finished UI adds a stage picker, sound toggle or touch overlay | Delivery presentation invented campaign, audio or mobile controls | Keep pause/result/title cards; drop independent-stage pickers, audio/haptic controls and touch layouts. Recheck that restart still owns the only round. |
