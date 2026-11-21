"""Guard tests over a fake probe: nothing here touches the real disk (except the OsProbe tests, tmp_path only)."""

from __future__ import annotations

import copy
import dataclasses
import ntpath
import os
import pickle
from pathlib import Path, PureWindowsPath

import pytest

from studio.config.checks import is_reparse_point, probe_reparse_point
from studio.config.guard import (
    Authorization,
    OsProbe,
    authorize,
    authorize_child,
    is_issued,
    revalidate,
)
from studio.config.scopes import (
    AccessDenied,
    AccessPolicy,
    Actor,
    FsOp,
    MatchMode,
    Scope,
    build_access_policy,
)
from studio.config.settings import PathsConfig

PROJECTS = Path(r"E:\AI_VIDEO_STUDIO\projects")
MODELS = Path(r"E:\AI_VIDEO_STUDIO\models")
ASSETS = Path(r"E:\AI_VIDEO_STUDIO\assets")
LTX_MODELS = Path(r"E:\AI-Data\LTX25")
COMFY_OUT = Path(r"E:\AI_VIDEO_STUDIO\comfyui\ComfyUI\output\studio")
LTX_VIDEOS = Path(r"E:\Dev\ltx25-igpu-streaming\videos\studio")


def _key(path: Path | str) -> str:
    return ntpath.normcase(ntpath.normpath(str(path)))


class FakeFs:
    """Dictionary-backed FsProbe. ``None`` values model probe failures."""

    def __init__(self) -> None:
        self.nodes: set[str] = set()
        self.real: dict[str, str] = {}
        self.reparse: set[str] = set()
        self.ids: dict[str, tuple[int, int] | None] = {}
        self.links: dict[str, int | None] = {}
        self.unknown_exists: set[str] = set()  # lexists cannot answer (None)
        self.unknown_reparse: set[str] = set()  # is_reparse_point cannot answer (None)
        self._next = 1

    def add(self, path: Path | str, *, links: int | None = 1) -> Path:
        pw = PureWindowsPath(str(path))
        for i in range(1, len(pw.parts) + 1):
            k = _key(Path(*pw.parts[:i]))
            if k not in self.nodes:
                self.nodes.add(k)
                self.ids[k] = (1, self._next)
                self._next += 1
        self.links[_key(path)] = links
        return Path(str(path))

    def lexists(self, path: str) -> bool | None:
        if _key(path) in self.unknown_exists:
            return None
        return _key(path) in self.nodes

    def realpath(self, path: str) -> str:
        """Resolve through remapped ancestors, like a real ``realpath`` follows a link above the path."""
        pw = PureWindowsPath(str(path))
        for i in range(len(pw.parts), 0, -1):
            mapped = self.real.get(_key(Path(*pw.parts[:i])))
            if mapped is not None:
                return str(Path(mapped, *pw.parts[i:]))
        return str(path)

    def is_reparse_point(self, path: str) -> bool | None:
        if _key(path) in self.unknown_reparse:
            return None
        return _key(path) in self.reparse

    def identity(self, path: str) -> tuple[int, int] | None:
        return self.ids.get(_key(path))

    def link_count(self, path: str) -> int | None:
        return self.links.get(_key(path), 1)


@pytest.fixture(scope="module")
def policy() -> AccessPolicy:
    return build_access_policy(PathsConfig.model_validate({}))


@pytest.fixture
def fs() -> FakeFs:
    f = FakeFs()
    for root in (PROJECTS, MODELS, ASSETS, LTX_MODELS, COMFY_OUT, LTX_VIDEOS):
        f.add(root)
    return f


# --- baseline behaviour --------------------------------------------------------------------------


