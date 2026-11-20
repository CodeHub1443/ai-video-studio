"""Shared vocabulary of the domain.

``Operation``, ``MediaType`` and ``CommercialUse`` deliberately mirror the registry's enums
(``studio.registry.schema``) *by value* without importing them: the registry package pulls in the
pydantic-based configuration, and the domain must stay independent of it. Keeping the two in step is
a later test obligation (the value sets must match); nothing can check it while execution is not authorized.
"""

from __future__ import annotations

from enum import StrEnum


class Operation(StrEnum):
    T2I = "t2i"
    I2I = "i2i"
    INPAINT = "inpaint"
    T2V = "t2v"
    I2V = "i2v"
    V2V = "v2v"
    UPSCALE = "upscale"
    INTERP = "interp"
    TTS = "tts"
    MUSIC = "music"
    SFX = "sfx"
    ASR = "asr"
    TRANSCODE = "transcode"
    RENDER_TIMELINE = "render_timeline"


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    TEXT = "text"
    MASK = "mask"
    TIMELINE = "timeline"


class CommercialUse(StrEnum):
    """Recorded rights fact only. What an ``unknown`` or ``no`` value *blocks* is an undecided policy (D7-D9)."""

    YES = "yes"
    CONDITIONAL = "conditional"
    NO = "no"
    UNKNOWN = "unknown"
