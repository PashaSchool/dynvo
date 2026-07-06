"""Stage 6.97 — deterministic feature-level LOC ($0, output layer).

Operator invariant (validate_scan.py I2): «фіча без коду = грубий баг» —
every dev feature with ≥1 real owned source file must carry a nonzero
LOC. Before this stage the ONLY LOC surface was the flow rollup
(``flows[].nodes[].lines``), so features whose files never became flows
(package anchors, flowless route groups, config features) showed "·" on
the dashboard even with dozens of owned paths.

OWNED vs SHARED accounting (2026-07-05 loc-truth fix)
-----------------------------------------------------
The naïve "sum every path's line count into every feature that lists it"
model inflated ``sum(product_features[].loc)`` by ~13× on real repos:
a file shared by N features was counted N times, and a bare *directory*
path (``frontend``) was walked recursively into build output
(``dist/``, ``coverage/``). This stage now:

* Counts each physical file ONCE at its deterministic PRIMARY owner.
  Primary owner of a shared file = the dev feature with the strongest
  claim: (1) the most sibling counted files in the SAME directory,
  (2) tie → the feature with the most flows (behavioural mass),
  (3) tie → slug order (smallest ``name`` first). Fully deterministic.
* Emits two fields per feature:
    ``loc``        — OWNED lines: files attributed only to this feature
                     + this feature's primary-owned shared files, once.
    ``loc_shared`` — lines in files this feature touches but does NOT
                     primarily own (visible, never summed into ``loc``).
* Restricts *directory* path expansion to real source extensions
  (:data:`_SOURCE_EXTS`) and skips build/output/cache directories, so a
  bare ``frontend`` path no longer pulls ``dist/`` / ``coverage/`` /
  generated typings. EXPLICIT file paths bypass the extension gate (a
  feature that lists ``vercel.json`` still counts it).
* Populates ``member_files[].loc`` (per-file line count) and
  ``scan_meta.loc_accounting`` (``repo_loc`` / ``sum_pf_owned`` /
  ``sum_pf_shared_refs``) for the global sanity check
  ``sum_pf_owned <= repo_loc`` (validator I13).

The OWNED/SHARED partition is disjoint per file across DEVELOPER
features (a file has exactly one primary owner), and each product
feature belongs-set is disjoint from every other's, so
``sum_pf_owned <= repo_loc`` holds by construction. Product-layer
duplicates that also live in ``features[]`` are excluded from the
ownership computation and instead mirror their product rollup.

Counting convention (REUSED, not reinvented):
``faultline.tools.line_completeness.executable_lines`` — the engine's
canonical per-language LOC scanner (comment/blank-aware for the hash- &
C-comment families; non-blank fallback for any other text file, so
YAML/JSON/config-as-product features still get a nonzero count).

Excluded from the COUNT (the paths themselves stay listed on the
feature — this is a metric, not a membership strip):

* test files/dirs      — :func:`stage_6_9_test_strip.is_test_path`
* generated code       — :func:`stage_6_9b_generated_strip.is_generated_path`
* lockfiles            — ``package-lock.json`` / ``pnpm-lock.yaml`` / …
* minified/bundled     — ``*.min.js`` / ``*.min.css`` / ``*.map``
* binary/media         — extension denylist + NUL-byte sniff
* files missing on disk (historical paths from the git window)
* (directory walks only) non-source extensions + build/cache dirs

Per-file counts are cached for the whole scan (shared files and the
PF rollup never re-read a file). Deterministic, no LLM, no network.
Disable via ``FAULTLINE_STAGE_6_97_FEATURE_LOC=0`` (default ON).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from faultline.pipeline_v2.stage_6_9_test_strip import is_test_path
from faultline.pipeline_v2.stage_6_9b_generated_strip import is_generated_path

if TYPE_CHECKING:  # pragma: no cover — typing only
    from faultline.models.types import Feature

__all__ = [
    "STAGE_6_97_ENV_FLAG",
    "stage_6_97_enabled",
    "apply_feature_loc",
    "count_file_loc",
]

STAGE_6_97_ENV_FLAG = "FAULTLINE_STAGE_6_97_FEATURE_LOC"

#: Lockfiles — machine-written dependency snapshots, never product code.
_LOCKFILE_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "poetry.lock",
    "uv.lock",
    "cargo.lock",
    "composer.lock",
    "gemfile.lock",
    "bun.lockb",
    "go.sum",
    "flake.lock",
}

#: Binary / media / bundle extensions — unreadable or non-authored.
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".bmp",
    ".svgz", ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp3", ".mp4", ".wav", ".ogg", ".webm", ".mov", ".avi",
    ".so", ".dylib", ".dll", ".exe", ".bin", ".wasm", ".o", ".a",
    ".jar", ".class", ".pyc", ".pyo", ".db", ".sqlite", ".sqlite3",
    ".lockb", ".node", ".heic", ".psd", ".ai",
}

#: Real authored-source extensions — the ONLY extensions a *directory*
#: path is expanded into (an explicit FILE path bypasses this gate, so
#: config-as-product features that list a specific ``.json``/``.yaml``
#: file still count). Excludes data/doc families (``.json`` data blobs,
#: ``.md``, ``.txt``, ``.csv``, ``.xml``) that bloat dir-walks.
_SOURCE_EXTS = {
    # programming
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts",
    ".cts", ".go", ".rs", ".rb", ".java", ".kt", ".kts", ".scala",
    ".php", ".ex", ".exs", ".erl", ".c", ".h", ".cc", ".cpp", ".hpp",
    ".cs", ".swift", ".m", ".mm", ".dart", ".lua", ".clj", ".cljs",
    ".hs", ".ml", ".r", ".jl", ".groovy", ".pl", ".pm",
    # web / templates
    ".vue", ".svelte", ".astro", ".html", ".htm", ".hbs", ".ejs",
    ".pug", ".jade", ".liquid", ".erb", ".haml", ".blade.php",
    # styles
    ".css", ".scss", ".sass", ".less", ".styl",
    # schema / query / config-as-source
    ".sql", ".graphql", ".gql", ".prisma", ".proto", ".yaml", ".yml",
    ".toml",
    # shell / infra-as-code
    ".sh", ".bash", ".zsh", ".tf",
}

#: Directories never counted in a recursive walk — VCS, vendored deps,
#: build output, caches. Their presence in a bare-directory path was the
#: ``frontend`` → ``dist/`` + ``coverage/`` inflation vector.
_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "env", "__pycache__",
    "dist", "build", "out", ".next", ".nuxt", ".svelte-kit", ".turbo",
    ".output", "coverage", ".coverage", ".nyc_output", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".cache", "storybook-static",
    "target", ".gradle", ".idea", ".vscode", "vendor",
    "bower_components", ".terraform", ".parcel-cache", "__snapshots__",
    # Agent/IDE scratch state — ``.claude/worktrees/*`` holds FULL repo
    # checkouts (git worktrees) that otherwise get counted N times.
    ".claude", ".worktrees", "worktrees",
}

#: Report / build-metadata / data extensions — excluded even from an
#: EXPLICIT file path (they are machine-written, not authored source).
_REPORT_EXTS = {
    ".info", ".lcov", ".tsbuildinfo", ".log", ".snap",
    ".csv", ".tsv", ".coverage",
}

_SNIFF_BYTES = 8192


def stage_6_97_enabled() -> bool:
    """Default ON; ``FAULTLINE_STAGE_6_97_FEATURE_LOC=0`` disables."""
    return os.environ.get(STAGE_6_97_ENV_FLAG, "1").strip() not in {
        "0", "false", "False",
    }


# ── Per-file counting ───────────────────────────────────────────────────


def _is_excluded_name(path: str) -> bool:
    """Lockfile / minified / sourcemap / binary-extension exclusion."""
    base = path.lower().replace("\\", "/").rsplit("/", 1)[-1]
    if base in _LOCKFILE_NAMES:
        return True
    if base.endswith((".min.js", ".min.css", ".min.mjs", ".map")):
        return True
    ext = os.path.splitext(base)[1]
    return ext in _BINARY_EXTS or ext in _REPORT_EXTS


def _is_source_ext(path: str) -> bool:
    """True when ``path`` has an authored-source extension (dir-walk gate)."""
    base = path.lower().replace("\\", "/").rsplit("/", 1)[-1]
    if base.endswith(".d.ts"):
        return False  # generated typings (also caught by generated_strip)
    ext = os.path.splitext(base)[1]
    return ext in _SOURCE_EXTS


def count_file_loc(abs_path: Path, rel_path: str) -> int:
    """Line count for ONE file per the engine's LOC convention; 0 when
    the file is excluded (test / generated / lockfile / binary / missing
    / empty)."""
    if is_test_path(rel_path) or is_generated_path(rel_path):
        return 0
    if _is_excluded_name(rel_path):
        return 0
    try:
        with open(abs_path, "rb") as fp:
            head = fp.read(_SNIFF_BYTES)
    except OSError:
        return 0
    if b"\x00" in head:  # NUL sniff — extensionless binaries
        return 0
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    # Late import: tools ← pipeline_v2 is the existing dependency
    # direction for the strip predicates; the LOC scanner lives in
    # tools and imports stage_6_9b, so import at call time to keep
    # module-import order cycle-free.
    from faultline.tools.line_completeness import executable_lines

    ext = os.path.splitext(rel_path)[1].lower()
    return len(executable_lines(text, ext))


def _iter_dir_files(root: Path) -> Iterable[Path]:
    """Sorted recursive file walk, skipping VCS/vendor/build dirs."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def _file_loc_cached(abs_path: Path, rel: str, cache: dict[str, int]) -> int:
    """Per-FILE memoised line count (the scan-wide cache is keyed by the
    normalised relative file path)."""
    if rel in cache:
        return cache[rel]
    n = count_file_loc(abs_path, rel)
    cache[rel] = n
    return n