def test_authorize_existing_file_issues_a_genuine_authorization(policy: AccessPolicy, fs: FakeFs):
    target = fs.add(PROJECTS / "p1" / "a.txt")
    auth = authorize(policy, target, FsOp.WRITE, probe=fs)
    assert is_issued(auth)
    assert auth.scope_name == "projects"
    assert auth.op is FsOp.WRITE
    assert auth.multi_link is False
    assert auth.anchor == target
    assert auth.anchor_identity == fs.ids[_key(target)]


def test_authorize_missing_target_anchors_at_deepest_existing_dir(policy: AccessPolicy, fs: FakeFs):
    fs.add(PROJECTS / "p1")
    auth = authorize(policy, PROJECTS / "p1" / "new" / "x.bin", FsOp.WRITE, probe=fs)
    assert auth.anchor == PROJECTS / "p1"


def test_authorize_refuses_non_studio_actor(policy: AccessPolicy, fs: FakeFs):
    with pytest.raises(AccessDenied, match="studio operations"):
        authorize(policy, PROJECTS / "x", FsOp.WRITE, actor=Actor.LTX, probe=fs)


@pytest.mark.parametrize(
    "path, op",
    [
        (Path(r"E:\elsewhere\x.txt"), FsOp.READ),
        (ASSETS / "orig.png", FsOp.WRITE),
        (MODELS / "m.gguf", FsOp.DELETE),
        (Path(r"E:\AI_VIDEO_STUDIO\software\src\x.py"), FsOp.WRITE),
    ],
)
def test_lexical_denials_come_first(policy: AccessPolicy, fs: FakeFs, path: Path, op: FsOp):
    with pytest.raises(AccessDenied):
        authorize(policy, path, op, probe=fs)


def test_symlink_alias_on_the_anchor_path_is_refused(policy: AccessPolicy, fs: FakeFs):
    target = fs.add(PROJECTS / "alias" / "a.txt")
    fs.real[_key(PROJECTS / "alias")] = r"E:\somewhere\else"
    with pytest.raises(AccessDenied, match="link or alias"):
        authorize(policy, target, FsOp.WRITE, probe=fs)


def test_reparse_point_inside_scope_is_refused(policy: AccessPolicy, fs: FakeFs):
    target = fs.add(PROJECTS / "junction" / "a.txt")
    fs.reparse.add(_key(PROJECTS / "junction"))
    with pytest.raises(AccessDenied, match="reparse"):
        authorize(policy, target, FsOp.WRITE, probe=fs)


def test_target_whose_real_path_differs_is_refused(policy: AccessPolicy, fs: FakeFs):
    target = fs.add(PROJECTS / "a.txt")
    fs.real[_key(target)] = r"E:\AI_VIDEO_STUDIO\projects\other.txt"
    with pytest.raises(AccessDenied, match="resolves to"):
        authorize(policy, target, FsOp.WRITE, probe=fs)


def test_not_even_the_drive_root_exists(policy: AccessPolicy):
    with pytest.raises(AccessDenied, match="drive root"):
        authorize(policy, PROJECTS / "a.txt", FsOp.WRITE, probe=FakeFs())


# --- G-1: probe failure denies ---------------------------------------------------------------------


@pytest.mark.parametrize("op", [FsOp.READ, FsOp.WRITE, FsOp.DELETE])
def test_g1_unknown_link_count_denies_every_operation(policy: AccessPolicy, fs: FakeFs, op: FsOp):
    target = fs.add(PROJECTS / "p1" / "a.txt", links=None)
    with pytest.raises(AccessDenied, match="hard-link count unknown"):
        authorize(policy, target, op, probe=fs)


def test_g1_unknown_link_count_denies_in_a_read_only_scope_too(policy: AccessPolicy, fs: FakeFs):
    target = fs.add(MODELS / "m.gguf", links=None)
    with pytest.raises(AccessDenied, match="hard-link count unknown"):
        authorize(policy, target, FsOp.READ, probe=fs)


