# ADR-0003: LTX integration through its unchanged CLI

Status: Accepted

## Context
`E:\Dev\ltx25-igpu-streaming` is an independent Git repository with its own venv and weights
(`E:\AI-Data\LTX25`). It holds protected untracked perfume files (`episodes/perfume-commercial-01/`,
`prompts/misty-forest-sunrise.txt`, `prompts/perfume_pushin.txt`). `render.py` writes outputs inside the repo
(`videos/`, `latents/`, `logs/`, `previews/`, all git-ignored) and builds the child environment from
`os.environ`.

## Decision
The studio runs the LTX venv's `python.exe render.py ...` as a subprocess and never modifies the LTX
repository. Execution-only settings (`SYNC_S1`, TEMP/HF redirects) are passed through the environment.
Outputs go to a reserved namespace (`--category studio`, names prefixed `studio-`, unique per attempt via
`--prefix`). Prompt and start-image files live in studio-owned folders and are passed as absolute paths.
A path guard allows only `videos/studio/**`, `latents/studio-*`, `logs/studio-*`, `previews/studio-*` and
denies `episodes/**`, `prompts/**` and anything tracked by Git. `shot_run.py`, `episode_run*.py` and
`assemble_*.py` are never invoked. The LTX commit is recorded in every run.

## Consequences
- LTX stays independently runnable and protected files are untouched.
- Progress is obtained by tailing logs; contract tests guard against CLI changes.
- Features requiring LTX changes (e.g. `--out-dir`, JSON progress) need a separate, explicit decision.