def _expand_feature_files(
    repo_root: Path,
    paths: Iterable[Any],
    cache: dict[str, int],
) -> dict[str, int]:
    """Resolve a feature's ``paths`` to a ``{rel_file: loc}`` map of its
    COUNTED files (loc > 0).

    * A FILE path counts regardless of extension (existing exclusions
      still apply) — config-as-product surfaces stay visible.
    * A DIRECTORY path is walked but only real *source* extensions are
      counted (dir-walk discipline — no build/data blowup), and build /
      cache directories are skipped entirely.
    """
    out: dict[str, int] = {}
    for raw in paths or []:
        rel = str(raw).replace(os.sep, "/").strip("/")
        # A whole-repo claim (``.`` / ``""`` / a path resolving to the repo
        # root) is an attribution pathology, not an owned surface — walking
        # it would pull the ENTIRE repo into one feature (the "AI PF 987K
        # dir-walk" case). Contribute nothing; its files are owned by the
        # real feature that lists them specifically.
        if _is_root_marker(rel):
            continue
        abs_path = repo_root / rel
        try:
            if abs_path.resolve() == repo_root.resolve():
                continue
        except OSError:
            pass
        if abs_path.is_dir():
            for child in _iter_dir_files(abs_path):
                child_rel = child.relative_to(repo_root).as_posix()
                if not _is_source_ext(child_rel):
                    continue
                n = _file_loc_cached(child, child_rel, cache)
                if n > 0:
                    out[child_rel] = n
        else:
            n = _file_loc_cached(abs_path, rel, cache)
            if n > 0:
                out[rel] = n
    return out