def test_g1_link_count_is_irrelevant_for_a_missing_target(policy: AccessPolicy, fs: FakeFs):
    fs.add(PROJECTS / "p1")
    auth = authorize(policy, PROJECTS / "p1" / "missing.txt", FsOp.WRITE, probe=fs)
    assert is_issued(auth)


def test_g1_os_probe_link_count_is_none_for_a_missing_path(tmp_path: Path):
    assert OsProbe().link_count(str(tmp_path / "missing.bin")) is None


def test_os_probe_counts_hard_links_under_tmp_path(tmp_path: Path):
    a = tmp_path / "a.bin"
    a.write_bytes(b"x")
    probe = OsProbe()
    assert probe.link_count(str(a)) == 1
    b = tmp_path / "b.bin"
    try:
        os.link(a, b)
    except OSError:  # pragma: no cover - filesystem without hard links
        pytest.skip("hard links unavailable")
    assert probe.link_count(str(a)) == 2
    assert probe.identity(str(a)) == probe.identity(str(b))
    assert probe.identity(str(tmp_path / "missing.bin")) is None


# --- G-2: unknown identity denies ----------------------------------------------------------------


def test_g2_authorize_denies_when_the_anchor_identity_is_unknown(policy: AccessPolicy, fs: FakeFs):
    target = fs.add(PROJECTS / "p1" / "a.txt")
    fs.ids[_key(target)] = None
    with pytest.raises(AccessDenied, match="identity"):
        authorize(policy, target, FsOp.WRITE, probe=fs)


def test_g2_authorize_denies_an_unknown_identity_for_a_missing_target(
    policy: AccessPolicy, fs: FakeFs
):
    fs.add(PROJECTS / "p1")
    fs.ids[_key(PROJECTS / "p1")] = None
    with pytest.raises(AccessDenied, match="identity"):
        authorize(policy, PROJECTS / "p1" / "new.txt", FsOp.WRITE, probe=fs)


def test_g2_revalidate_denies_when_the_identity_becomes_unknown(policy: AccessPolicy, fs: FakeFs):
    target = fs.add(PROJECTS / "p1" / "a.txt")
    auth = authorize(policy, target, FsOp.WRITE, probe=fs)
    fs.ids[_key(target)] = None
    with pytest.raises(AccessDenied, match="identity"):
        revalidate(policy, auth, probe=fs)


def test_g2_revalidate_denies_an_authorization_without_identity(policy: AccessPolicy, fs: FakeFs):
    forged = object.__new__(Authorization)
    for name, value in (
        ("path", PROJECTS / "a"),
        ("scope_name", "projects"),
        ("op", FsOp.WRITE),
        ("anchor", PROJECTS),
        ("anchor_identity", None),
        ("multi_link", False),
    ):
        object.__setattr__(forged, name, value)
    with pytest.raises(AccessDenied):
        revalidate(policy, forged, probe=fs)


def test_revalidate_accepts_an_unchanged_target_and_detects_a_swap(
    policy: AccessPolicy, fs: FakeFs
):
    target = fs.add(PROJECTS / "p1" / "a.txt")
    auth = authorize(policy, target, FsOp.WRITE, probe=fs)
    again = revalidate(policy, auth, probe=fs)
    assert is_issued(again) and again.anchor_identity == auth.anchor_identity
    fs.ids[_key(target)] = (9, 99)  # the file was replaced
    with pytest.raises(AccessDenied, match="replaced"):
        revalidate(policy, auth, probe=fs)


def test_revalidate_detects_a_new_hard_link_in_a_read_only_scope(policy: AccessPolicy, fs: FakeFs):
    target = fs.add(MODELS / "m.gguf")
    auth = authorize(policy, target, FsOp.READ, probe=fs)
    fs.links[_key(target)] = 2
    with pytest.raises(AccessDenied, match="additional hard links"):
        revalidate(policy, auth, probe=fs)


# --- G-3: no forged Authorization ----------------------------------------------------------------


