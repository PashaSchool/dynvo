r"""tRPC procedure linker — Stage 6.4 (Sprint D1, C7).

What it links
=============

tRPC creates a typed RPC client where call sites look like:

    trpc.user.create.useMutation()
    trpc.user.getById.useQuery({id})
    api.checker.testHttp.mutate({...})           // openstatus convention
    trpc.admin.users.list.query()                // nested 3+ levels

…but the actual procedure body lives in a server-side router file:

    // packages/api/src/router/user.ts
    export const userRouter = createTRPCRouter({
      create: publicProcedure.input(...).mutation(async ({input}) => {...}),
      getById: publicProcedure.query(...),
    })

C3 (whole-import-tree) only resolves the IMPORT of the tRPC CLIENT
wrapper (``import { trpc } from "@/trpc/client"``) — the dotted call
path is a string of property accesses through a Proxy, opaque to the
import graph. This linker:

  * Discovers every router definition file by regex
    (``createTRPCRouter\({...})`` / ``router\({...})``) and parses the
    nested key tree into a procedure path table.
  * Maps each procedure path (``user.create``) to the file + line range
    of its handler body.
  * Detects call sites of the form
    ``<clientId>.<path>.<verb>(``  where verb ∈ {useQuery, useMutation,
    query, mutate, useSubscription, subscribe} and ``clientId`` is any
    of the configured tRPC client identifiers (``trpc``, ``api``,
    ``client``, ``t``).
  * Emits a :class:`FrameworkLink` per matched call site.

Pipeline gap closed
===================

Without this, "change in user.create" never shows up as "affects flow X
that uses it" because the import tree stops at the client wrapper.

Determinism
===========

Pure file IO + regex. NO LLM. NO network. Same code → same links.

Failure modes
=============

* No ``@trpc/*`` dependency in any workspace package.json → ``is_active``
  returns False (no work done, telemetry records skip reason).
* Router file uses dynamic spread (``...otherRouter``) → those keys are
  flagged as ``unresolved_spread`` in telemetry but the rest still work.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from faultline.framework_linkers.base import FrameworkLink

if TYPE_CHECKING:
    from faultline.models.types import Feature
    from faultline.pipeline_v2.run_logger import StageLogger
    from faultline.pipeline_v2.stage_0_intake import ScanContext


logger = logging.getLogger(__name__)


# ── Activation gates ────────────────────────────────────────────────────────

_TRPC_DEP_PREFIXES: tuple[str, ...] = ("@trpc/",)

# Client identifiers we recognise as the entry-point object. Restricted
# list — broader matches false-positive on every Math.X.Y in the codebase.
_TRPC_CLIENT_IDENTIFIERS: tuple[str, ...] = ("trpc", "api", "client", "t")

# Verbs that conclusively mark a tRPC call site (vs an arbitrary
# `something.X.Y()`).
_TRPC_VERBS: tuple[str, ...] = (
    "useQuery", "useMutation", "useSubscription", "useInfiniteQuery",
    "query", "mutate", "subscribe", "fetch", "prefetch", "invalidate",
)


# ── Regex library ───────────────────────────────────────────────────────────

# Find every router definition. We capture (router-var, full-source).
# The full-source parse happens after we slice to the matching brace.
_ROUTER_DEF = re.compile(
    r"""
    (?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*
    (?:createTRPCRouter|router)\s*\(\s*\{
    """,
    re.VERBOSE,
)

# Procedure markers within a router body — used as evidence a key holds
# a leaf procedure rather than a sub-router.
_PROCEDURE_MARKER = re.compile(
    r"""\b(publicProcedure|protectedProcedure|adminProcedure|procedure|t\.procedure)\b"""
)
# Sub-router reference — used to chain composed routers.
_SUB_ROUTER_REF = re.compile(r"""^\s*([A-Za-z_$][\w$]*)Router\s*[,}]""")

# Call sites: <client>.<path>.<verb>(
# Build the verb alternation once.
_VERB_ALT = "|".join(re.escape(v) for v in _TRPC_VERBS)
_CLIENT_ALT = "|".join(re.escape(c) for c in _TRPC_CLIENT_IDENTIFIERS)
_CALL_SITE = re.compile(
    rf"""\b({_CLIENT_ALT})\.([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+)\.({_VERB_ALT})\s*\(""",
)


# ── Internal types ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ProcedureEntry:
    """One leaf procedure with file + line span."""

    file: str        # repo-relative POSIX
    symbol: str      # dotted path like "user.create"
    line_start: int
    line_end: int


@dataclass
class _LinkerTelemetry:
    """Per-scan counters surfaced into the Stage 6.4 artifact."""

    router_map_size: int = 0
    router_files_parsed: int = 0
    procedure_call_sites_found: int = 0
    links_emitted: int = 0
    unmatched_call_sites: int = 0
    features_processed: int = 0
    files_scanned: int = 0
    files_unreadable: int = 0
    unmatched_paths_sample: list[str] = field(default_factory=list)
    sample_links: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "router_map_size": self.router_map_size,
            "router_files_parsed": self.router_files_parsed,
            "procedure_call_sites_found": self.procedure_call_sites_found,
            "links_emitted": self.links_emitted,
            "unmatched_call_sites": self.unmatched_call_sites,
            "features_processed": self.features_processed,
            "files_scanned": self.files_scanned,
            "files_unreadable": self.files_unreadable,
            "unmatched_paths_sample": list(self.unmatched_paths_sample),
            "sample_links": list(self.sample_links),
        }


# ── Helpers ────────────────────────────────────────────────────────────────


@lru_cache(maxsize=4096)
def _read_text_cached(abs_path: str) -> str | None:
    try:
        with open(abs_path, "r", encoding="utf-8") as fp:
            return fp.read()
    except (OSError, UnicodeDecodeError):
        return None


def _find_matching_brace(text: str, open_idx: int) -> int:
    """Return the index just past the matching ``}`` for the ``{`` at ``open_idx``.

    Skips braces inside strings (single, double, template literals) and
    line / block comments. Returns ``-1`` when no match is found.
    """
    depth = 0
    i = open_idx
    n = len(text)
    in_str: str | None = None  # quote char or None
    in_line_comment = False
    in_block_comment = False
    tpl_brace_stack: list[int] = []

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            i += 1
            continue
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if in_str == "`" and ch == "$" and nxt == "{":
                tpl_brace_stack.append(depth)
                in_str = None
                i += 2
                continue
            if ch == in_str:
                in_str = None
                i += 1
                continue
            i += 1
            continue
        # not in string / comment
        if ch == "/" and nxt == "/":
            in_line_comment = True
            i += 2
            continue
        if ch == "/" and nxt == "*":
            in_block_comment = True
            i += 2
            continue
        if ch in ("'", '"', "`"):
            in_str = ch
            i += 1
            continue
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            if tpl_brace_stack and depth == tpl_brace_stack[-1]:
                # Closing a ${...} interpolation — back to template string.
                tpl_brace_stack.pop()
                in_str = "`"
                i += 1
                continue
            depth -= 1
            if depth == 0:
                return i + 1
            i += 1
            continue
        i += 1
    return -1


def _split_top_level_object(body: str) -> list[tuple[str, str]]:
    """Parse a JS object literal body (between ``{`` and ``}``) into key/value pairs.

    Returns ``[(key, value), ...]``. Values are raw substrings. Handles
    nested braces / parens / brackets and string literals.
    """
    items: list[tuple[str, str]] = []
    n = len(body)
    i = 0

    def _skip_trivia(j: int, eat_commas: bool = True) -> int:
        while j < n:
            c = body[j]
            if c.isspace():
                j += 1
                continue
            if eat_commas and c == ",":
                j += 1
                continue
            if c == "/" and j + 1 < n and body[j + 1] == "/":
                while j < n and body[j] != "\n":
                    j += 1
                continue
            if c == "/" and j + 1 < n and body[j + 1] == "*":
                j += 2
                while j + 1 < n and not (body[j] == "*" and body[j + 1] == "/"):
                    j += 1
                j += 2
                continue
            return j
        return j

    while i < n:
        i = _skip_trivia(i)
        if i >= n:
            break
        # Skip spread elements: `...foo,`
        if body.startswith("...", i):
            # find next comma or end at top level
            depth = 0
            j = i + 3
            while j < n:
                c = body[j]
                if c in "({[":
                    depth += 1
                elif c in ")}]":
                    depth -= 1
                elif c == "," and depth == 0:
                    break
                j += 1
            i = j + 1
            continue
        # Key
        if body[i] in ("'", '"', "`"):
            quote = body[i]
            j = i + 1
            while j < n and body[j] != quote:
                if body[j] == "\\":
                    j += 2
                    continue
                j += 1
            key = body[i + 1: j]
            i = j + 1
        else:
            j = i
            while j < n and (body[j].isalnum() or body[j] in "_$"):
                j += 1
            if j == i:
                # Unknown token — bail.
                break
            key = body[i: j]
            i = j
        # After reading the key, DO NOT eat commas yet — that's how we
        # distinguish shorthand (`foo, bar`) from `key: value`.
        i = _skip_trivia(i, eat_commas=False)
        # Shorthand: `foo,` or `foo}` or end-of-body.
        if i >= n or body[i] in ",}\n":
            items.append((key, key))
            if i < n and body[i] == ",":
                i += 1
            continue
        if i < n and body[i] == ":":
            i += 1
        else:
            # Not a proper `key: value` — skip token.
            continue
        # Value: read until top-level comma or end.
        depth_paren = 0
        depth_brace = 0
        depth_bracket = 0
        in_str: str | None = None
        v_start = i
        while i < n:
            c = body[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == in_str:
                    in_str = None
                i += 1
                continue
            if c in ("'", '"', "`"):
                in_str = c
                i += 1
                continue
            if c == "(":
                depth_paren += 1
            elif c == ")":
                depth_paren -= 1
            elif c == "{":
                depth_brace += 1
            elif c == "}":
                if depth_brace == 0:
                    break
                depth_brace -= 1
            elif c == "[":
                depth_bracket += 1
            elif c == "]":
                depth_bracket -= 1
            elif c == "," and depth_paren == 0 and depth_brace == 0 and depth_bracket == 0:
                break
            i += 1
        value = body[v_start: i].strip()
        items.append((key, value))
        if i < n and body[i] == ",":
            i += 1
    return items


def _line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ── Linker class ───────────────────────────────────────────────────────────


class TrpcProcedureLinker:
    """Resolves ``trpc.X.Y.{useQuery|mutate|...}()`` calls to procedure files."""

    name: str = "trpc-procedure"
    activation_keys: tuple[str, ...] = ("any-with-trpc-dep",)

    def __init__(self) -> None:
        # Procedure-path string -> _ProcedureEntry
        self._procedure_map: dict[str, _ProcedureEntry] | None = None
        # Router-var-name -> path-prefix-relative entries (for resolving
        # composed routers across files via `createTRPCRouter({user: userRouter})`).
        self._router_vars: dict[str, dict[str, _ProcedureEntry]] | None = None
        self._tracked_set: set[str] | None = None
        self._repo_root: Path | None = None
        self.telemetry: _LinkerTelemetry = _LinkerTelemetry()

    # ── Activation ─────────────────────────────────────────────────────

    def is_active(self, ctx: "ScanContext") -> bool:
        # Check workspace package.json + any tracked package.json files.
        if self._scan_workspace_deps(ctx):
            self._repo_root = ctx.repo_path
            return True
        # Fallback: scan top-level + workspace package.json files.
        if self._scan_tracked_package_json(ctx):
            self._repo_root = ctx.repo_path
            return True
        return False

    @staticmethod
    def _scan_workspace_deps(ctx: "ScanContext") -> bool:
        for ws in (getattr(ctx, "workspaces", None) or []):
            pkg = getattr(ws, "package_json", None)
            if not pkg:
                continue
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                d = pkg.get(key)
                if not isinstance(d, dict):
                    continue
                for dep in d:
                    if any(str(dep).startswith(p) for p in _TRPC_DEP_PREFIXES):
                        return True
        return False

    @staticmethod
    def _scan_tracked_package_json(ctx: "ScanContext") -> bool:
        import json
        for rel in ctx.tracked_files:
            if not rel.endswith("package.json"):
                continue
            # Skip node_modules entries.
            if "/node_modules/" in "/" + rel:
                continue
            abs_path = str(ctx.repo_path / rel)
            try:
                with open(abs_path, "r", encoding="utf-8") as fp:
                    pkg = json.load(fp)
            except (OSError, ValueError):
                continue
            for key in ("dependencies", "devDependencies", "peerDependencies"):
                d = pkg.get(key)
                if not isinstance(d, dict):
                    continue
                for dep in d:
                    if any(str(dep).startswith(p) for p in _TRPC_DEP_PREFIXES):
                        return True
        return False

    # ── Router map ─────────────────────────────────────────────────────

    def _ensure_router_map(self, ctx: "ScanContext") -> dict[str, _ProcedureEntry]:
        if self._procedure_map is not None:
            return self._procedure_map

        self._repo_root = ctx.repo_path
        self._tracked_set = set(ctx.tracked_files)
        # First pass: parse every router definition into its local
        # key/value tree, scoped by the router variable name.
        # Each "local" map: prefix-relative-path -> _ProcedureEntry OR
        # sub-router-var-name (string) for chaining.
        local_maps: dict[str, dict[str, _ProcedureEntry | str]] = {}

        for rel in ctx.tracked_files:
            if not rel.endswith((".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs")):
                continue
            # Cheap pre-filter: only files mentioning router/trpc primitives.
            abs_path = str(ctx.repo_path / rel)
            text = _read_text_cached(abs_path)
            if text is None:
                continue
            if "createTRPCRouter" not in text and "router(" not in text:
                continue
            file_local_count = 0
            for m in _ROUTER_DEF.finditer(text):
                var_name = m.group(1)
                # Find the opening { after the match end and locate matching }.
                obj_open = text.find("{", m.end() - 1)
                if obj_open == -1:
                    continue
                obj_close = _find_matching_brace(text, obj_open)
                if obj_close == -1:
                    continue
                body = text[obj_open + 1: obj_close - 1]
                items = _split_top_level_object(body)
                local: dict[str, _ProcedureEntry | str] = {}
                for key, value in items:
                    # Compute the value's line range relative to the file.
                    # Find the value within the body (first occurrence
                    # after the key marker is close enough — we only
                    # need rough line range for UI).
                    value_idx = text.find(value, obj_open) if value else obj_open
                    if value_idx == -1:
                        value_idx = obj_open
                    line_start = _line_no(text, value_idx)
                    # End line: approximate as start of next item OR obj_close.
                    line_end = _line_no(text, obj_close - 1)
                    # IMPORTANT order: inline-router check FIRST. An
                    # inline sub-router contains procedure markers in
                    # its leaves, so the PROCEDURE_MARKER test would
                    # otherwise false-trigger on it.
                    is_inline_router = (
                        value
                        and ("createTRPCRouter(" in value or value.lstrip().startswith("router("))
                    )
                    if is_inline_router:
                        # Recursive parse for inline router({...}).
                        inner_open = text.find("{", value_idx)
                        inner_close = _find_matching_brace(text, inner_open) if inner_open != -1 else -1
                        if inner_open != -1 and inner_close != -1:
                            inner_body = text[inner_open + 1: inner_close - 1]
                            inner_items = _split_top_level_object(inner_body)
                            synth_var = f"__inline_{var_name}_{key}__"
                            inner_local: dict[str, _ProcedureEntry | str] = {}
                            for ik, iv in inner_items:
                                iv_idx = text.find(iv, inner_open) if iv else inner_open
                                if iv_idx == -1:
                                    iv_idx = inner_open
                                ls = _line_no(text, iv_idx)
                                le = _line_no(text, inner_close - 1)
                                if _PROCEDURE_MARKER.search(iv or ""):
                                    inner_local[ik] = _ProcedureEntry(
                                        file=rel, symbol=ik,
                                        line_start=ls, line_end=le,
                                    )
                                elif iv and re.fullmatch(r"[A-Za-z_$][\w$]*", iv.strip()):
                                    inner_local[ik] = iv.strip()
                            if inner_local:
                                local_maps[synth_var] = inner_local
                                local[key] = synth_var
                        continue
                    if _PROCEDURE_MARKER.search(value or ""):
                        local[key] = _ProcedureEntry(
                            file=rel, symbol=key,
                            line_start=line_start, line_end=line_end,
                        )
                    elif value and re.fullmatch(r"[A-Za-z_$][\w$]*", value.strip()):
                        # Shorthand sub-router reference like `user: userRouter`
                        # or shorthand `userRouter,` (key == value).
                        local[key] = value.strip()
                    elif value == key:
                        # Pure shorthand `userRouter,` — treat as a
                        # sub-router var.
                        local[key] = key
                    # else: unknown shape — silently drop. Could be a
                    # value we can't classify (raw object literal,
                    # spread, etc.). Telemetry stays clean.
                if local:
                    local_maps[var_name] = local
                    file_local_count += 1
            if file_local_count:
                self.telemetry.router_files_parsed += 1

        # Second pass: resolve sub-router chains. Identify the root
        # router(s) — typically `appRouter` / `edgeRouter` / `lambdaRouter`
        # / `mergeRouters(a,b)` — but in absence of explicit root,
        # treat EVERY router var as a potential root and emit its
        # paths. The procedure map keys are dotted full paths.
        flat: dict[str, _ProcedureEntry] = {}

        # We want: for each routerVar, the full set of dotted leaf paths.
        # For chaining, we just inline sub-router maps at the prefix.
        # Use memoization to avoid infinite loops.
        memo: dict[str, dict[str, _ProcedureEntry]] = {}

        def expand(var: str, seen: frozenset[str]) -> dict[str, _ProcedureEntry]:
            if var in memo:
                return memo[var]
            if var in seen:
                return {}
            local = local_maps.get(var)
            if not local:
                return {}
            out: dict[str, _ProcedureEntry] = {}
            for key, val in local.items():
                if isinstance(val, _ProcedureEntry):
                    out[key] = val
                else:
                    # val is a sub-router var name. Try `val` exactly
                    # AND `val` minus trailing 'Router' resolution
                    # (e.g. `user: userRouter` → expand `userRouter`).
                    sub = expand(val, seen | {var})
                    for sub_path, entry in sub.items():
                        out[f"{key}.{sub_path}"] = entry
            memo[var] = out
            return out

        # First treat known root variable names with priority.
        root_priority = ("appRouter", "edgeRouter", "lambdaRouter")
        roots_seen: set[str] = set()
        for root in root_priority:
            if root in local_maps:
                roots_seen.add(root)
                for path, entry in expand(root, frozenset()).items():
                    flat.setdefault(path, entry)
        # Then add every OTHER router var that isn't an inline-only
        # synthetic var (those start with `__inline_`).
        for var in local_maps:
            if var.startswith("__inline_"):
                continue
            if var in roots_seen:
                continue
            for path, entry in expand(var, frozenset()).items():
                flat.setdefault(path, entry)

        self._procedure_map = flat
        self.telemetry.router_map_size = len(flat)
        return flat

    # ── Public surface ────────────────────────────────────────────────

    def link_for_feature(
        self,
        feature: "Feature",
        ctx: "ScanContext",
        log: "StageLogger",
    ) -> list[FrameworkLink]:
        proc_map = self._ensure_router_map(ctx)
        if not proc_map:
            return []
        self.telemetry.features_processed += 1

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
            # Cheap pre-filter: any of our client identifiers used.
            if not any(f"{c}." in text for c in _TRPC_CLIENT_IDENTIFIERS):
                continue
            self.telemetry.files_scanned += 1
            for m in _CALL_SITE.finditer(text):
                self.telemetry.procedure_call_sites_found += 1
                # client = m.group(1)  # unused (kept for telemetry-future)
                path = m.group(2)
                # verb  = m.group(3)
                line_no = _line_no(text, m.start())
                entry = proc_map.get(path)
                if entry is None:
                    self.telemetry.unmatched_call_sites += 1
                    if len(self.telemetry.unmatched_paths_sample) < 10:
                        self.telemetry.unmatched_paths_sample.append(path)
                    continue
                source_symbol = _enclosing_symbol(text.splitlines(), line_no)
                link = FrameworkLink(
                    source_file=rel,
                    source_symbol=source_symbol,
                    source_line=line_no,
                    target_file=entry.file,
                    target_symbol=path,
                    target_line_start=entry.line_start,
                    target_line_end=entry.line_end,
                    linker=self.name,
                    link_kind="trpc-procedure",
                    confidence=1.0,
                    reason=f"trpc call {path} resolves to {entry.file}",
                )
                links.append(link)
                if len(self.telemetry.sample_links) < 5:
                    self.telemetry.sample_links.append({
                        "source": f"{rel}:{line_no}",
                        "target": f"{entry.file}:{path}",
                        "verb": m.group(3),
                    })
                log.emit(
                    feature.name,
                    f"trpc link: {rel}:{line_no} → {entry.file}:{path}",
                    linker=self.name, path=path, verb=m.group(3),
                )
        self.telemetry.links_emitted += len(links)
        return links

    # ── Internals ─────────────────────────────────────────────────────

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


# ── Enclosing-symbol heuristic (same as C4/C5) ─────────────────────────────


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


__all__ = ["TrpcProcedureLinker"]
