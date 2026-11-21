# CLAUDE.md: AI Video Studio (software\)

Standing instructions for Claude Code in this repository. A task brief from the operator adds the task-specific scope; it never removes a rule here unless it says so explicitly and the operator confirms.

## What this project is
A local-first, Python-orchestrated AI video production studio. It coordinates ComfyUI (image generation and editing), LTX-2.5 (video and audio, through its unchanged CLI) and FFmpeg (post-production) on one Windows 11 machine: an RTX 3050 Laptop GPU with 6 GB VRAM, 32 GB RAM, an i7-13620H and Python 3.12. It must stay generic (films, commercials, music videos, documentaries, social content). The first production, a 60+ second luxury perfume commercial, is a proving ground, not a design target.

## Roles and workflow
- Planner and reviewer: Claude Desktop. Operator and final approver: the user. Executor: you.
- You work only from a brief pasted by the operator: one bounded task per session. The brief header (model, environment mode, work mode, effort) records what the operator set.
- If there is no brief, or no work mode is stated, assume READ-ONLY and ask.
- A recommendation is not an authorization. A design proposal is not an approved contract.

## Precedence
1. The operator's explicit instruction, including the approved brief.
2. Accepted ADRs (docs/adr, status Accepted) and this file.
3. Everything else: proposed ADRs, design documents, README, comments.
If a brief conflicts with an accepted ADR or this file, stop and report; do not resolve it yourself.

## Work modes
- READ-ONLY: inspect and report. No edits, tests or state changes.
- DESIGN-ONLY: READ-ONLY plus proposals. Write a design document only at the path the brief authorizes.
- IMPLEMENTATION: edit only the files the brief assigns, after plan approval.
- REVIEW: evaluate a change against requirements. Do not fix anything.

## Authorization levels (none are granted by default; each brief grants them per task)
- A1 author source and docs under software\
- A2 environment: venv creation, package installation, tooling changes
- A3 offline checks: ruff (check only), pyright, pytest with fakes, read-only doctor checks
- A4 database and data writes
- A5 engine control without inference (start, stop or query ComfyUI or LTX)
- A6 GPU inference
- A7 benchmarking
- A8 VCS operations (commit, branch, merge, push, worktree) and destructive operations

## Hard rules (always apply)
- The orchestration application never imports torch or any engine runtime (ADR-0005). GPU work runs out of process.
- src/studio/domain stays standard-library only: no pydantic, registry or engine imports.
- ComfyUI is externally managed: never start, stop, restart, reconfigure or modify it (ADR-0002).
- The LTX repository E:\Dev\ltx25-igpu-streaming is used only through its unchanged CLI and is never modified (ADR-0003). Its untracked files episodes/perfume-commercial-01/, prompts/misty-forest-sunrise.txt and prompts/perfume_pushin.txt are protected: never modify, move, clean, reset, stash or repurpose them.
- Never write, delete or move anything in engine directories, model directories (E:\AI_VIDEO_STUDIO\models, E:\AI-Data) or assets\ originals. The studio writes only to its own roots and outputs\studio, and every write path must pass studio.config.guard.authorize.
- Only one heavy GPU workload at a time. NVML free VRAM is the primary measurement: never double-count CUDA contexts already reflected in it. Every resource threshold is provisional until calibrated.
- Approval gates never hold a GPU lease. Generated outputs never overwrite approved versions. Imported assets are first-class assets with provenance.
- No paid or cloud inference APIs. No model or package downloads without explicit authorization.
- Never print secrets or environment variable values; report names only.
- Do not run Python in any form (imports, compileall, tools) unless the brief grants A3, and then only with the shared dev environment and PYTHONDONTWRITEBYTECODE=1.

## Decision references
- User decisions are cited as UD-n. ADR-0008 (D-n, P-n), ADR-0010 (P-n, U-n) and the older design items D6-D9 are separate series. Never mix them.
- Accepted ADRs are immutable. Change them only through a superseding ADR or an amendment explicitly marked as awaiting approval.
- Never turn a proposed decision into an accepted one.

## Git and repository rules
- This repository holds software\ only. Remote: origin = github.com/CodeHub1443/ai-video-studio (private).
- main is the integration branch. Do not commit to main unless the brief says so. Work on branches named <type>/<brief-id>-<slug>, for example feat/b05-slot-model.
- Parallel editing sessions each use their own worktree at E:\AI_VIDEO_STUDIO\worktrees\<branch-slug> and their own branch. Never run two editing sessions in one directory. Never reuse a worktree that has uncommitted work.
- Before any Git write: run git status and git worktree list. Stop if there are unexpected changes or you are not where the brief says.
- Run git from inside the repository or worktree, not with git -C, so the permission rules apply.
- Commit messages: Conventional Commits, type(scope): summary, with types feat, fix, docs, refactor, test, chore, build. One logical change per commit. Put the brief ID in the body ("Brief: B-nn"). Commit only when the brief grants A8.
- Push only when the brief authorizes it; the operator approves each push. Never force-push, delete remote branches, amend or rebase pushed commits, or rewrite history.
- Never use git reset --hard, git clean, git stash, git restore on uncommitted work, git branch -D, or git checkout -- <file>.
- Merge into main only after a REVIEW verdict of "accept" and explicit operator approval, preferably through a GitHub pull request, otherwise git merge --no-ff.
- Tags (for example m0.3a) only when a brief says so.
- Never commit secrets, .env files, models, generated media, databases or virtual environments.

## Layout
- src/studio/config: settings, paths, namespaces, access scopes, real-path guard, hardware profile, checks.
- src/studio/registry: engine, model and binding schema, loader, static compatibility checks.
- src/studio/domain: stdlib-only entities, state tables, recovery rules, canonical form, validation.
- config/: studio.toml, studio.test.toml, hardware/, registry/{engines,models,bindings}.
- docs/: architecture, configuration, registry, security model, domain model, adr/, design/.
- Planned, not present yet: tests/, workflows/, and the packages core, persistence, engines, resources, scheduler, assets, qc, audio, post, review, plan, cli.

## Commands (only when the brief authorizes them)
- Shared dev environment for every worktree: E:\AI_VIDEO_STUDIO\envs\studio-dev (Python 3.12, torch-free, the project is not installed). Exact versions: requirements/dev.lock.txt. Never install into it unless the brief grants A2.
- Run from the repository or worktree root with PYTHONPATH=src and PYTHONDONTWRITEBYTECODE=1, using E:\AI_VIDEO_STUDIO\envs\studio-dev\Scripts\python.exe (PY):
  - PY -m ruff check src tests --no-cache   (never --fix unless the brief says so)
  - PY -m ruff format --check src tests --no-cache
  - E:\AI_VIDEO_STUDIO\envs\studio-dev\Scripts\pyright.exe --pythonpath PY
  - PY -m pytest -p no:cacheprovider   (default addopts exclude the gpu, comfyui and ltx markers)
- Formatting is owned by ruff format; E501 is not linted.
- Every ruff invocation, including ruff format on changed files, uses --no-cache (R-7), so no .ruff_cache is created.

## Stop conditions
Stop and report the issue, evidence, impact, decision needed and safest next step when:
- the baseline or source of truth is unclear, or it changes during the task;
- requirements conflict, or you need an action outside your work mode or authorization;
- another session owns the files;
- an operation could delete or overwrite user work;
- the same problem has failed twice with the same approach.

## Required final report
1. Summary
2. Files/sections inspected or changed
3. Findings/decisions with evidence
4. Commands and tests run, with exact results
5. Checks not run and why
6. Risks, assumptions, and open questions
7. Recommended next step
