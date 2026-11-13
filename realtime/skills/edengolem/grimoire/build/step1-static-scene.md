# Stage 1: Build the complete static scene

Use after intake. This stage turns the validated brief into a complete, inspectable static composition with the asset factories. It is the fastest way to expose missing theme elements before behaviour work.

## Workflow

Read `brief.json`, especially `scene.theme_elements`, `interaction_tags`, `player_modality`, `win_state` and `fail_state`. For each theme element, choose an existing factory or a small primitive fallback and record the mapping in `scene-manifest.json`. A menu, HUD, board or grid still sits in the 3D modeled scene; do not add a 3D avatar or traversal when the brief does not call for one, and do not replace the scene with a DOM-only or canvas2d surface. Prefer a skill factory when one fits. Treat environment factories as candidates first: a custom 3D `setting` (arena, meadow, street, chamber) should consider `ArenaLayout`, `ArenaEnvironment`, `NaturalEnvironment`, `BoardEnvironment`, `RaceTrackEnvironment` or `GreyboxKitFactory` before stacking primitives. Record the considered catalog path and why it was skipped. Actors, hazards, goals and DOM/interface rows do not need that record. When a role has no factory and the game still needs 3D structure, build it from simple primitives (box, sphere, cylinder, capsule, plane, a few extruded slabs). Do not import GLTF/FBX/OBJ packs or author dense custom meshes; silhouette, color and scale should carry the role. See [greybox presentation](../patterns/greybox-kit.md) for that fallback.

Keep the geometry readable rather than ornamental. A catalog environment already supplies floor, stands or terrain; do not restack concentric decorative rings, over-tessellated cylinders or a field of unique props around it. Instancing or merging meshes is not a substitute for that judgment. Experience review owns complexity; the gate does not count meshes.

Use the [asset router](../../assets/summary.md) and only the selected factory API references. Reuse factory modules and resolved parameters. Add an asset only when the brief needs a representation no existing factory can provide or a factory has a verified bug. Freeze factory animation and simulation in this stage. Create collision and interaction placeholders as metadata or disabled proxies when later behaviour will need them.

### Compose the intended presentation

Use the run created by [standalone setup](standalone-project.md): `runs/step1/<name>/` with `index.html`, `main.js`, scene modules and `step1.json`. The scene must include every required role that can be seen or represented: player/opponent, board or world, hazards, resources, goals and interface anchors. Set an intentional camera/framing for the static composition and expose `window.__scene = {scene, camera}` for the Three.js scene. HUD, menus and boards overlay or sit in that 3D scene; do not replace it with a DOM-only or canvas2d play surface. No gameplay adapter or input checks are required in this stage.

Keep one authoritative owner for scene objects and parameters. Save resolved factory options in `scene-manifest.json`; later stages import or extend this scene instead of rebuilding it.

When the brief's play happens on an interface -- a board, battle menu, monitoring view or placement
area -- lay out and bind that surface here, and read only the
[infrastructure](../../assets/catalogs/ui.md#infrastructure) and
[core play surface](../../assets/catalogs/ui.md#core-play-surface) sections of the UI catalog.
Live values, result cards and decorative menus are not this stage's work: leave feedback to Stage 2
and final presentation to Stage 3. Do not add audio, haptic, gamepad or touch-layout assets, a
DOM/canvas2d play surface, or a level-select of independent stages; in-round rooms or a boss
arena after the first encounter may still be composed as one connected 3D space. A game whose
scene is purely 3D may need no UI entry at all.

The scaffold pre-fills `scene-manifest.json` with each inventory ID. Complete its blank bindings
and assets; use `custom_reason` instead of an asset for DOM or custom representations.
A custom 3D `setting` also names `considered` (a catalog environment path, or a list of them).
`step1.json.scene_manifest` names this file; card fields follow the [run schema](../../docs/specs/run-meta.schema.json). Each inventory ID has one binding and
names its copied asset or explains a custom representation. For example:

```json
{"elements":[
  {"id":"player","binding":{"object":"hero"},"asset":"assets/world/object/factory/CharacterVisualFactory.js","options":{"preset":"warrior"}},
  {"id":"arena","binding":{"object":"floor"},"custom_reason":"A meadow with one landmark, not a catalog arena.","considered":"assets/world/environment/ArenaLayout.js"},
  {"id":"commands","binding":{"selector":"#commands"},"custom_reason":"Static battle menu"}
]}
```

Give the Three.js root its bound `name`. Repeated or later-spawned subjects can use representative
instances, shown at a fixed pose; do not show every future spawn simultaneously. Keep the original
counts and gameplay intent in scene data for later behavior.

Keep the play canvas inside `#app`. The template pins `#app canvas { width:100%; height:100% }`.
`installResize` from [PrototypeShell.js](../../assets/user-interface/PrototypeShell.js) calls
`setSize(cssWidth, cssHeight, false)` and observes the container as well as the window, so the
drawing buffer can follow `devicePixelRatio` without inflating the CSS box, including when `#app`
changes size without a window resize. Do not call `setSize` with `updateStyle` true, and do not
leave the canvas unstyled. Framing does not require the player to be centered.

## Verify and record

Run the static-scene gate through [stage closeout](../review/gates.md#stage-closeout); the current
task packet supplies live paths and argv. It validates metadata, local boot, camera/framing, a WebGL
play canvas whose CSS box matches `#app` at `device_scale_factor=2`, resource loading, a manifest that
covers the brief's theme elements, and a considered catalog path on each custom 3D `setting`. Inspect
the generated screenshot for readability, missing roles and ornamental stacking. The gate proves
static completeness, canvas fit and catalog-first records; it does not count meshes, require a
centered player, or claim movement, rules or win/loss behaviour. Record functional, experience and
performance assessments at closeout. Composition and geometric simplicity stay experience review.
