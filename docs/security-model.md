# Security model (filesystem and configuration)

Scope: what M0.2 declares and validates. **Nothing here is exercised yet**: no code has been run and no
component writes, moves or deletes files. Items marked *(approved)* implement decisions the user
supplied; items marked *(proposed)* are the implementer's choices awaiting approval (ADR-0008, ADR-0009).

## 1. Ownership and actors

Three actors: the **studio**, **ComfyUI** and **LTX**. Four operations: read, write, delete, move-source
(a move deletes its source, so it is judged as a delete there).

| Location | Owner | Studio may | Engine may |
|---|---|---|---|
| `projects`, `cache`, `logs`, `data`, `outputs\studio` | studio | read, write, delete, move-source | nothing |
| `assets\` (read-only originals) | nobody | **read only** | nothing |
| `models`, LTX weights | nobody | **read only** | read (engines read them directly) |
| `<comfyui_output_dir>\studio\**` | ComfyUI | **read/import only** | ComfyUI writes |
| `<ltx>\videos\studio\**`, `<ltx>\{latents,logs,previews}\studio-*` | LTX | **read/import only** | LTX writes |
| `<ltx>\episodes`, `<ltx>\prompts` | nobody | **nothing** (undecided D11) | nothing |
| everything else, including the rest of `outputs\`, `software\` | - | read nothing, write nothing (default deny; `software\` and models explicitly denied for mutation) | - |

*(approved)* The studio never writes, deletes or move-source-deletes in an external engine directory.
The consequences for ADR-0006's "move" and hardlink read-only-attribute steps are in ADR-0009 (proposed).

## 2. Pinned namespaces *(approved)*

The output namespaces are code constants in `studio.config.namespaces`, not configuration:
ComfyUI `studio`, LTX category `studio` and name prefix `studio-`, studio outputs subfolder `studio`.
Settings keys that could change them (`output_subdir`, `studio_category`, `name_prefix`,
`studio_outputs_subdir`) no longer exist and are rejected. They restate ADR-0002/0003 and have not been
checked against what the engines actually write (U-4).

## 3. Layers of enforcement

1. **Lexical pre-check** `AccessPolicy.check(path, actor, op)` (`studio.config.scopes`): default deny,
   deny rules first, then scopes; each scope says which actor may perform which operation. It is a
   necessary condition only, because strings cannot reveal links or races.
2. **Mandatory real-path validation** `authorize(policy, path, op)` (`studio.config.guard`) *(approved)*:
   the only way to obtain an `Authorization`. It performs no file operation. It requires that
   - the pre-check passes;
   - the deepest existing ancestor-or-self of the target resolves (`realpath`) to itself, so no symlink,
     junction, mapped-drive or 8.3 alias lies above it;
   - no existing component from the scope base down is a reparse point of any kind;
   - an existing target's real path is still inside the same scope;
   - *(proposed)* a mutating operation on an existing file with more than one hard link is refused.
   Any failure raises `AccessDenied` (fail closed). A symlink/junction escape therefore cannot be authorized.
3. **Read-only environment checks** (`check_environment`) report links on configured roots and ancestors.

Engine output targets (a prefix the studio hands to ComfyUI or LTX) are validated lexically with
`policy.check(path, Actor.COMFYUI|Actor.LTX, FsOp.WRITE)` before being sent; `authorize` refuses non-studio actors.

## 4. TOCTOU-safe design (to be implemented with the first file-operation milestone) *(approved: design)*

A check followed by a path-based operation is racy: the path can become a junction in between. `authorize`
narrows the window but cannot close it. The file layer MUST:
1. never use the path string for the operation after authorization;
2. call `revalidate(policy, authorization)` immediately before use (rechecks scope, links, and that the
   anchor directory was not replaced);
3. open the parent directory handle **without following reparse points** (Win32 `CreateFile` with
   `FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS`) and create/open the final component relative
   to that handle;
4. after opening, obtain the handle's final path (`GetFinalPathNameByHandle`) and verify it is still inside
   the scope and equals the authorized path; abort and clean up if not;
5. write via a temporary name in the same scope and finish with an atomic replace (ADR-0006), never
   modifying a published file in place;
6. treat any unexpected condition as failure.
This protocol is a design and is unverified (U-12). `os.path.realpath`, `lstat` and `stat` are the only
APIs used today, and only for inspection.

## 5. Path-segment hardening (`studio.config.paths`)

Applied to registry model-file paths, hardware-profile names, `render_script`, and every configured
absolute path. A segment is rejected if it:
- is empty, `.` or `..` (`.` alone is accepted only as a model root, and only for `format: directory`);
- contains `: < > " | ? * / \` or a control character. `:` rules out drive components and **NTFS alternate
  data streams** (`file:stream`, `file::$DATA`);
- ends with a dot or space; is a reserved device name (`CON`, `NUL.txt`, `COM1`, ...);
- looks like an 8.3 short name (`PROGRA~1`); is longer than 255 characters (relative paths: 240).

Absolute paths must be on a lettered drive; UNC, `\\?\`, `\\.\` and drive-relative paths are rejected.

## 6. Environments *(approved)*

| Env | Behaviour |
|---|---|
| `dev` | Enabled. Real paths and engines allowed. |
| `test` | Isolated. `test_sandbox_root` is required and is the explicit allowlist: `workspace_root`, every studio root, every external root (models, ComfyUI, ComfyUI output, LTX repo, **LTX python**, LTX weights) and the **ffmpeg/ffprobe executables** (absolute paths, no bare names) must all lie strictly inside it. The sandbox must not overlap the real workspace, the protected LTX repository or the real LTX weights, and must be at least two levels below a drive root. `comfyui.port` must not be 8188; `allow_remote` is false; `allows_live_engines` is false. |
| `prod` | **Disabled** pending a separate approved design. Constructing settings for it raises. |

`STUDIO_ENV` is validated (case-insensitive; empty is an error). The same value selects the overlay and the
`env` field; a TOML file cannot set `env`. Limits: the sandbox is lexical, nothing proves it is a temporary
folder (U-6); `registry_dir` and the config folder are read-only inputs and are not confined (U-5); a link
inside the sandbox is caught by `authorize`/`check_environment`, not at construction.

## 7. Registry as an input

Registry YAML is parsed with the safe loader (no object construction; duplicate keys rejected), file size
is capped, symlinked files and folders are rejected, `adapter` must be under `studio.engines.` and is never
imported by the registry, and model metadata carries no absolute paths.