@pytest.fixture
def real_auth(policy: AccessPolicy, fs: FakeFs) -> Authorization:
    return authorize(policy, fs.add(PROJECTS / "p1" / "a.txt"), FsOp.WRITE, probe=fs)


def test_g3_direct_construction_is_refused():
    with pytest.raises(TypeError):
        Authorization(  # type: ignore[call-arg]
            path=PROJECTS / "a",
            scope_name="projects",
            op=FsOp.WRITE,
            anchor=PROJECTS,
            anchor_identity=(1, 1),
        )
    with pytest.raises(TypeError):
        Authorization(  # type: ignore[arg-type]
            object(),
            path=PROJECTS / "a",
            scope_name="projects",
            op=FsOp.WRITE,
            anchor=PROJECTS,
            anchor_identity=(1, 1),
        )
    with pytest.raises(TypeError):
        Authorization(  # type: ignore[arg-type]
            None,
            path=PROJECTS / "a",
            scope_name="projects",
            op=FsOp.WRITE,
            anchor=PROJECTS,
            anchor_identity=(1, 1),
        )


def test_g3_dataclasses_replace_cannot_forge(real_auth: Authorization):
    with pytest.raises(TypeError):
        dataclasses.replace(real_auth, path=PROJECTS / "other")  # type: ignore[type-var]


def test_g3_copy_and_deepcopy_are_refused(real_auth: Authorization):
    with pytest.raises(TypeError):
        copy.copy(real_auth)
    with pytest.raises(TypeError):
        copy.deepcopy(real_auth)


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_g3_pickle_is_refused(real_auth: Authorization, protocol: int):
    with pytest.raises(TypeError):
        pickle.dumps(real_auth, protocol=protocol)


def test_g3_attributes_are_read_only(real_auth: Authorization):
    with pytest.raises(AttributeError):
        real_auth.path = PROJECTS / "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del real_auth.scope_name  # type: ignore[misc]


def test_g3_subclassing_is_refused():
    with pytest.raises(TypeError):

        class Fake(Authorization):  # noqa: B903
            pass


def test_g3_is_issued_is_true_only_for_issued_objects(real_auth: Authorization):
    assert is_issued(real_auth)
    assert not is_issued(object())
    assert not is_issued(None)
    forged = object.__new__(Authorization)
    for name, value in (
        ("path", real_auth.path),
        ("scope_name", real_auth.scope_name),
        ("op", real_auth.op),
        ("anchor", real_auth.anchor),
        ("anchor_identity", real_auth.anchor_identity),
        ("multi_link", False),
    ):
        object.__setattr__(forged, name, value)
    assert not is_issued(forged)


def test_g3_a_forgery_is_refused_by_revalidate_and_authorize_child(
    policy: AccessPolicy, fs: FakeFs, real_auth: Authorization
):
    forged = object.__new__(Authorization)
    for name in ("path", "scope_name", "op", "anchor", "anchor_identity", "multi_link"):
        object.__setattr__(forged, name, getattr(real_auth, name))
    with pytest.raises(AccessDenied, match="not an Authorization"):
        revalidate(policy, forged, probe=fs)
    with pytest.raises(AccessDenied, match="not an Authorization"):
        authorize_child(policy, forged, "child.txt", FsOp.DELETE, probe=fs)


def test_g3_a_collected_authorization_leaves_the_registry():
    import gc

    from studio.config import guard

    before = len(guard._ISSUED)  # pyright: ignore[reportPrivateUsage]
    pol = build_access_policy(PathsConfig.model_validate({}))
    fs = FakeFs()
    fs.add(PROJECTS)
    auth = authorize(pol, PROJECTS / "x.txt", FsOp.WRITE, probe=fs)
    assert len(guard._ISSUED) == before + 1  # pyright: ignore[reportPrivateUsage]
    del auth
    gc.collect()
    assert len(guard._ISSUED) == before  # pyright: ignore[reportPrivateUsage]


# --- G-4 / I-13: READ of a multi-link file -----------------------------------------------------------


