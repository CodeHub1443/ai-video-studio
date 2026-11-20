"""Approved output namespaces, PINNED as code constants.

These values are deliberately not configuration: a settings file, an environment variable or an overlay
must not be able to broaden what an engine may write or move the studio's output area. Changing one is a
code change that needs an approved decision (see docs/adr/0008 and 0009).

They restate ADR-0003 (LTX) and ADR-0002 (ComfyUI). They have NOT been checked against what
``render.py`` or ComfyUI really write (open item U-4).
"""

from __future__ import annotations

from typing import Final

# Studio-owned: the only part of outputs_root the studio may write.
STUDIO_OUTPUTS_SUBDIR: Final = "studio"

# ComfyUI writes below <comfyui_output_dir>/studio/ (per-attempt layout studio/<project>/<job>/<attempt>).
COMFYUI_OUTPUT_SUBDIR: Final = "studio"

# LTX writes <ltx_repo>/videos/studio/** and <ltx_repo>/{latents,logs,previews}/studio-* only.
LTX_VIDEOS_FOLDER: Final = "videos"
LTX_STUDIO_CATEGORY: Final = "studio"
LTX_PREFIXED_FOLDERS: Final = ("latents", "logs", "previews")
LTX_NAME_PREFIX: Final = "studio-"

# Protected LTX folders: no actor may read, write, delete or move from them (ADR-0003; import of the
# protected perfume files is the undecided D11).
LTX_PROTECTED_FOLDERS: Final = ("episodes", "prompts")
