"""Store-mutation linker — Stage 6.4 (Sprint C6).

What it links
=============

Modern React apps use external state stores (Zustand, Redux Toolkit,
Jotai, Valtio, Nanostores, TanStack Store) where a selector READ in
one file and a setter CALL in another file modify a shared piece of
state — but the two files share NO direct symbol import path.

C3 (whole-import-tree) only resolves the import of the store hook
(``import { useUserStore } from "@/stores/user"``). It does NOT know
that ``useUserStore(s => s.setRole)`` and a sibling component calling
``setRole("admin")`` are the SAME mutation surface as the ``setRole``
defined inside the store's ``create()`` body.

This linker closes the gap by:

  * Building a registry of store DEFINITIONS (per library) with the
    line range of every mutator (setter / reducer / writable atom).
  * Scanning each feature's files for READ sites (selector calls,
    ``useSelector``, ``useAtomValue``) and MUTATION sites (setter
    calls, ``dispatch(setX(...))``, ``useSetAtom``).
  * Emitting a :class:`FrameworkLink` per detected site pointing at
    the mutator's source location with ``link_kind = "store-read"``
    or ``"store-mutation"``.

Determinism
===========

Pure file IO + regex. NO LLM. NO network. Same code → same links.

Failure modes
=============

* No known store library in any workspace's package.json → ``is_active``
  returns False (no work, telemetry records skip reason).
* Store file imports ``set`` from another module (e.g. ``lodash/set``)
  → handled: the registry parser only treats ``set`` as a mutator
  CALL when it appears inside a ``create()`` body OR a slice
  ``reducers`` block.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TYPE_CHECKING

from faultline.framework_linkers.base import FrameworkLink, canonical_sample

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Library detection ───────────────────────────────────────────────────────
# Each library has a tuple of (canonical-name, dep-substring-match).
# We match by dependency NAME substring so monorepo packages that pin
# major versions (``zustand@4.x.x``) still register as "zustand".

_STORE_LIBRARIES: dict[str, tuple[str, ...]] = {
    "zustand": ("zustand",),
    "redux": ("@reduxjs/toolkit", "redux", "react-redux"),
    "jotai": ("jotai",),
    "valtio": ("valtio",),
    "nanostores": ("nanostores", "@nanostores/react"),
    "tanstack-store": ("@tanstack/react-store", "@tanstack/store"),
}

# Files we even bother reading.
_JS_EXTENSIONS: tuple[str, ...] = (
    ".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs",
)


# ── Regex library — store DEFINITIONS ───────────────────────────────────────

# Zustand: `create<T>()((set, get) => ({ ... }))` or
#          `create((set) => ({ ... }))` or
#          `createStore((set, get) => ({ ... }))` (the `zustand/vanilla`
# entrypoint export). We require an arrow-function whose first param is
# `set` to appear within ~200 chars of the `create(` token — this avoids
# false-positives on Jotai's `createStore()` (no `set` arg) and on
# generic factory helpers.
_ZUSTAND_CREATE = re.compile(
    r"""\b(?:create|createStore)\b(?:\s*<[^>]*(?:>[^>]*)?>)?\s*\(""",
    re.VERBOSE,
)
# Validation regex applied to the window AFTER the create token —
# must contain an arrow whose first parameter is `set`.
_ZUSTAND_SET_PARAM = re.compile(
    r"""\(\s*\(?\s*(?:set|_set)\b""",
)

# Export of a store hook from a Zustand-style file:
#   `export const useUserStore = create(...)`
#   `export const userStore = createStore(...)`
_ZUSTAND_EXPORT = re.compile(
    r"""^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*
        (?:create|createStore)\b
    """,
    re.MULTILINE | re.VERBOSE,
)

# Redux Toolkit: `createSlice({ name: 'user', reducers: { setRole: ... } })`
_RTK_SLICE = re.compile(
    r"""\bcreateSlice\s*\(\s*\{""",
)
# Inside the slice we look for the `reducers:` key and capture its block.
_RTK_REDUCERS_BLOCK = re.compile(
    r"""\breducers\s*:\s*\{""",
)

# Jotai: `atom(...)` definitions. We're conservative and only treat as
# "store" the writable-atom form `atom(read, write)` where the second
# arg is a function — that's the mutator surface. Also recognises
# `atomWithReducer` whose action-arg makes it inherently a mutator.
_JOTAI_WRITABLE_ATOM = re.compile(
    r"""\b(?:(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*)?
        atom\b(?:\s*<[^>]*(?:>[^>]*)?>)?\s*\(\s*
        (?:[^,()]+|\([^)]*\)|\{[^}]*\})\s*,\s*   # read arg
        (?:async\s+)?(?:\(|function\b)            # write arg starts
    """,
    re.VERBOSE | re.DOTALL,
)
# Basic read-only atom — emits a "read"-able target so reads can link.
# We allow an optional TS generic argument `atom<Type>(...)` AND a newline
# between `atom<` and the opening paren (real-world code wraps long type
# annotations across multiple lines). We DON'T require `export` — module-
# private atoms (`const aiQueueAtom = atom(...)`) are still valid link
# targets when accessed from the same file. We also accept the common
# `atomWithStorage` / `atomFamily` factories from `jotai/utils`.
_JOTAI_READ_ATOM = re.compile(
    r"""^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*
        (?:atom|atomWithStorage|atomWithReducer|atomFamily|atomWithDefault)\b
        (?:\s*<[^>]*(?:>[^>]*)?>)?\s*\(""",
    re.MULTILINE | re.VERBOSE | re.DOTALL,
)

# Valtio: `proxy({ ... })` exported from a module.
_VALTIO_PROXY = re.compile(
    r"""^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*proxy\s*\(""",
    re.MULTILINE,
)

# Nanostores: `atom(...)` / `map(...)` / `deepMap(...)`.
_NANO_ATOM = re.compile(
    r"""^\s*export\s+(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*
        (?:atom|map|deepMap|computed)\s*\(""",
    re.MULTILINE | re.VERBOSE,
)


# Key-name extractor for `{ key: fn, key2: (state) => ... }` blocks.
# Captures the first identifier before a colon at the start of a line
# (with optional indent). Used for Zustand returned-object keys and RTK
# `reducers:` block keys.
_OBJECT_KEY_FN = re.compile(
    r"""^\s*([A-Za-z_$][\w$]*)\s*:\s*
        (?:async\s+)?
        (?:\([^)]*\)\s*=>|function\b)
    """,
    re.MULTILINE | re.VERBOSE,
)


# ── Regex library — CALL sites ──────────────────────────────────────────────

# Zustand selector READ:  useUserStore(s => s.role)  or  useUserStore()
_ZUSTAND_HOOK_CALL = re.compile(
    r"""\b([A-Za-z_$][\w$]*Store)\s*\(""",
)

# Redux selector READ: useSelector(state => state.user.role)
_REDUX_USESELECTOR = re.compile(
    r"""\buseSelector\s*\(""",
)

# Redux mutation: dispatch(actionCreator(...))  -- we match the inner
# action creator identifier so we can link it back to the slice that
# defined it.
_REDUX_DISPATCH_CALL = re.compile(
    r"""\bdispatch\s*\(\s*([A-Za-z_$][\w$]*)\s*\(""",
)

# Jotai consumers
_JOTAI_USE_ATOM_VALUE = re.compile(
    r"""\buseAtomValue\s*\(\s*([A-Za-z_$][\w$]*)""",
)
_JOTAI_USE_SET_ATOM = re.compile(
    r"""\buseSetAtom\s*\(\s*([A-Za-z_$][\w$]*)""",
)
_JOTAI_USE_ATOM = re.compile(
    r"""\buseAtom\s*\(\s*([A-Za-z_$][\w$]*)""",
)

# Nanostores
_NANO_USE_STORE = re.compile(
    r"""\b(?:useStore|\$)\s*\(\s*([A-Za-z_$][\w$]*)""",
)

# Import-statement parser — used to know which local name maps to which
# imported store identifier (e.g. `import { useUserStore } from "@/stores/user"`).
_IMPORT_NAMED = re.compile(
    r"""^\s*import\s*\{([^}]+)\}\s*from\s*['"]([^'"]+)['"]""",
    re.MULTILINE,
)


# ── Internal types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _StoreMutator:
    """One callable mutation surface inside a store-definition file."""

    module_file: str          # repo-relative POSIX
    store_name: str           # e.g. "useUserStore" / "userSlice" / "userAtom"
    mutator: str              # e.g. "setRole" / "reset"  -- "" if whole-store
    line_start: int
    line_end: int
    library: str              # "zustand" | "redux" | "jotai" | ...


@dataclass(frozen=True)
class _StoreField:
    """One readable field inside a store-definition file.

    For Zustand we point reads at the store's exported hook (first line
    of the export) so consumers can see "which file owns this state".
    For Jotai/Valtio reads we point at the atom/proxy declaration.
    """

    module_file: str
    store_name: str
    line_start: int
    line_end: int
    library: str


@dataclass
class _LinkerTelemetry:
    """Per-scan counters surfaced into the Stage 6.4 artifact."""

    libraries_detected: list[str] = field(default_factory=list)
    store_files_count: int = 0
    store_registry_size: int = 0
    read_sites_found: int = 0
    mutation_sites_found: int = 0
    links_emitted: int = 0
    links_attached: int = 0
    features_processed: int = 0
    files_scanned: int = 0
    files_unreadable: int = 0
    unmatched: int = 0
    sample_links: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "libraries_detected": sorted(self.libraries_detected),
            "store_files_count": self.store_files_count,
            "store_registry_size": self.store_registry_size,
            "read_sites_found": self.read_sites_found,
            "mutation_sites_found": self.mutation_sites_found,
            "links_emitted": self.links_emitted,
            "links_attached": self.links_attached,
            "features_processed": self.features_processed,
            "files_scanned": self.files_scanned,
            "files_unreadable": self.files_unreadable,
            "unmatched": self.unmatched,
            "sample_links": canonical_sample(self.sample_links, 10),
        }


# ── Helpers ────────────────────────────────────────────────────────────────


@lru_cache(maxsize=4096)
def _read_text_cached(abs_path: str) -> str | None:
    try:
        with open(abs_path, "r", encoding="utf-8") as fp:
            return fp.read()
    except (OSError, UnicodeDecodeError):
        return None


def _detect_libraries(ctx: "ScanContext") -> set[str]:
    """Scan ctx.workspaces' package.json for known store libs.

    Returns canonical library names (zustand / redux / jotai / valtio /
    nanostores / tanstack-store).
    """
    found: set[str] = set()

    def _collect_deps(pkg: dict[str, object]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            d = pkg.get(key)
            if isinstance(d, dict):
                out.update(d)
        return out

    workspaces = getattr(ctx, "workspaces", None) or []
    for ws in workspaces:
        pkg = getattr(ws, "package_json", None)
        if not isinstance(pkg, dict):
            continue
        deps = _collect_deps(pkg)
        if not deps:
            continue
        for lib_name, needles in _STORE_LIBRARIES.items():
            if lib_name in found:
                continue
            for dep_name in deps:
                if not isinstance(dep_name, str):
                    continue
                if dep_name in needles:
                    found.add(lib_name)
                    break

    # Fallback: scan ALL tracked `package.json` files. This catches two
    # important shapes:
    #   * mono-app repos where Stage 0 didn't declare workspaces
    #     (e.g. infisical with `frontend/package.json`).
    #   * monorepos whose Stage-0 workspace enumeration missed a package
    #     for any reason.
    repo_path = getattr(ctx, "repo_path", None)
    tracked = getattr(ctx, "tracked_files", None) or ()
    if repo_path is not None:
        import json as _json
        for rel in tracked:
            posix = str(rel).replace("\\", "/")
            if not posix.endswith("package.json"):
                continue
            if "/node_modules/" in "/" + posix:
                continue
            text = _read_text_cached(str(repo_path / posix))
            if text is None:
                continue
            try:
                pkg = _json.loads(text)
            except (ValueError, TypeError):
                continue
            if not isinstance(pkg, dict):
                continue
            deps = _collect_deps(pkg)
            if not deps:
                continue
            for lib_name, needles in _STORE_LIBRARIES.items():
                if lib_name in found:
                    continue
                for dep_name in deps:
                    if isinstance(dep_name, str) and dep_name in needles:
                        found.add(lib_name)
                        break
            # Short-circuit when we've found every known lib.
            if len(found) == len(_STORE_LIBRARIES):
                break

    return found


def _line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _detect_zustand_store(rel: str, text: str) -> tuple[list[_StoreMutator], list[_StoreField]]:
    """Parse a Zustand store-definition file into mutators + fields.

    Returns ([] , []) when the file is not a Zustand store.
    """
    # Cheap pre-filter: must mention `create(` AND have a `(set`/`set,`
    # within the next ~200 chars — otherwise we'd false-positive on every
    # factory helper or on Jotai's `createStore()` (no `set` arg).
    create_m = _ZUSTAND_CREATE.search(text)
    if not create_m:
        return [], []
    # Look at the window starting at the create token. We use search (not
    # match) because the matched text already includes the opening paren
    # and possibly an arrow signature like `create<T>()((set,...) => ...`.
    window = text[create_m.start():create_m.start() + 400]
    if not _ZUSTAND_SET_PARAM.search(window):
        return [], []

    exports = list(_ZUSTAND_EXPORT.finditer(text))
    if not exports:
        return [], []

    # Find the body of each `create(...)` call. Conservative: pick the
    # outermost `({ ... })` that follows the create token.
    mutators: list[_StoreMutator] = []
    fields: list[_StoreField] = []
    for exp in exports:
        store_name = exp.group(1)
        decl_line = _line_no(text, exp.start())
        # Find a body opening `{` after the `set` keyword in the
        # parameter list. We look for the first `({` AFTER the `=>`.
        rest = text[exp.start():]
        arrow_idx = rest.find("=>")
        if arrow_idx < 0:
            continue
        body_start_rel = rest.find("(", arrow_idx)
        if body_start_rel < 0:
            body_start_rel = rest.find("{", arrow_idx)
        if body_start_rel < 0:
            continue
        body_abs_start = exp.start() + body_start_rel
        # Balance braces to find the body end.
        depth = 0
        body_end = body_abs_start
        for i in range(body_abs_start, min(len(text), body_abs_start + 50_000)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    body_end = i + 1
                    break
        body_text = text[body_abs_start:body_end]
        if not body_text:
            continue
        # Mutator keys: `name: (args) => ...` that mention `set(` in their body.
        # We scan key-fn objects at any depth (Zustand `slices` pattern nests).
        for km in _OBJECT_KEY_FN.finditer(body_text):
            mutator_name = km.group(1)
            # Skip selector helpers — they have no `set(`/`set({` call in body.
            # We look in the ~300 chars following the key for `set(` or `set({`.
            local_window = body_text[km.start(): km.start() + 400]
            if not re.search(r"\bset\s*\(", local_window):
                # No mutation — treat as a readable field instead.
                continue
            abs_offset = body_abs_start + km.start()
            mut_line = _line_no(text, abs_offset)
            mutators.append(_StoreMutator(
                module_file=rel,
                store_name=store_name,
                mutator=mutator_name,
                line_start=mut_line,
                line_end=mut_line,
                library="zustand",
            ))
        # The hook itself is a readable field surface — point reads at
        # the export declaration line.
        fields.append(_StoreField(
            module_file=rel,
            store_name=store_name,
            line_start=decl_line,
            line_end=decl_line,
            library="zustand",
        ))
    return mutators, fields


def _detect_redux_slice(rel: str, text: str) -> tuple[list[_StoreMutator], list[_StoreField]]:
    """Parse Redux Toolkit slice into mutators (reducer keys) + fields."""
    if not _RTK_SLICE.search(text):
        return [], []

    mutators: list[_StoreMutator] = []
    # We find each `createSlice({ ... })` and inside it the `reducers: { ... }` block.
    for slice_m in _RTK_SLICE.finditer(text):
        # Balance braces from the opening `{`.
        open_idx = text.find("{", slice_m.end() - 1)
        if open_idx < 0:
            continue
        depth = 0
        slice_end = open_idx
        for i in range(open_idx, min(len(text), open_idx + 80_000)):
            ch = text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    slice_end = i + 1
                    break
        slice_text = text[open_idx:slice_end]
        # Find `name: 'user'` for the store_name.
        name_m = re.search(r"""name\s*:\s*['"]([^'"]+)['"]""", slice_text)
        store_name = (name_m.group(1) + "Slice") if name_m else "slice"
        # Find the `reducers: { ... }` sub-block.
        red_m = _RTK_REDUCERS_BLOCK.search(slice_text)
        if not red_m:
            continue
        red_open_rel = slice_text.find("{", red_m.end() - 1)
        if red_open_rel < 0:
            continue
        depth = 0
        red_end_rel = red_open_rel
        for i in range(red_open_rel, len(slice_text)):
            ch = slice_text[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    red_end_rel = i + 1
                    break
        red_text = slice_text[red_open_rel:red_end_rel]
        red_abs = open_idx + red_open_rel
        # Reducer keys: `setRole: (state, action) => ...` or `setRole(state, action) { ... }`.
        reducer_re = re.compile(
            r"""^\s*([A-Za-z_$][\w$]*)\s*(?:\([^)]*\)\s*\{|\:)""",
            re.MULTILINE,
        )
        for km in reducer_re.finditer(red_text):
            mutator_name = km.group(1)
            if mutator_name in ("name", "initialState", "reducers", "extraReducers"):
                continue
            abs_offset = red_abs + km.start()
            line_no = _line_no(text, abs_offset)
            mutators.append(_StoreMutator(
                module_file=rel,
                store_name=store_name,
                mutator=mutator_name,
                line_start=line_no,
                line_end=line_no,
                library="redux",
            ))
    return mutators, []


def _detect_jotai_atoms(rel: str, text: str) -> tuple[list[_StoreMutator], list[_StoreField]]:
    """Parse Jotai atom definitions into mutators (writable) + read fields."""
    mutators: list[_StoreMutator] = []
    fields: list[_StoreField] = []

    # Writable atoms: `export const setRoleAtom = atom(get => ..., (get, set, v) => set(...))`
    for m in _JOTAI_WRITABLE_ATOM.finditer(text):
        atom_name = m.group(1)
        if not atom_name:
            continue
        line_no = _line_no(text, m.start())
        mutators.append(_StoreMutator(
            module_file=rel,
            store_name=atom_name,
            mutator=atom_name,
            line_start=line_no,
            line_end=line_no,
            library="jotai",
        ))
        fields.append(_StoreField(
            module_file=rel,
            store_name=atom_name,
            line_start=line_no,
            line_end=line_no,
            library="jotai",
        ))

    # Read-only atoms: only register as fields, not mutators.
    seen_names = {m.store_name for m in mutators}
    for m in _JOTAI_READ_ATOM.finditer(text):
        atom_name = m.group(1)
        if atom_name in seen_names:
            continue
        line_no = _line_no(text, m.start())
        fields.append(_StoreField(
            module_file=rel,
            store_name=atom_name,
            line_start=line_no,
            line_end=line_no,
            library="jotai",
        ))
    return mutators, fields


def _detect_valtio_proxies(rel: str, text: str) -> tuple[list[_StoreMutator], list[_StoreField]]:
    """Valtio proxies — every exported proxy is both readable and mutable."""
    mutators: list[_StoreMutator] = []
    fields: list[_StoreField] = []
    for m in _VALTIO_PROXY.finditer(text):
        proxy_name = m.group(1)
        line_no = _line_no(text, m.start())
        mutators.append(_StoreMutator(
            module_file=rel,
            store_name=proxy_name,
            mutator=proxy_name,
            line_start=line_no,
            line_end=line_no,
            library="valtio",
        ))
        fields.append(_StoreField(
            module_file=rel,
            store_name=proxy_name,
            line_start=line_no,
            line_end=line_no,
            library="valtio",
        ))
    return mutators, fields


def _detect_nanostores(rel: str, text: str) -> tuple[list[_StoreMutator], list[_StoreField]]:
    """Nanostores `atom()` / `map()` exports — readable and mutable."""
    mutators: list[_StoreMutator] = []
    fields: list[_StoreField] = []
    for m in _NANO_ATOM.finditer(text):
        atom_name = m.group(1)
        line_no = _line_no(text, m.start())
        mutators.append(_StoreMutator(
            module_file=rel,
            store_name=atom_name,
            mutator=atom_name,
            line_start=line_no,
            line_end=line_no,
            library="nanostores",
        ))
        fields.append(_StoreField(
            module_file=rel,
            store_name=atom_name,
            line_start=line_no,
            line_end=line_no,
            library="nanostores",
        ))
    return mutators, fields


# ── Linker class ────────────────────────────────────────────────────────────


class StoreMutationLinker:
    """Resolves Zustand / Redux / Jotai / Valtio / Nanostores call sites."""

    name: str = "store-mutation"
    activation_keys: tuple[str, ...] = (
        "zustand-active", "redux-active", "jotai-active",
        "valtio-active", "nanostores-active", "tanstack-store-active",
    )

    def __init__(self) -> None:
        self._libraries: set[str] | None = None
        # Indexed by store-hook/identifier name -> list of mutators/fields.
        self._mutators_by_store: dict[str, list[_StoreMutator]] | None = None
        self._fields_by_store: dict[str, list[_StoreField]] | None = None
        # All RTK action-creator names indexed by name -> mutator entry.
        self._rtk_action_names: dict[str, _StoreMutator] | None = None
        # All store files (any library) — used to skip them during call-site scan.
        self._store_files: set[str] | None = None
        self.telemetry: _LinkerTelemetry = _LinkerTelemetry()

    # ── Activation ──────────────────────────────────────────────────────

    def is_active(self, ctx: "ScanContext") -> bool:
        libs = self._libraries
        if libs is None:
            libs = _detect_libraries(ctx)
            self._libraries = libs
            self.telemetry.libraries_detected = sorted(libs)
        return bool(libs)

    # ── Registry build ──────────────────────────────────────────────────

    def _ensure_registry(self, ctx: "ScanContext") -> None:
        if self._mutators_by_store is not None:
            return

        libs = self._libraries if self._libraries is not None else _detect_libraries(ctx)
        self._libraries = libs
        self.telemetry.libraries_detected = sorted(libs)

        mutators_by_store: dict[str, list[_StoreMutator]] = {}
        fields_by_store: dict[str, list[_StoreField]] = {}
        rtk_action_names: dict[str, _StoreMutator] = {}
        store_files: set[str] = set()

        for rel in getattr(ctx, "tracked_files", ()) or ():
            posix = rel.replace("\\", "/")
            if not posix.endswith(_JS_EXTENSIONS):
                continue
            # Cheap path-based filter — anything under node_modules is ignored.
            if "/node_modules/" in "/" + posix:
                continue
            abs_path = str(ctx.repo_path / posix)
            text = _read_text_cached(abs_path)
            if text is None:
                continue

            file_mutators: list[_StoreMutator] = []
            file_fields: list[_StoreField] = []
            if "zustand" in libs:
                m, f = _detect_zustand_store(posix, text)
                file_mutators.extend(m)
                file_fields.extend(f)
            if "redux" in libs:
                m, f = _detect_redux_slice(posix, text)
                file_mutators.extend(m)
                file_fields.extend(f)
            if "jotai" in libs:
                m, f = _detect_jotai_atoms(posix, text)
                file_mutators.extend(m)
                file_fields.extend(f)
            if "valtio" in libs:
                m, f = _detect_valtio_proxies(posix, text)
                file_mutators.extend(m)
                file_fields.extend(f)
            if "nanostores" in libs or "tanstack-store" in libs:
                m, f = _detect_nanostores(posix, text)
                file_mutators.extend(m)
                file_fields.extend(f)

            if not file_mutators and not file_fields:
                continue

            store_files.add(posix)
            for entry in file_mutators:
                mutators_by_store.setdefault(entry.store_name, []).append(entry)
                if entry.library == "redux":
                    rtk_action_names[entry.mutator] = entry
            for entry in file_fields:
                fields_by_store.setdefault(entry.store_name, []).append(entry)

        self._mutators_by_store = mutators_by_store
        self._fields_by_store = fields_by_store
        self._rtk_action_names = rtk_action_names
        self._store_files = store_files
        self.telemetry.store_files_count = len(store_files)
        # Registry size = number of distinct (store, mutator) pairs.
        size = sum(len(v) for v in mutators_by_store.values())
        size += sum(len(v) for v in fields_by_store.values())
        self.telemetry.store_registry_size = size

    # ── Public surface ─────────────────────────────────────────────────

    def link_for_feature(
        self,
        feature: "Feature",
        ctx: "ScanContext",
        log: "StageLogger",
    ) -> list[FrameworkLink]:
        if not self.is_active(ctx):
            return []
        self._ensure_registry(ctx)
        self.telemetry.features_processed += 1

        assert self._mutators_by_store is not None
        assert self._fields_by_store is not None
        assert self._rtk_action_names is not None
        assert self._store_files is not None
        if not self._mutators_by_store and not self._fields_by_store:
            return []

        caller_files = self._caller_files(feature)
        if not caller_files:
            return []

        links: list[FrameworkLink] = []
        for rel in caller_files:
            abs_path = str(ctx.repo_path / rel)
            text = _read_text_cached(abs_path)
            if text is None:
                self.telemetry.files_unreadable += 1
                continue
            self.telemetry.files_scanned += 1
            links.extend(self._scan_file(rel, text))

        self.telemetry.links_emitted += len(links)
        return links

    # ── Internals ──────────────────────────────────────────────────────

    @staticmethod
    def _caller_files(feature: "Feature") -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for attr in (feature.symbol_attributions or []):
            f = (attr.file or "").replace("\\", "/")
            if f and f not in seen:
                seen.add(f)
                out.append(f)
        for f in (feature.paths or []):
            f = (f or "").replace("\\", "/")
            if not f:
                continue
            if "." not in f.rsplit("/", 1)[-1]:
                continue
            if f in seen:
                continue
            seen.add(f)
            out.append(f)
        return out

    def _scan_file(self, rel: str, text: str) -> list[FrameworkLink]:
        """Detect read + mutation call sites in a single caller file."""
        assert self._mutators_by_store is not None
        assert self._fields_by_store is not None
        assert self._rtk_action_names is not None
        assert self._store_files is not None

        results: list[FrameworkLink] = []

        # 1. Identify which store identifiers are even in scope here.
        #    We look at `import { useUserStore, userAtom, setRoleAction } from "..."`.
        local_to_store: dict[str, str] = {}
        for m in _IMPORT_NAMED.finditer(text):
            block = m.group(1)
            for raw in block.split(","):
                raw = raw.strip()
                if not raw or raw.startswith("type "):
                    continue
                if " as " in raw:
                    orig, alias = (p.strip() for p in raw.split(" as ", 1))
                else:
                    orig = alias = raw
                # Skip type-only `import { type X }` markers in the alias position.
                if orig.startswith("type "):
                    continue
                if (
                    orig in self._mutators_by_store
                    or orig in self._fields_by_store
                    or orig in self._rtk_action_names
                ):
                    local_to_store[alias] = orig

        # 2. Zustand: `useUserStore(...)` calls.
        for m in _ZUSTAND_HOOK_CALL.finditer(text):
            hook_name = m.group(1)
            real_name = local_to_store.get(hook_name, hook_name)
            line_no = _line_no(text, m.start())
            # READ link to the store hook declaration.
            for field_entry in self._fields_by_store.get(real_name, ()):
                if field_entry.library != "zustand":
                    continue
                self.telemetry.read_sites_found += 1
                link = FrameworkLink(
                    source_file=rel,
                    source_symbol=_enclosing_symbol(text.splitlines(), line_no),
                    source_line=line_no,
                    target_file=field_entry.module_file,
                    target_symbol=field_entry.store_name,
                    target_line_start=field_entry.line_start,
                    target_line_end=field_entry.line_end,
                    linker=self.name,
                    link_kind="store-read",
                    confidence=0.9,
                    reason=f"zustand selector read on {field_entry.store_name}",
                )
                results.append(link)
                self._record_sample(link)
                break  # one read link per call site is enough

        # 3. Zustand mutator call sites: `setRole("admin")` where `setRole`
        #    is a known mutator name in some store. We scan the source for
        #    every known mutator identifier appearing as a call.
        all_zustand_mutators: dict[str, _StoreMutator] = {}
        for entries in self._mutators_by_store.values():
            for e in entries:
                if e.library == "zustand":
                    all_zustand_mutators.setdefault(e.mutator, e)
        if all_zustand_mutators:
            names_alt = "|".join(re.escape(n) for n in all_zustand_mutators)
            mut_re = re.compile(rf"\b({names_alt})\s*\(")
            for m in mut_re.finditer(text):
                name = m.group(1)
                entry = all_zustand_mutators[name]
                line_no = _line_no(text, m.start())
                # Avoid linking calls inside the store's own definition file.
                if rel == entry.module_file:
                    continue
                self.telemetry.mutation_sites_found += 1
                link = FrameworkLink(
                    source_file=rel,
                    source_symbol=_enclosing_symbol(text.splitlines(), line_no),
                    source_line=line_no,
                    target_file=entry.module_file,
                    target_symbol=entry.mutator,
                    target_line_start=entry.line_start,
                    target_line_end=entry.line_end,
                    linker=self.name,
                    link_kind="store-mutation",
                    confidence=0.7,
                    reason=(
                        f"zustand setter call to {entry.mutator} on "
                        f"{entry.store_name}"
                    ),
                )
                results.append(link)
                self._record_sample(link)

        # 4. Redux: `dispatch(setRole(...))`.
        for m in _REDUX_DISPATCH_CALL.finditer(text):
            action_name = m.group(1)
            entry = self._rtk_action_names.get(action_name)
            if entry is None:
                self.telemetry.unmatched += 1
                continue
            line_no = _line_no(text, m.start())
            self.telemetry.mutation_sites_found += 1
            link = FrameworkLink(
                source_file=rel,
                source_symbol=_enclosing_symbol(text.splitlines(), line_no),
                source_line=line_no,
                target_file=entry.module_file,
                target_symbol=entry.mutator,
                target_line_start=entry.line_start,
                target_line_end=entry.line_end,
                linker=self.name,
                link_kind="store-mutation",
                confidence=0.9,
                reason=f"redux dispatch({action_name}(...))",
            )
            results.append(link)
            self._record_sample(link)

        # Redux: useSelector(state => state.X.Y) — best-effort link to
        # ANY redux slice in registry. We attach as "store-read" but the
        # target is the slice file (best we can do without TS types).
        for m in _REDUX_USESELECTOR.finditer(text):
            line_no = _line_no(text, m.start())
            # Pick the first redux slice as the read target.
            slice_entry: _StoreMutator | None = None
            for entry in self._rtk_action_names.values():
                slice_entry = entry
                break
            if slice_entry is None:
                continue
            self.telemetry.read_sites_found += 1
            link = FrameworkLink(
                source_file=rel,
                source_symbol=_enclosing_symbol(text.splitlines(), line_no),
                source_line=line_no,
                target_file=slice_entry.module_file,
                target_symbol=slice_entry.store_name,
                target_line_start=slice_entry.line_start,
                target_line_end=slice_entry.line_end,
                linker=self.name,
                link_kind="store-read",
                confidence=0.5,
                reason="useSelector read against redux slice",
            )
            results.append(link)
            self._record_sample(link)

        # 5. Jotai consumers.
        for pat, kind, conf in (
            (_JOTAI_USE_ATOM_VALUE, "store-read", 0.95),
            (_JOTAI_USE_ATOM, "store-read", 0.85),
            (_JOTAI_USE_SET_ATOM, "store-mutation", 0.95),
        ):
            for m in pat.finditer(text):
                atom_name = m.group(1)
                resolved = local_to_store.get(atom_name, atom_name)
                line_no = _line_no(text, m.start())
                # Prefer mutator entry for set-atom; field entry for reads.
                target: _StoreMutator | _StoreField | None = None
                if kind == "store-mutation":
                    mutators = self._mutators_by_store.get(resolved) or []
                    for entry in mutators:
                        if entry.library == "jotai":
                            target = entry
                            break
                else:
                    fields = self._fields_by_store.get(resolved) or []
                    for entry in fields:
                        if entry.library == "jotai":
                            target = entry
                            break
                if target is None:
                    self.telemetry.unmatched += 1
                    continue
                if kind == "store-mutation":
                    self.telemetry.mutation_sites_found += 1
                    target_symbol = target.mutator  # type: ignore[union-attr]
                else:
                    self.telemetry.read_sites_found += 1
                    target_symbol = target.store_name
                link = FrameworkLink(
                    source_file=rel,
                    source_symbol=_enclosing_symbol(text.splitlines(), line_no),
                    source_line=line_no,
                    target_file=target.module_file,
                    target_symbol=target_symbol,
                    target_line_start=target.line_start,
                    target_line_end=target.line_end,
                    linker=self.name,
                    link_kind=kind,
                    confidence=conf,
                    reason=f"jotai {kind} on {resolved}",
                )
                results.append(link)
                self._record_sample(link)

        # 6. Nanostores: `useStore(myAtom)` / `$myAtom(...)`.
        for m in _NANO_USE_STORE.finditer(text):
            atom_name = m.group(1)
            resolved = local_to_store.get(atom_name, atom_name)
            fields = self._fields_by_store.get(resolved) or []
            target = None
            for entry in fields:
                if entry.library == "nanostores":
                    target = entry
                    break
            if target is None:
                continue
            line_no = _line_no(text, m.start())
            self.telemetry.read_sites_found += 1
            link = FrameworkLink(
                source_file=rel,
                source_symbol=_enclosing_symbol(text.splitlines(), line_no),
                source_line=line_no,
                target_file=target.module_file,
                target_symbol=target.store_name,
                target_line_start=target.line_start,
                target_line_end=target.line_end,
                linker=self.name,
                link_kind="store-read",
                confidence=0.85,
                reason=f"nanostores useStore({resolved})",
            )
            results.append(link)
            self._record_sample(link)

        return results

    def _record_sample(self, link: FrameworkLink) -> None:
        # Uncapped append from worker threads; the cap + canonical order
        # are applied at ``as_dict`` emission (base.canonical_sample).
        self.telemetry.sample_links.append({
            "source": f"{link.source_file}:{link.source_line}",
            "target": f"{link.target_file}:{link.target_symbol}:L{link.target_line_start}",
            "kind": link.link_kind,
        })


# ── Enclosing-symbol heuristic (same idiom as C4/C5) ───────────────────────


_FN_DECL = re.compile(
    r"""^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?
        (?:function\s+([A-Za-z_$][\w$]*)
         |const\s+([A-Za-z_$][\w$]*)\s*=
         |let\s+([A-Za-z_$][\w$]*)\s*=
        )""",
    re.VERBOSE,
)


def _enclosing_symbol(lines: list[str], call_line: int) -> str:
    start = max(1, call_line - 200)
    for ln in range(call_line - 1, start - 1, -1):
        if ln <= 0 or ln > len(lines):
            continue
        line = lines[ln - 1]
        m = _FN_DECL.match(line)
        if m:
            for grp in m.groups():
                if grp:
                    return grp
    return "<module>"


__all__ = ["StoreMutationLinker"]
