"""Loader checks: REG-2 warning, REG-3 conflicts, REG-5 ignored entries, REG-6 allowlist, and old behaviour."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

from studio.registry import (
    EXECUTION_ONLY_CREATIVE_ALLOWLIST,
    RESERVED_CREATIVE_FIELDS,
    Registry,
    RegistryError,
    RegistryIssue,
    load_registry,
)


def codes(issues: Iterable[RegistryIssue], severity: str | None = None) -> list[str]:
    return [i.code for i in issues if severity is None or i.severity == severity]


def load_ok(specs: Any, root: Path, **binding_over: Any) -> Registry:
    return load_registry(specs.write(root, bindings=[specs.binding(**binding_over)]))


def load_failing(specs: Any, root: Path, **binding_over: Any) -> RegistryError:
    with pytest.raises(RegistryError) as info:
        load_ok(specs, root, **binding_over)
    return info.value


# --- baseline ------------------------------------------------------------------------------------


def test_a_valid_registry_loads_with_its_unknown_resource_warnings(specs: Any, reg_root: Path):
    reg = load_ok(specs, reg_root)
    assert set(reg.bindings) == {"eng.b1"}
    assert {"vram_unknown", "ram_unknown", "commit_unknown"} <= set(codes(reg.warnings))
    assert codes(reg.warnings, "error") == []


# --- REG-2: determinism vs execution_only ------------------------------------------------------------


def test_bitwise_with_execution_only_keys_warns(specs: Any, reg_root: Path):
    reg = load_ok(specs, reg_root, determinism="bitwise", execution_only=["threads"])
    warned = [i for i in reg.warnings if i.code == "determinism_execution_only"]
    assert len(warned) == 1 and warned[0].subject == "eng.b1" and warned[0].severity == "warning"


@pytest.mark.parametrize(
    "determinism, execution_only",
    [("bitwise", []), ("perceptual", ["threads"]), ("none", ["threads"]), ("perceptual", [])],
)
def test_other_combinations_do_not_warn(
    specs: Any, reg_root: Path, determinism: str, execution_only: list[str]
):
    reg = load_ok(specs, reg_root, determinism=determinism, execution_only=execution_only)
    assert "determinism_execution_only" not in codes(reg.warnings)


# --- REG-3: managed_environment vs execution_only -----------------------------------------------------


def test_a_name_in_both_lists_is_an_error(specs: Any, reg_root: Path):
    err = load_failing(
        specs, reg_root, execution_only=["SYNC_S1", "TEMP"], managed_environment=["TEMP", "TMP"]
    )
    assert "managed_environment_conflict" in codes(err.issues, "error")
    assert "TEMP".casefold() in str(err)


def test_the_conflict_check_ignores_case_like_windows_environment_names(specs: Any, reg_root: Path):
    err = load_failing(specs, reg_root, execution_only=["temp"], managed_environment=["TEMP"])
    assert "managed_environment_conflict" in codes(err.issues, "error")


def test_disjoint_lists_load(specs: Any, reg_root: Path):
    reg = load_ok(specs, reg_root, execution_only=["SYNC_S1"], managed_environment=["TEMP", "TMP"])
    assert reg.bindings["eng.b1"].managed_environment == ("TEMP", "TMP")
    assert codes(reg.warnings, "error") == []


def test_duplicate_managed_names_are_an_error_even_when_the_case_differs(
    specs: Any, reg_root: Path
):
    err = load_failing(specs, reg_root, managed_environment=["TEMP", "temp"])
    assert "duplicate_key" in codes(err.issues, "error")


# --- REG-6: reserved creative fields in execution_only -----------------------------------------------


@pytest.mark.parametrize(
    "key", sorted(RESERVED_CREATIVE_FIELDS - EXECUTION_ONLY_CREATIVE_ALLOWLIST)
)
def test_reserved_creative_fields_outside_the_allowlist_are_errors(
    specs: Any, reg_root: Path, key: str
):
    err = load_failing(specs, reg_root, execution_only=[key])
    issues = [i for i in err.issues if i.code == "execution_only_reserved"]
    assert len(issues) == 1 and key in issues[0].message


@pytest.mark.parametrize("key", sorted(EXECUTION_ONLY_CREATIVE_ALLOWLIST))
def test_allowlisted_fields_are_accepted(specs: Any, reg_root: Path, key: str):
    reg = load_ok(specs, reg_root, execution_only=[key])
    assert reg.bindings["eng.b1"].execution_only == (key,)
    assert "execution_only_reserved" not in codes(reg.warnings)


def test_all_three_allowlisted_fields_together_and_free_keys_are_accepted(
    specs: Any, reg_root: Path
):
    reg = load_ok(specs, reg_root, execution_only=["width", "height", "duration_s", "threads", "X"])
    assert len(reg.bindings["eng.b1"].execution_only) == 5


def test_fps_is_never_adaptable(specs: Any, reg_root: Path):
    err = load_failing(specs, reg_root, execution_only=["width", "fps"])
    assert [i.code for i in err.issues if i.severity == "error"] == ["execution_only_reserved"]


def test_the_check_is_exact_so_free_keys_that_merely_look_similar_are_allowed(
    specs: Any, reg_root: Path
):
    reg = load_ok(specs, reg_root, execution_only=["Seed", "fps_hint", "prompt_cache"])
    assert codes(reg.warnings, "error") == []


# --- REG-5: ignored directories and non-files ---------------------------------------------------------


def test_a_plain_subdirectory_is_reported_not_skipped_silently(specs: Any, reg_root: Path):
    specs.write(reg_root)
    (reg_root / "bindings" / "archive").mkdir()
    reg = load_registry(reg_root)
    hits = [i for i in reg.warnings if i.code == "ignored_entry"]
    assert [h.subject for h in hits] == ["bindings/archive"]
    assert hits[0].severity == "warning"


def test_yaml_named_directory_is_reported(specs: Any, reg_root: Path):
    specs.write(reg_root)
    (reg_root / "engines" / "old.yaml").mkdir()
    reg = load_registry(reg_root)
    assert [i.subject for i in reg.warnings if i.code == "ignored_entry"] == ["engines/old.yaml"]


def test_files_inside_an_ignored_directory_are_not_loaded(specs: Any, reg_root: Path):
    specs.write(reg_root)
    nested = reg_root / "bindings" / "nested"
    nested.mkdir()
    (nested / "eng.hidden.yaml").write_text(
        json.dumps(specs.binding(id="eng.hidden")), encoding="utf-8"
    )
    reg = load_registry(reg_root)
    assert set(reg.bindings) == {"eng.b1"}


def test_a_non_yaml_file_keeps_the_ignored_file_warning(specs: Any, reg_root: Path):
    specs.write(reg_root)
    (reg_root / "models" / "notes.txt").write_text("hi", encoding="utf-8")
    reg = load_registry(reg_root)
    assert [i.code for i in reg.warnings if i.subject == "models/notes.txt"] == ["ignored_file"]


def test_ignored_entries_do_not_make_a_valid_registry_fail(specs: Any, reg_root: Path):
    specs.write(reg_root)
    (reg_root / "engines" / "d").mkdir()
    (reg_root / "bindings" / "d.yml").mkdir()
    reg = load_registry(reg_root)
    assert codes(reg.warnings).count("ignored_entry") == 2


# --- behaviour that must not change --------------------------------------------------------------------


def test_missing_section_folder_is_an_error(specs: Any, reg_root: Path):
    specs.write(reg_root)
    (reg_root / "bindings" / "eng.b1.yaml").unlink()
    (reg_root / "bindings").rmdir()
    with pytest.raises(RegistryError) as info:
        load_registry(reg_root)
    assert "missing_dir" in codes(info.value.issues, "error")


def test_unknown_engine_and_unknown_model_are_errors(specs: Any, reg_root: Path):
    err = load_failing(specs, reg_root, engine="ghost", models=["ghost_model"])
    assert {"unknown_engine", "unknown_model"} <= set(codes(err.issues, "error"))


def test_duplicate_ids_and_bad_yaml_and_non_mappings_are_errors(specs: Any, reg_root: Path):
    specs.write(reg_root)
    b = reg_root / "bindings"
    (b / "copy.yaml").write_text(json.dumps(specs.binding()), encoding="utf-8")
    (b / "broken.yaml").write_text("a: [unclosed", encoding="utf-8")
    (b / "list.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    with pytest.raises(RegistryError) as info:
        load_registry(reg_root)
    found = set(codes(info.value.issues, "error"))
    assert {"duplicate_id", "yaml_error", "not_a_mapping"} <= found


def test_schema_errors_name_the_file_and_field(specs: Any, reg_root: Path):
    err = load_failing(specs, reg_root, surprise=1)
    assert any(i.code == "schema" and "surprise" in i.subject for i in err.issues)


def test_filename_mismatch_is_a_warning(specs: Any, reg_root: Path):
    specs.write(reg_root)
    (reg_root / "bindings" / "eng.b1.yaml").rename(reg_root / "bindings" / "other.yaml")
    assert "filename" in codes(load_registry(reg_root).warnings, "warning")


def test_registry_without_bindings_is_an_error(specs: Any, reg_root: Path):
    with pytest.raises(RegistryError) as info:
        load_registry(specs.write(reg_root, bindings=[]))
    assert "empty_registry" in codes(info.value.issues, "error")