@pytest.mark.parametrize(
    "scope_path", [MODELS / "m.gguf", LTX_MODELS / "w.safetensors", ASSETS / "o.png"]
)
def test_g4_read_of_multi_link_file_is_allowed_and_recorded_in_read_only_scopes(
    policy: AccessPolicy, fs: FakeFs, scope_path: Path
):
    target = fs.add(scope_path, links=3)
    auth = authorize(policy, target, FsOp.READ, probe=fs)
    assert auth.multi_link is True
    assert is_issued(auth)


@pytest.mark.parametrize(
    "scope_path", [MODELS / "m.gguf", LTX_MODELS / "w.safetensors", ASSETS / "o.png"]
)
def test_g4_single_link_read_in_read_only_scopes_is_not_flagged(
    policy: AccessPolicy, fs: FakeFs, scope_path: Path
):
    auth = authorize(policy, fs.add(scope_path), FsOp.READ, probe=fs)
    assert auth.multi_link is False


@pytest.mark.parametrize(
    "scope_path",
    [PROJECTS / "p" / "a.bin", COMFY_OUT / "img.png", LTX_VIDEOS / "v.mp4"],
    ids=["studio-owned", "comfyui-engine", "ltx-engine"],
)
def test_g4_read_of_multi_link_file_is_refused_in_studio_and_engine_scopes(
    policy: AccessPolicy, fs: FakeFs, scope_path: Path
):
    target = fs.add(scope_path, links=2)
    with pytest.raises(AccessDenied, match="multiple hard links"):
        authorize(policy, target, FsOp.READ, probe=fs)


@pytest.mark.parametrize("op", [FsOp.WRITE, FsOp.DELETE, FsOp.MOVE_SOURCE])
def test_g4_mutation_of_multi_link_file_is_refused(policy: AccessPolicy, fs: FakeFs, op: FsOp):
    target = fs.add(PROJECTS / "a.bin", links=2)
    with pytest.raises(AccessDenied, match="multiple hard links"):
        authorize(policy, target, op, probe=fs)


# --- G-5: authorize_child ------------------------------------------------------------------------


@pytest.fixture
def parent_dir(policy: AccessPolicy, fs: FakeFs) -> Authorization:
    return authorize(policy, fs.add(PROJECTS / "p1" / "work"), FsOp.DELETE, probe=fs)


def test_g5_child_authorization_happy_path(
    policy: AccessPolicy, fs: FakeFs, parent_dir: Authorization
):
    fs.add(PROJECTS / "p1" / "work" / "a.bin")
    child = authorize_child(policy, parent_dir, "a.bin", FsOp.DELETE, probe=fs)
    assert is_issued(child)
    assert child.path == PROJECTS / "p1" / "work" / "a.bin"
    assert child.op is FsOp.DELETE
    assert child.scope_name == parent_dir.scope_name


def test_g5_child_that_does_not_exist_is_still_authorized_for_write(
    policy: AccessPolicy, fs: FakeFs, parent_dir: Authorization
):
    child = authorize_child(policy, parent_dir, "new.bin", FsOp.WRITE, probe=fs)
    assert child.anchor == parent_dir.path


@pytest.mark.parametrize(
    "name", ["..", ".", "", "a/b", "a\\b", "C:x", "a:b", "con", "x.", "PROGRA~1"]
)
def test_g5_child_name_must_be_one_clean_segment(
    policy: AccessPolicy, fs: FakeFs, parent_dir: Authorization, name: str
):
    with pytest.raises(AccessDenied, match="invalid child name"):
        authorize_child(policy, parent_dir, name, FsOp.DELETE, probe=fs)


def test_g5_parent_must_be_a_mutation(policy: AccessPolicy, fs: FakeFs):
    read_parent = authorize(policy, fs.add(PROJECTS / "p1"), FsOp.READ, probe=fs)
    with pytest.raises(AccessDenied, match="not a mutation"):
        authorize_child(policy, read_parent, "a.bin", FsOp.DELETE, probe=fs)


