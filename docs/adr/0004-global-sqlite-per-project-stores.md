# ADR-0004: Global SQLite database and per-project stores

Status: Accepted

## Context
Single-user system needing durable job queue, history and reproducibility, plus large media that should be
portable per project. Authored production intent should be IDE-editable and diffable.

## Decision
- One global SQLite database (WAL) at `data/studio.db` holds runtime history and execution state: plan
  revisions, jobs, prepared tasks, attempts, runs, takes, objects, assets, versions, lineage, slot bindings,
  approvals, gates, exports, GPU/lease events.
- Each project has `projects/<slug>/{plan,store,work,review}`. Plan YAML is authored intent; the object store
  is per project.
- YAML is never written back automatically. `plan apply` is append-only: it archives and marks approvals
  stale and never deletes takes, assets, approvals or history. Changes flow `plan diff` ->
  `plan apply --confirm <diffhash>`.

## Consequences
- Zero-ops storage; migrations through Alembic.
- Cross-project queries are easy; a project's media is self-contained, while its history lives in the global DB.
- Moving a project needs an export/bundle step that carries its DB rows.