def _parent_dir(rel: str) -> str:
    return rel.rsplit("/", 1)[0] if "/" in rel else ""


def _is_root_marker(rel: str) -> bool:
    """True when a path is a repo-root / whole-repo STRUCTURAL marker
    (``.`` / ``""`` / ``./`` / ``..``) rather than an owned surface.

    Such markers — including ``{"path": ".", "role": "anchor"}`` member
    files seen on papermark — must contribute ZERO LOC: counted, they
    would pull the ENTIRE repo into every feature that carries one
    (Auth / File-Uploads / Background-Jobs each counting ~350k lines of a
    249k-line repo). Disk-independent so it holds even when the repo is
    not checked out.
    """
    r = rel.strip().strip("/").strip()
    return r in ("", ".", "..")


# ── Stage body ──────────────────────────────────────────────────────────


def apply_feature_loc(
    features: list["Feature"],
    product_features: list["Feature"] | None,
    repo_root: Path | str,
) -> dict[str, Any]:
    """Stamp OWNED ``loc`` + ``loc_shared`` on every developer + product
    feature IN PLACE, populate ``member_files[].loc``, and return the
    telemetry dict (including ``loc_accounting``) for
    ``scan_meta['feature_loc']``.
    """
    root = Path(repo_root)
    cache: dict[str, int] = {}  # rel_file -> loc (scan-wide, per FILE)

    # Ownership is computed over DEVELOPER features only. Product-layer
    # features are duplicated into ``features[]`` (they carry the same
    # paths as their member devs) — counting them here would double every
    # file. They instead mirror the product rollup below.
    dev_features = [
        f for f in features
        if getattr(f, "layer", "developer") != "product"
    ]

    # Expand each dev feature to its counted files.
    dev_files: list[dict[str, int]] = []
    file_to_devs: dict[str, list[int]] = {}
    for i, feat in enumerate(dev_features):
        files = _expand_feature_files(root, feat.paths, cache)
        dev_files.append(files)
        for fp in files:
            file_to_devs.setdefault(fp, []).append(i)

    # Per-dev tiebreak signals for primary-owner selection.
    dev_dircount: list[dict[str, int]] = []
    for files in dev_files:
        dc: dict[str, int] = {}
        for fp in files:
            d = _parent_dir(fp)
            dc[d] = dc.get(d, 0) + 1
        dev_dircount.append(dc)
    dev_flowcount = [len(getattr(f, "flows", None) or []) for f in dev_features]
    dev_slug = [str(getattr(f, "name", "") or "") for f in dev_features]
    # Product-Spine §4.1 — a concern FACET never wins primary ownership of a
    # shared file: the structural owner does (the facet's claim is a
    # cross-cutting VIEW). Facet-exclusive files still count on the facet
    # itself (visible), but a facet has no product_feature_id, so its owned
    # lines never roll into any PF.
    from faultline.pipeline_v2.spine_hygiene import is_facet

    dev_is_facet = [1 if is_facet(f) else 0 for f in dev_features]

    def _primary(fp: str) -> int:
        owners = file_to_devs[fp]
        if len(owners) == 1:
            return owners[0]
        d = _parent_dir(fp)
        # Non-facet first, then max sibling-dir count, then max flow count,
        # then smallest slug.
        return min(
            owners,
            key=lambda i: (
                dev_is_facet[i],
                -dev_dircount[i].get(d, 0),
                -dev_flowcount[i],
                dev_slug[i],
            ),
        )

    primary_of: dict[str, int] = {fp: _primary(fp) for fp in file_to_devs}

    # ── Developer feature loc / loc_shared + member_files loc ───────────
    zero_loc_with_paths = 0
    for i, feat in enumerate(dev_features):
        files = dev_files[i]
        owned = sum(l for fp, l in files.items() if primary_of[fp] == i)
        shared = sum(l for fp, l in files.items() if primary_of[fp] != i)
        if owned == 0 and files:
            # I2-safety: a pure-sharer (primary of nothing) still owns
            # SOME real code — attribute its single largest counted file
            # so «фіча без коду» never fires. This dev-level floor does
            # NOT feed the PF rollup (which uses the disjoint owned SET),
            # so the sum_pf_owned <= repo_loc invariant is preserved.
            owned = max(files.values())
        feat.loc = owned
        feat.loc_shared = shared
        if feat.paths and owned == 0:
            zero_loc_with_paths += 1
        _stamp_member_file_loc(root, feat, cache)

    # ── Product feature rollup (each physical file counted ONCE) ────────
    dev_idx_by_pfid: dict[str, list[int]] = {}
    for i, feat in enumerate(dev_features):
        pfid = getattr(feat, "product_feature_id", None)
        if pfid:
            dev_idx_by_pfid.setdefault(pfid, []).append(i)

    pf_loc_by_name: dict[str, tuple[int, int]] = {}
    sum_pf_owned = 0
    sum_pf_shared = 0
    pfs = product_features or []
    for pf in pfs:
        members = dev_idx_by_pfid.get(pf.name, [])
        owned_files: dict[str, int] = {}
        ref_files: dict[str, int] = {}
        for i in members:
            for fp, l in dev_files[i].items():
                ref_files[fp] = l
                if primary_of[fp] == i:
                    owned_files[fp] = l
        if not owned_files and not members:
            # PF with no dev members — count its own paths, but only files
            # not already primary-owned elsewhere (invariant safety).
            own = _expand_feature_files(root, pf.paths, cache)
            owned_files = {fp: l for fp, l in own.items() if fp not in primary_of}
            ref_files = own
        pf.loc = sum(owned_files.values())
        pf.loc_shared = sum(
            l for fp, l in ref_files.items() if fp not in owned_files
        )
        _stamp_member_file_loc(root, pf, cache)
        pf_loc_by_name[pf.name] = (pf.loc, pf.loc_shared)
        sum_pf_owned += pf.loc
        sum_pf_shared += pf.loc_shared

    # Mirror the product rollup onto product-layer duplicates that live in
    # ``features[]`` (so the dashboard + validator I2 see them consistently).
    for feat in features:
        if getattr(feat, "layer", "developer") == "product":
            loc_pair = pf_loc_by_name.get(feat.name)
            if loc_pair is not None:
                feat.loc, feat.loc_shared = loc_pair
                _stamp_member_file_loc(root, feat, cache)

    # ── Global sanity ──────────────────────────────────────────────────
    repo_loc = sum(v for v in cache.values() if v > 0)
    counted_files = sum(1 for v in cache.values() if v > 0)
    loc_accounting = {
        "repo_loc": repo_loc,
        "sum_pf_owned": sum_pf_owned,
        "sum_pf_shared_refs": sum_pf_shared,
    }
    return {
        "enabled": True,
        "features_total": len(features),
        "features_with_loc": sum(1 for f in features if (f.loc or 0) > 0),
        "features_zero_loc_with_paths": zero_loc_with_paths,
        "product_features_total": len(pfs),
        "paths_indexed": len(cache),
        "files_counted": counted_files,
        "loc_accounting": loc_accounting,
    }


def _stamp_member_file_loc(
    repo_root: Path,
    feat: "Feature",
    cache: dict[str, int],
) -> None:
    """Populate ``member_files[].loc`` with the per-file line count.

    The count is the file's OWN executable-line count (provenance level):
    the same shared file carries the same ``loc`` on every claimant, so
    this is NOT the feature's owned share (``Feature.loc`` is). Directory
    member paths (rare) stay ``None``.
    """
    for mf in getattr(feat, "member_files", None) or []:
        rel = str(getattr(mf, "path", "") or "").replace(os.sep, "/").strip("/")
        if not rel:
            continue
        # Repo-root / directory anchors are structural markers, not owned
        # code — record 0, never the whole repo (papermark corruption).
        if _is_root_marker(rel):
            mf.loc = 0
            continue
        abs_path = repo_root / rel
        if abs_path.is_dir():
            mf.loc = 0
            continue
        mf.loc = _file_loc_cached(abs_path, rel, cache)