def test_g5_child_operation_must_be_a_mutation(
    policy: AccessPolicy, fs: FakeFs, parent_dir: Authorization
):
    with pytest.raises(AccessDenied, match="not a mutation"):
        authorize_child(policy, parent_dir, "a.bin", FsOp.READ, probe=fs)


def test_g5_every_check_is_rerun_for_the_child(
    policy: AccessPolicy, fs: FakeFs, parent_dir: Authorization
):
    # reparse point child
    link = fs.add(PROJECTS / "p1" / "work" / "link")
    fs.reparse.add(_key(link))
    with pytest.raises(AccessDenied, match="reparse"):
        authorize_child(policy, parent_dir, "link", FsOp.DELETE, probe=fs)
    # alias child (real path differs)
    alias = fs.add(PROJECTS / "p1" / "work" / "alias")
    fs.real[_key(alias)] = r"E:\AI_VIDEO_STUDIO\data\x"
    with pytest.raises(AccessDenied):
        authorize_child(policy, parent_dir, "alias", FsOp.DELETE, probe=fs)
    # unknown link count and unknown identity
    unknown = fs.add(PROJECTS / "p1" / "work" / "unknown", links=None)
    with pytest.raises(AccessDenied, match="hard-link count unknown"):
        authorize_child(policy, parent_dir, "unknown", FsOp.DELETE, probe=fs)
    noid = fs.add(PROJECTS / "p1" / "work" / "noid")
    fs.ids[_key(noid)] = None
    with pytest.raises(AccessDenied, match="identity"):
        authorize_child(policy, parent_dir, "noid", FsOp.DELETE, probe=fs)
    assert unknown.name == "unknown"


def test_g5_multi_link_children_are_refused(
    policy: AccessPolicy, fs: FakeFs, parent_dir: Authorization
):
    fs.add(PROJECTS / "p1" / "work" / "hl.bin", links=2)
    for op in (FsOp.WRITE, FsOp.DELETE, FsOp.MOVE_SOURCE):
        with pytest.raises(AccessDenied, match="multiple hard links"):
            authorize_child(policy, parent_dir, "hl.bin", op, probe=fs)


def test_g5_child_below_a_reparse_parent_is_refused(policy: AccessPolicy, fs: FakeFs):
    parent = authorize(policy, fs.add(PROJECTS / "p2" / "d"), FsOp.DELETE, probe=fs)
    fs.add(PROJECTS / "p2" / "d" / "c.bin")
    fs.reparse.add(
        _key(PROJECTS / "p2" / "d")
    )  # the directory became a junction after authorization
    with pytest.raises(AccessDenied, match="reparse"):
        authorize_child(policy, parent, "c.bin", FsOp.DELETE, probe=fs)


def test_g5_parent_in_a_read_only_scope_is_impossible(policy: AccessPolicy, fs: FakeFs):
    with pytest.raises(AccessDenied):
        authorize(policy, fs.add(MODELS / "d"), FsOp.DELETE, probe=fs)


# --- G-5 / C-4: the parent is revalidated, and the child must stay in the parent's scope ---------------


def test_c4_authorize_child_revalidates_the_parent(
    policy: AccessPolicy, fs: FakeFs, parent_dir: Authorization
):
    fs.add(PROJECTS / "p1" / "work" / "a.bin")
    fs.ids[_key(parent_dir.path)] = (9, 99)  # the parent directory was swapped after authorization
    with pytest.raises(AccessDenied, match="replaced"):
        authorize_child(policy, parent_dir, "a.bin", FsOp.DELETE, probe=fs)


