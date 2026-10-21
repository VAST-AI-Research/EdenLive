# Agent instructions for eden-live

Read [`model_behaviour.md`](./model_behaviour.md) before changing this folder,
and keep its "Maintaining this behaviour" invariants. The ones most often
broken:

- The game and the client's `source_video` track are two draft sources behind
  one protocol (`edenlive_draft.py`). Nothing past `take()` branches on the
  source, and the model half never learns it.
- `set_source` switches sources inside a world; only `start` opens a new one.
- A refusal raises `CommandError` with a code the command's description names.

A change to a command, a code, a track, or the draft path updates
`model_behaviour.md`, `PORTING.md`, and the `reactor.yaml` version log in the
same change. The repository-wide rules are in the root `AGENTS.md`.
