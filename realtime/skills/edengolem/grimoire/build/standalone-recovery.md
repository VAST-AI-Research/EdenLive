# Standalone setup recovery

Outcome: repair packaging, manually merge an existing layout or diagnose a relocated project.
Read only when [normal setup](standalone-project.md) cannot complete, a preview service needs
management, or delivery requests fail. Preserve the user's output directory and unrelated files.

## Manual initialization

For an existing layout the scaffold cannot merge, perform the same copy manually: runtime files
under `assets/`, runs under `runs/stepX/<name>/`, `private: true`/`type: "module"` manifest with
`npm install --save-exact three@0.161.0` (optional Rapier 0.14.0), copied `serve_step.py` and executable
`launch.sh`. Keep the template's local `/vendor/` import map and project-owned lockfile. Do not copy
the skill's development manifest, `node_modules/` or symlinks back to the skill. After an interrupted
npm install, retain the generated files and rerun installation in that output root.

## Manual stage transition

Use only after a manually recorded pass or when resuming a transition. Normal `finish.py` does this:

```sh
python <skill-root>/forge/scaffold.py advance --project-root /path/to/game --name behavior --from-stage scene
```

Use `--from-stage behavior --name acceptance` for the next transition. Every game, including an
explicit no-UI brief, retains all three stages. The command validates stage order,
copies implementation to the next stage, adjusts run metadata and records source hashes/evidence
in `source-artifact.json`. It excludes the source `evidence/` directory and leaves the next stage
in progress with no pass. Review any additional reports kept outside `evidence/` as historical
material. Started destinations and symlinked runs require explicit handling; they are preserved.
Interface and board games use their actual static surfaces in scene; no scene-skip path is needed.

## Background service ownership

For a background preview, keep service output out of the controlling terminal:

```sh
python <skill-root>/forge/run_project.py --project-root /path/to/game --stage step3 \
  --log /path/to/game/evidence/server.log --pid-file /path/to/game/evidence/server.pid \
  > /path/to/game/evidence/runner.log 2>&1 &
game_runner_pid=$!
# When this preview is finished:
kill "$game_runner_pid"
wait "$game_runner_pid"
```

The PID file holds the owned server PID after readiness; the shell variable holds its supervising
runner. Existing PID files are preserved. Stop the recorded runner, not whichever process occupies
a port. A terminated runner cleans its server and PID file; SIGTERM exits with status 130.

## Relocation diagnostics

Acceptance closeout already runs relocation; invoke it again only for a packaging change or failure:

```sh
python <skill-root>/forge/run_project.py --project-root /path/to/game --stage step3 --only final \
  --relocate --output runs/step3/final/evidence/relocation.json
```

One invocation copies into a new path with spaces, excludes installed packages, runs bounded
`npm ci --ignore-scripts`, launches the copied project, rejects external/missing runtime resources,
and executes the declared input cases through the first signature success. Earlier cases in that
prefix retain prerequisites. `--input-check <case-name>` selects another endpoint. Observations
must prove actual gameplay change, not a key log. Set `--width`/`--height` to the cases' viewport
(runner default 960 x 640; behavior default 960 x 600) when using screen coordinates.

The report is saved under the original project even after temporary-copy cleanup. Passing a new
destination to `--relocate /new/path` retains that explicit copy; `--keep-relocated` retains an
automatic copy. Existing destinations are never overwritten. Games with unsupported inputs need
equivalent input-only/manual evidence against the relocated copy; the unavailable automated
portion remains `not_evaluated`. Use the retained copy for such inspection. No check here repeats
or substitutes the full win/loss/replay sequence. A failed optional tool does not justify removing
a defining input.

## Troubleshooting

| Symptom | Likely cause | Remedy and recheck |
| --- | --- | --- |
| Scaffold refuses a conflict | Existing file/package settings differ from the proposed copy | Inspect the conflict, merge deliberately or use the manual setup; rerun schema/startup checks. |
| Copier reports a computed reference | Static scanning cannot resolve a loader or URL | Supply actual local files with `--extra` and a resolution note, or adapt the loader manually; check runtime requests. |
| `/assets/` request fails after moving the project | A transitive dependency was omitted or points back to the skill | Copy the missing runtime dependency at its expected project path; recheck from the relocated copy. |
| `/vendor/` redirects to a CDN or rules cannot import a package | Dependencies were installed in the skill checkout or the lockfile is incomplete | Install the pinned dependency in the output root; restore with `npm ci` and confirm local responses. |
| A gate refuses an external run or checks the skill's assets | `--project-root` was omitted or points to the wrong directory | Set it to the output root and rerun with explicit run/evidence paths. |
| The launcher opens the wrong stage or an empty list | The stage has no `<name>/index.html`, or the wrong stage was selected | Check the stage artifact path and launch that step; the default selects the latest populated step1/step2/step3. |