def test_c4_authorize_child_refuses_a_parent_whose_identity_became_unknown(
    policy: AccessPolicy, fs: FakeFs, parent_dir: Authorization
):
    fs.add(PROJECTS / "p1" / "work" / "a.bin")
    fs.ids[_key(parent_dir.path)] = None
    with pytest.raises(AccessDenied, match="identity"):
        authorize_child(policy, parent_dir, "a.bin", FsOp.DELETE, probe=fs)


def test_g5_child_in_a_different_scope_than_the_parent_is_refused(fs: FakeFs):
    base = PROJECTS / "a"
    outer = Scope("outer", base, MatchMode.SUBTREE, Actor.STUDIO)
    inner = Scope("inner", base / "inner", MatchMode.SUBTREE, Actor.STUDIO)
    nested = AccessPolicy(scopes=(inner, outer), deny=())  # the first matching scope wins
    fs.add(base / "inner")
    parent = authorize(nested, base, FsOp.DELETE, probe=fs)
    assert parent.scope_name == "outer"
    with pytest.raises(AccessDenied, match="differs from the parent's"):
        authorize_child(nested, parent, "inner", FsOp.DELETE, probe=fs)


# --- D-2: a rewritten Authorization is no longer issued ----------------------------------------------


@pytest.mark.parametrize(
    "field, value",
    [
        ("path", Path(r"C:\Windows\System32\x.dll")),
        ("scope_name", "data"),
        ("op", FsOp.DELETE),
        ("anchor", PROJECTS),
        ("anchor_identity", (9, 99)),
        ("multi_link", True),
    ],
)
def test_d2_rewriting_any_field_with_object_setattr_is_detected(
    policy: AccessPolicy, fs: FakeFs, real_auth: Authorization, field: str, value: object
):
    assert is_issued(real_auth)
    object.__setattr__(real_auth, field, value)
    assert not is_issued(real_auth)
    with pytest.raises(AccessDenied, match="not an Authorization"):
        revalidate(policy, real_auth, probe=fs)


def test_d2_rewriting_a_field_through_the_slot_descriptor_is_detected(real_auth: Authorization):
    vars(Authorization)["op"].__set__(real_auth, FsOp.DELETE)
    assert real_auth.op is FsOp.DELETE
    assert not is_issued(real_auth)


def test_d2_a_value_of_another_type_that_compares_equal_is_detected(real_auth: Authorization):
    object.__setattr__(real_auth, "scope_name", str.__new__(type("S", (str,), {}), "projects"))
    assert real_auth.scope_name == "projects"
    assert not is_issued(real_auth)


def test_d2_a_deleted_slot_is_not_issued(real_auth: Authorization):
    vars(Authorization)["path"].__delete__(real_auth)
    assert not is_issued(real_auth)


def test_d2_a_read_parent_rewritten_to_delete_cannot_authorize_children(
    policy: AccessPolicy, fs: FakeFs
):
    parent = authorize(policy, fs.add(PROJECTS / "p1" / "dir"), FsOp.READ, probe=fs)
    with pytest.raises(AccessDenied, match="not a mutation"):
        authorize_child(policy, parent, "x.bin", FsOp.DELETE, probe=fs)
    object.__setattr__(parent, "op", FsOp.DELETE)
    with pytest.raises(AccessDenied, match="not an Authorization"):
        authorize_child(policy, parent, "x.bin", FsOp.DELETE, probe=fs)


def test_d2_an_unmodified_authorization_stays_issued_after_use(
    policy: AccessPolicy, fs: FakeFs, real_auth: Authorization
):
    assert is_issued(revalidate(policy, real_auth, probe=fs))
    assert is_issued(real_auth)


# --- C-3: probe failures other than absence deny -------------------------------------------------------


@pytest.mark.parametrize("which", ["target", "parent"])
def test_c3_unknown_existence_denies(policy: AccessPolicy, fs: FakeFs, which: str):
    fs.add(PROJECTS / "p1")
    target = PROJECTS / "p1" / "new" / "x.bin"
    fs.unknown_exists.add(_key(target if which == "target" else target.parent))
    with pytest.raises(AccessDenied, match="existence of .* unknown"):
        authorize(policy, target, FsOp.WRITE, probe=fs)


@pytest.mark.parametrize("op", [FsOp.READ, FsOp.WRITE])
def test_c3_unknown_reparse_state_denies(policy: AccessPolicy, fs: FakeFs, op: FsOp):
    target = fs.add(PROJECTS / "p1" / "a.txt")
    fs.unknown_reparse.add(_key(PROJECTS / "p1"))
    with pytest.raises(AccessDenied, match="reparse state of .* unknown"):
        authorize(policy, target, op, probe=fs)


def test_c3_unknown_reparse_state_of_the_anchor_itself_denies(policy: AccessPolicy, fs: FakeFs):
    target = fs.add(PROJECTS / "p1" / "a.txt")
    fs.unknown_reparse.add(_key(target))
    with pytest.raises(AccessDenied, match="reparse state of .* unknown"):
        authorize(policy, target, FsOp.WRITE, probe=fs)


def _lstat_raising(monkeypatch: pytest.MonkeyPatch, target: Path, exc: BaseException) -> None:
    """Make ``os.lstat`` fail for ONE path and delegate for every other path."""
    real = os.lstat

    def fake(path: str | os.PathLike[str], *args: object, **kwargs: object) -> os.stat_result:
        if os.fspath(path) == str(target):
            raise exc
        return real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "lstat", fake)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (FileNotFoundError(2, "gone"), False),
        (NotADirectoryError(20, "not a directory"), False),
        (PermissionError(13, "denied"), None),
        (OSError(22, "invalid"), None),
        (ValueError("embedded null"), None),
    ],
    ids=["not-found", "not-a-directory", "permission", "other-oserror", "valueerror"],
)
def test_c3_os_probe_lexists_is_tri_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: BaseException, expected: bool | None
):
    target = tmp_path / "t.bin"
    target.write_bytes(b"x")
    assert OsProbe().lexists(str(target)) is True
    _lstat_raising(monkeypatch, target, exc)
    assert OsProbe().lexists(str(target)) is expected
    assert OsProbe().lexists(str(tmp_path / "other.bin")) is False  # plain absence stays False


@pytest.mark.parametrize(
    "exc, expected",
    [
        (FileNotFoundError(2, "gone"), False),
        (NotADirectoryError(20, "not a directory"), False),
        (PermissionError(13, "denied"), None),
        (OSError(22, "invalid"), None),
    ],
    ids=["not-found", "not-a-directory", "permission", "other-oserror"],
)
def test_c3_reparse_probe_is_tri_state_but_the_public_check_keeps_its_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: BaseException, expected: bool | None
):
    target = tmp_path / "t.bin"
    target.write_bytes(b"x")
    assert probe_reparse_point(target) is False and is_reparse_point(target) is False
    _lstat_raising(monkeypatch, target, exc)
    assert probe_reparse_point(target) is expected
    assert OsProbe().is_reparse_point(str(target)) is expected
    assert is_reparse_point(target) is False  # non-guard callers: unchanged (fail-open, tracked)


def test_c3_the_guard_denies_when_the_real_probe_cannot_stat_a_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pol = build_access_policy(
        PathsConfig.model_validate(
            {
                "workspace_root": tmp_path / "ws",
                "ltx_repo": tmp_path / "ltx",
                "ltx_models": tmp_path / "ltx_models",
            }
        )
    )
    folder = tmp_path / "ws" / "projects" / "p1"
    folder.mkdir(parents=True)
    target = folder / "a.txt"
    assert is_issued(authorize(pol, target, FsOp.WRITE))  # baseline: the real probe accepts it
    _lstat_raising(monkeypatch, folder, PermissionError(13, "denied"))
    with pytest.raises(AccessDenied, match="unknown"):
        authorize(pol, target, FsOp.WRITE)
