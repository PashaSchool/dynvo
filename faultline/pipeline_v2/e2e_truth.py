"""E2E-journey truth (Stage 6.98) — maintainer-authored user journeys
from playwright/cypress specs as UF evidence + named recall holes.

E2E specs are the ONE in-repo prose-free source where the MAINTAINER
names complete user journeys ("user can share document", describe>it
chains, ordered goto/click/fill steps). The engine has only ever used
them for coverage (``flow_test_mapper``); this module reads them as
DETECTION-grade ground truth:

  1. **Extract** — deterministic parse of every e2e spec file into
     ``E2EJourney`` rows: ``title_chain`` (describe>…>it), ordered
     ``steps`` (goto/click/fill with the visible selector/URL), and
     ``urls_touched`` (goto/visit args, ``redirectPath:`` values and
     bare path-shaped string literals, template ``${…}`` → ``:param``).
  2. **Stitch** ($0 LLM) — each journey's URLs run through the SAME
     tenancy-transparent route-family key the spine uses
     (:func:`spine_anchors._pattern_key_chain` — private-import
     precedent: ``flow_test_mapper`` ↔ ``analyzer.test_mapper``), then
     match against ``UserFlow.routes`` families. Route-family overlap
     ⇒ ``matched`` (journey CONFIRMS the UF); a journey no UF claims ⇒
     ``orphan_journeys[]`` — a recall hole ALREADY NAMED by the
     maintainer. Journeys with no route evidence fall back to a
     content-token overlap vs UF name/resource (flagged ``via="name"``,
     never mixed with route matches).
  3. **Emit** — ``scan_meta["e2e_truth"]`` (compact telemetry + orphan
     titles) and a full per-scan artifact via ``write_stage_artifact``.
     Matched titles are ALSO shaped as W3-naming-contract candidate
     dicts (``naming_candidates[]`` in the artifact) — data only; no
     naming_contract wiring here (coordinator decision).

Parser honesty (started simple per the track brief): a string/comment/
template-aware masking scan + brace-depth walk — NOT a real TS parser.
Known accepted limits, each rare in spec code and none affecting
determinism: regex literals containing braces can skew depth; strings
nested inside ``${…}`` expressions are not re-tracked; ``it.each(…)``
parametrised titles are skipped (dynamic anyway). tree-sitter (already
a wheel dep for W6-AST) is the natural upgrade path once ts_ast lands.

Repos with no e2e specs report ``e2e_absent: true`` and ZERO impact.
Kill-switch ``FAULTLINE_E2E_TRUTH=0`` ⇒ the stage never runs, no
scan_meta key, no artifact — byte-identical output.

[[rule-no-readme]]: specs are code, not prose docs. [[rule-no-magic-
tuning]]: matching is family-key equality/prefix — no thresholds; the
weak-title set and the function-word stoplist are tiny universal
vocabularies (honesty filters, not detection knobs).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from faultline.pipeline_v2.spine_anchors import (
    _pattern_key_chain,
    load_spine_vocab,
    normalize_anchor_key,
)

E2E_TRUTH_ENV = "FAULTLINE_E2E_TRUTH"


def e2e_truth_enabled() -> bool:
    """Default ON (this branch); ``FAULTLINE_E2E_TRUTH=0`` disables the
    stage entirely — no scan_meta key, no artifact, byte-identical."""
    return os.environ.get(E2E_TRUTH_ENV, "1").strip().lower() not in {
        "0", "false",
    }


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

@dataclass
class E2EJourney:
    """One maintainer-authored journey = one ``test(…)`` / ``it(…)``."""

    file: str                         # repo-relative posix path
    title_chain: tuple[str, ...]      # (describe, …, test title)
    steps: list[dict[str, str]] = field(default_factory=list)
    urls_touched: list[str] = field(default_factory=list)  # sorted, deduped
    runner_project: str = ""          # repo-relative dir of the runner config

    @property
    def journey_id(self) -> str:
        return f"{self.file}::{' > '.join(self.title_chain)}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "journey_id": self.journey_id,
            "file": self.file,
            "title_chain": list(self.title_chain),
            "steps": self.steps,
            "urls_touched": self.urls_touched,
            "runner_project": self.runner_project,
        }


_SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", ".turbo", "out",
    "coverage", "vendor", "__pycache__", ".venv", "venv",
    "test-results", "playwright-report",
}
_SPEC_SUFFIX_RE = re.compile(
    r"\.(?:spec|test|cy|e2e|e2e-spec)\.(?:ts|tsx|js|jsx|mjs|cjs)$"
)
_PLAYWRIGHT_MARKER = "@playwright/test"
_CY_CALL_RE = re.compile(r"\bcy\.[a-z]")
_RUNNER_CONFIGS = (
    "playwright.config.ts", "playwright.config.js", "playwright.config.mjs",
    "cypress.config.ts", "cypress.config.js", "cypress.json",
)

# test()/it()/describe() head — dotted chain restricted to a whitelist so
# `test.describe.configure(`, `test.use(`, `it.each(` never parse as heads.
_HEAD_RE = re.compile(r"\b(?:test|it|describe)(?:\s*\.\s*[A-Za-z]+)*\s*\(")
_HEAD_TOKENS = {
    "test", "it", "describe", "only", "skip", "fixme", "slow",
    "serial", "parallel",
}
_TPL_EXPR_RE = re.compile(r"\$\{[^{}]*\}")

# Titles that carry zero journey information (typebot: 8× "should work
# as expected") — the FILE STEM is the signal instead. Honesty filter.
_WEAK_TITLES = {
    "should work as expected", "should work", "works", "works as expected",
    "should work properly", "should work correctly",
}

_ASSET_EXT_RE = re.compile(
    r"\.(?:png|jpe?g|svg|gif|ico|css|js|mjs|json|pdf|webp|mp4|woff2?|zip)$",
    re.IGNORECASE,
)
_PATHISH_RE = re.compile(r"^/(?!/)[^\s'\"`]*$")

_URL_ARG_RE = re.compile(r"\.(?:goto|visit)\s*\(\s*(['\"`])(.*?)\1", re.DOTALL)
_REDIRECT_RE = re.compile(r"redirectPath\s*:\s*(['\"`])(.*?)\1", re.DOTALL)
_LOCATOR_HEADS = (
    "getByRole|getByTestId|getByText|getByLabel|getByLabelText|"
    "getByPlaceholder|getByTitle|getByAltText|locator|frameLocator|"
    "contains|get|find|findByText"
)
_CLICK_RE = re.compile(
    r"(?:%s)\s*\(\s*(['\"`])(.*?)\1[^;\n]{0,200}?\.\s*click\s*\("
    % _LOCATOR_HEADS,
    re.DOTALL,
)
_FILL_RE = re.compile(
    r"(?:%s)\s*\(\s*(['\"`])(.*?)\1[^;\n]{0,200}?\.\s*(?:fill|type)\s*\("
    % _LOCATOR_HEADS,
    re.DOTALL,
)


def _mask(text: str) -> str:
    """Same-length copy with comments and string CONTENTS blanked.

    String delimiters stay (so arguments can be located and read back
    from the original); everything inside them, plus comments and
    ``${…}`` template expressions, becomes spaces. Brace/paren depth
    computed on the mask therefore sees only real code structure.
    """
    out = list(text)
    n = len(text)
    i = 0
    mode: list[str] = []  # stack: LC BC SQ DQ TPL TEX (+brace counter piggyback)
    tex_depth: list[int] = []

    def cur() -> str:
        return mode[-1] if mode else "CODE"

    while i < n:
        c = text[i]
        m = cur()
        if m == "CODE":
            if c == "/" and i + 1 < n and text[i + 1] == "/":
                mode.append("LC"); out[i] = out[i + 1] = " "; i += 2; continue
            if c == "/" and i + 1 < n and text[i + 1] == "*":
                mode.append("BC"); out[i] = out[i + 1] = " "; i += 2; continue
            if c == "'":
                mode.append("SQ"); i += 1; continue
            if c == '"':
                mode.append("DQ"); i += 1; continue
            if c == "`":
                mode.append("TPL"); i += 1; continue
            i += 1
            continue
        if m == "LC":
            if c == "\n":
                mode.pop()
            else:
                out[i] = " "
            i += 1
            continue
        if m == "BC":
            if c == "*" and i + 1 < n and text[i + 1] == "/":
                out[i] = out[i + 1] = " "; mode.pop(); i += 2; continue
            out[i] = " " if c != "\n" else c
            i += 1
            continue
        if m in ("SQ", "DQ"):
            q = "'" if m == "SQ" else '"'
            if c == "\\" and i + 1 < n:
                out[i] = out[i + 1] = " "; i += 2; continue
            if c == q:
                mode.pop(); i += 1; continue
            out[i] = " " if c != "\n" else c
            i += 1
            continue
        if m == "TPL":
            if c == "\\" and i + 1 < n:
                out[i] = out[i + 1] = " "; i += 2; continue
            if c == "`":
                mode.pop(); i += 1; continue
            if c == "$" and i + 1 < n and text[i + 1] == "{":
                mode.append("TEX"); tex_depth.append(1)
                out[i] = out[i + 1] = " "; i += 2; continue
            out[i] = " " if c != "\n" else c
            i += 1
            continue
        # TEX — ${…} expression; nested braces tracked, strings inside NOT
        # re-entered (accepted limit, see module docstring).
        if c == "{":
            tex_depth[-1] += 1
        elif c == "}":
            tex_depth[-1] -= 1
            if tex_depth[-1] == 0:
                tex_depth.pop(); mode.pop()
        out[i] = " " if c != "\n" else c
        i += 1
    return "".join(out)


def _read_string_at(text: str, clean: str, pos: int) -> tuple[str, int] | None:
    """First string literal at/after ``pos`` in ``clean`` (skipping only
    whitespace) → (raw content from *text*, index after the literal)."""
    n = len(clean)
    j = pos
    while j < n and clean[j] in " \t\r\n":
        j += 1
    if j >= n or clean[j] not in "'\"`":
        return None
    q = clean[j]
    k = clean.find(q, j + 1)
    if k < 0:
        return None
    raw = text[j + 1:k]
    raw = _TPL_EXPR_RE.sub(":param", raw)
    raw = raw.replace("\\'", "'").replace('\\"', '"')
    return raw.strip(), k + 1


def _head_kind(head: str) -> str | None:
    """'describe' | 'test' for a whitelisted head, else None."""
    tokens = [t for t in re.split(r"[.\s(]+", head) if t]
    if not tokens or any(t not in _HEAD_TOKENS for t in tokens):
        return None
    return "describe" if "describe" in tokens else "test"


def _body_span(clean: str, open_paren: int) -> tuple[int, int] | None:
    """Span of the callback body ``{…}`` for a head whose ``(`` sits at
    ``open_paren``. The LAST depth-1 brace span before the call closes:
    the callback is always the final argument, so a playwright options
    object (``test('x', { tag: '@a' }, async () => {…})``) never wins."""
    depth = 0
    i = open_paren
    n = len(clean)
    last: tuple[int, int] | None = None
    while i < n:
        c = clean[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return last  # None ⇒ e.g. test('x', fnRef) — no inline body
        elif c == "{" and depth == 1:
            brace = 0
            j = i
            while j < n:
                if clean[j] == "{":
                    brace += 1
                elif clean[j] == "}":
                    brace -= 1
                    if brace == 0:
                        break
                j += 1
            last = (i, min(j + 1, n))
            i = last[1]
            continue
        i += 1
    return last


def _normalize_url(raw: str) -> str | None:
    """goto/visit/redirect arg → path, or None when not path-shaped."""
    u = raw.strip()
    if not u:
        return None
    if u.startswith(("http://", "https://")):
        rest = u.split("://", 1)[1]
        u = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    u = u.split("?", 1)[0].split("#", 1)[0]
    if not u.startswith("/") or u.startswith("//"):
        return None
    if len(u) <= 1 or " " in u:
        return None
    if _ASSET_EXT_RE.search(u):
        return None
    if not re.search(r"[A-Za-z]", u):
        return None
    return u


def _harvest_urls(body: str) -> tuple[list[dict[str, str]], set[str]]:
    """Ordered goto/click/fill steps + every path-shaped literal."""
    steps: list[tuple[int, dict[str, str]]] = []
    urls: set[str] = set()
    for m in _URL_ARG_RE.finditer(body):
        u = _normalize_url(_TPL_EXPR_RE.sub(":param", m.group(2)))
        if u:
            urls.add(u)
            steps.append((m.start(), {"kind": "goto", "arg": u}))
    for m in _REDIRECT_RE.finditer(body):
        u = _normalize_url(_TPL_EXPR_RE.sub(":param", m.group(2)))
        if u:
            urls.add(u)
    for m in _CLICK_RE.finditer(body):
        steps.append((m.start(), {"kind": "click", "arg": m.group(2).strip()}))
    for m in _FILL_RE.finditer(body):
        steps.append((m.start(), {"kind": "fill", "arg": m.group(2).strip()}))
    # bare path-shaped string literals (hrefs, expect(page).toHaveURL …)
    for m in re.finditer(r"(['\"`])(/[^\s'\"`]*)\1", body):
        u = _normalize_url(_TPL_EXPR_RE.sub(":param", m.group(2)))
        if u:
            urls.add(u)
    steps.sort(key=lambda t: t[0])
    return [s for _, s in steps], urls


def _parse_spec(text: str, rel: str, runner_project: str) -> list[E2EJourney]:
    clean = _mask(text)
    journeys: list[E2EJourney] = []
    stack: list[tuple[int, str]] = []  # (body_close_idx, title)
    for m in _HEAD_RE.finditer(clean):
        kind = _head_kind(clean[m.start():m.end()])
        if kind is None:
            continue
        while stack and stack[-1][0] <= m.start():
            stack.pop()
        open_paren = m.end() - 1
        got = _read_string_at(text, clean, m.end())
        if got is None:
            continue  # dynamic / non-string title — skipped, documented
        title = got[0]
        span = _body_span(clean, open_paren)
        if kind == "describe":
            if span is not None and title:
                stack.append((span[1], title))
            continue
        body = text[span[0]:span[1]] if span else ""
        steps, urls = _harvest_urls(body)
        journeys.append(E2EJourney(
            file=rel,
            title_chain=tuple([t for _, t in stack] + [title]),
            steps=steps,
            urls_touched=sorted(urls),
            runner_project=runner_project,
        ))
    return journeys


def _runner_project(path: Path, repo_root: Path) -> tuple[str, str]:
    """(repo-relative runner dir, runner name) via nearest config."""
    for parent in path.parents:
        for cfg in _RUNNER_CONFIGS:
            if (parent / cfg).is_file():
                runner = "cypress" if "cypress" in cfg else "playwright"
                try:
                    return parent.relative_to(repo_root).as_posix() or ".", runner
                except ValueError:
                    return ".", runner
        if parent == repo_root:
            break
    return ".", "unknown"


def extract_e2e_journeys(
    repo_root: Path,
) -> tuple[list[E2EJourney], dict[str, Any]]:
    """Discover + parse every e2e spec under ``repo_root``.

    Discovery is CONTENT-gated (not path-gated): a candidate ``*.spec.*``
    / ``*.cy.*`` / ``*.test.*`` file counts only when it imports
    ``@playwright/test`` or drives ``cy.*`` — so typebot's specs under
    ``src/test/`` are found while vitest/jest unit specs are not.
    """
    repo_root = Path(repo_root)
    specs: list[Path] = []
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file() or not _SPEC_SUFFIX_RE.search(path.name):
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        specs.append(path)

    journeys: list[E2EJourney] = []
    projects: dict[str, dict[str, Any]] = {}
    n_spec_files = 0
    for path in specs:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        is_pw = _PLAYWRIGHT_MARKER in text
        is_cy = bool(_CY_CALL_RE.search(text)) and not is_pw
        if not (is_pw or is_cy):
            continue
        n_spec_files += 1
        rel = path.relative_to(repo_root).as_posix()
        proj_dir, runner = _runner_project(path, repo_root)
        if runner == "unknown":
            runner = "playwright" if is_pw else "cypress"
        proj = projects.setdefault(
            proj_dir, {"runner": runner, "spec_files": 0, "journeys": 0},
        )
        proj["spec_files"] += 1
        rows = _parse_spec(text, rel, proj_dir)
        proj["journeys"] += len(rows)
        journeys.extend(rows)

    telemetry = {
        "spec_files": n_spec_files,
        "journeys": len(journeys),
        "runner_projects": {k: projects[k] for k in sorted(projects)},
    }
    return journeys, telemetry


# --------------------------------------------------------------------------
# Stitching (deterministic, $0 LLM)
# --------------------------------------------------------------------------

_STOP_TOKENS = {
    # function words + assertion boilerplate only — never domain nouns
    "should", "can", "be", "able", "to", "the", "a", "an", "and", "or",
    "in", "on", "of", "for", "is", "it", "when", "with", "as", "not",
    "work", "works", "expected", "properly", "correctly", "correct",
    "test", "tests", "spec", "specs", "index",
}


def _fam(pattern: str, vocab: dict[str, Any], version_re: re.Pattern[str]) -> tuple[str, ...]:
    segs = _pattern_key_chain(pattern or "", vocab, version_re)
    return tuple(k for k in (normalize_anchor_key(s) for s in segs) if k)


def _fam_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """Subsequence-family overlap: len(shorter) when the shorter chain is
    an ordered subsequence of the longer, else 0.

    Why subsequence, not prefix equality: a route's dynamic segments are
    DROPPED by the key chain while the journey URL carries them as
    CONCRETE literals — ``/project/abc/integrations/webhooks`` (spec)
    vs ``/project/[ref]/integrations/webhooks`` (route) yields
    ``(project, abc, integration, webhook)`` vs
    ``(project, integration, webhook)``: the route fam is a subsequence
    of the journey fam. Order is preserved, so unrelated same-token
    surfaces don't collide; best-match picks the deepest overlap."""
    if not a or not b:
        return 0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    it = iter(long_)
    if all(tok in it for tok in short):
        return len(short)
    return 0


def _content_tokens(*chunks: str) -> set[str]:
    toks: set[str] = set()
    for chunk in chunks:
        for part in re.split(r"[^A-Za-z0-9]+", chunk or ""):
            key = normalize_anchor_key(part)
            for t in key.split("-"):
                if len(t) >= 3 and t not in _STOP_TOKENS:
                    toks.add(t)
    return toks


def _uf_get(uf: Any, name: str, default: Any = None) -> Any:
    if isinstance(uf, dict):
        return uf.get(name, default)
    return getattr(uf, name, default)


def _flow_uf_map(
    user_flows: list[Any], flows: list[Any],
) -> dict[str, str]:
    """flow uuid/name → uf_id, from BOTH directions (``Flow.user_flow_id``
    and ``UserFlow.member_flow_ids`` — members may be uuid or name)."""
    uf_ids = {str(_uf_get(u, "id") or "") for u in user_flows}
    out: dict[str, str] = {}
    for uf in user_flows:
        uid = str(_uf_get(uf, "id") or "")
        for m in _uf_get(uf, "member_flow_ids") or []:
            out.setdefault(str(m), uid)
    for fl in flows or []:
        target = str(_uf_get(fl, "user_flow_id") or "")
        if target and target in uf_ids:
            for key in (_uf_get(fl, "uuid"), _uf_get(fl, "name")):
                if key:
                    out.setdefault(str(key), target)
    return out


def _route_file_lane(
    routes_index: list[dict[str, Any]] | None,
    flows: list[Any],
    flow_to_uf: dict[str, str],
    vocab: dict[str, Any],
    version_re: re.Pattern[str],
) -> list[tuple[tuple[str, ...], str]]:
    """(route fam, uf_id) pairs via routes_index → handler file → flow →
    UF. This is the lane that BITES on real scans: ``UserFlow.routes``
    is empty on the current corpus (papermark 0/52, typebot 0/100,
    supabase 0/54 cold scans) while ``routes_index`` is populated."""
    if not routes_index:
        return []
    by_entry: dict[str, list[Any]] = {}
    by_path: dict[str, list[Any]] = {}
    for fl in flows or []:
        e = str(_uf_get(fl, "entry_point_file") or "")
        if e:
            by_entry.setdefault(e, []).append(fl)
        for p in _uf_get(fl, "paths") or []:
            by_path.setdefault(str(p), []).append(fl)

    def _uf_for_file(fpath: str) -> str | None:
        cands = by_entry.get(fpath)
        if not cands:
            # most-specific containing flow: fewest paths, then lex uuid
            cands = sorted(
                by_path.get(fpath, []),
                key=lambda f: (len(_uf_get(f, "paths") or []),
                               str(_uf_get(f, "uuid") or _uf_get(f, "name"))),
            )[:1]
        for fl in cands:
            for key in (_uf_get(fl, "uuid"), _uf_get(fl, "name")):
                uid = flow_to_uf.get(str(key or ""))
                if uid:
                    return uid
        return None

    pairs: list[tuple[tuple[str, ...], str]] = []
    for entry in routes_index:
        fam = _fam(str(entry.get("pattern") or ""), vocab, version_re)
        if not fam:
            continue
        uid = _uf_for_file(str(entry.get("file") or ""))
        if uid:
            pairs.append((fam, uid))
    return pairs


def stitch_journeys(
    journeys: list[E2EJourney],
    user_flows: list[Any],
    routes_index: list[dict[str, Any]] | None = None,
    flows: list[Any] | None = None,
) -> dict[str, Any]:
    """Match journeys → UFs. Route-family overlap first (two lanes:
    direct ``UserFlow.routes`` + routes_index→handler-file→flow→UF);
    content-token fallback ONLY when a journey has zero route evidence
    (``via="name"``)."""
    vocab = load_spine_vocab()
    version_re = re.compile(
        vocab.get("version_segment_pattern") or r"^v\d+$")

    uf_by_id: dict[str, Any] = {}
    uf_fams: list[tuple[Any, set[tuple[str, ...]]]] = []
    uf_toks: list[tuple[Any, set[str]]] = []
    for uf in user_flows:
        uf_by_id[str(_uf_get(uf, "id") or "")] = uf
        fams = {
            f for f in (
                _fam(r, vocab, version_re)
                for r in (_uf_get(uf, "routes") or [])
            ) if f
        }
        uf_fams.append((uf, fams))
        uf_toks.append((uf, _content_tokens(
            str(_uf_get(uf, "name") or ""),
            str(_uf_get(uf, "resource") or ""),
        )))
    flow_to_uf = _flow_uf_map(user_flows, flows or [])
    for fam, uid in _route_file_lane(
            routes_index, flows or [], flow_to_uf, vocab, version_re):
        uf = uf_by_id.get(uid)
        if uf is None:
            continue
        for cand, fams in uf_fams:
            if cand is uf:
                fams.add(fam)
                break

    matched: list[dict[str, Any]] = []
    orphans: list[E2EJourney] = []
    evidence: dict[str, dict[str, Any]] = {}

    for j in journeys:
        j_fams = {
            f for f in (_fam(u, vocab, version_re) for u in j.urls_touched)
            if f
        }
        best: tuple[int, str, Any] | None = None
        via = "route"
        if j_fams:
            for uf, fams in uf_fams:
                score = max(
                    (_fam_overlap(jf, ff) for jf in j_fams for ff in fams),
                    default=0,
                )
                if score > 0:
                    key = (score, str(_uf_get(uf, "id") or ""))
                    if best is None or (key[0], ) > (best[0], ) or (
                            key[0] == best[0] and key[1] < best[1]):
                        best = (score, key[1], uf)
        else:
            via = "name"
            stem = Path(j.file).stem.split(".")[0]
            j_tok = _content_tokens(" ".join(j.title_chain), stem,
                                    Path(j.file).parent.name)
            for uf, toks in uf_toks:
                shared = len(j_tok & toks)
                if shared > 0:
                    key = (shared, str(_uf_get(uf, "id") or ""))
                    if best is None or key[0] > best[0] or (
                            key[0] == best[0] and key[1] < best[1]):
                        best = (shared, key[1], uf)

        if best is None:
            orphans.append(j)
            continue
        _, uf_id, uf = best
        row = {
            "journey_id": j.journey_id,
            "file": j.file,
            "title_chain": list(j.title_chain),
            "uf_id": uf_id,
            "uf_name": str(_uf_get(uf, "name") or ""),
            "product_feature_id": _uf_get(uf, "product_feature_id"),
            "via": via,
            "score": best[0],
            "urls_touched": j.urls_touched,
        }
        matched.append(row)
        ev = evidence.setdefault(uf_id, {"journeys": [], "specs": set()})
        ev["journeys"].append(" > ".join(j.title_chain))
        ev["specs"].add(j.file)

    matched.sort(key=lambda r: r["journey_id"])
    uf_e2e_evidence = {
        k: {"journeys": sorted(v["journeys"]), "specs": sorted(v["specs"])}
        for k, v in sorted(evidence.items())
    }
    return {
        "matched": matched,
        "orphans": orphans,
        "uf_e2e_evidence": uf_e2e_evidence,
    }


def _naming_candidates(matched: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """W3-contract-shaped candidate dicts for matched UFs whose journey
    titles are informative. DATA ONLY — nothing here feeds the labeler;
    wiring into naming_contract is a coordinator decision."""
    by_uf: dict[str, dict[str, Any]] = {}
    for row in matched:
        titles = [t for t in row["title_chain"]
                  if t.strip().lower() not in _WEAK_TITLES]
        if not titles:
            # weak-title journey — file stem carries the signal instead
            stem = Path(row["file"]).stem.split(".")[0]
            titles = [normalize_anchor_key(stem).replace("-", " ")]
        cand = by_uf.setdefault(row["uf_id"], {
            "kind": "uf",
            "key": row["uf_id"],
            "current": row["uf_name"],
            "candidates": [],
            "context": {"source": "e2e", "specs": []},
        })
        top = titles[-1]
        if top and top not in cand["candidates"]:
            cand["candidates"].append(top)
        if row["file"] not in cand["context"]["specs"]:
            cand["context"]["specs"].append(row["file"])
    out = []
    for key in sorted(by_uf):
        c = by_uf[key]
        c["candidates"] = c["candidates"][:3]
        c["context"]["specs"] = sorted(c["context"]["specs"])
        out.append(c)
    return out


# --------------------------------------------------------------------------
# Stage entry
# --------------------------------------------------------------------------

def run_e2e_truth(
    repo_root: Path,
    user_flows: list[Any],
    routes_index: list[dict[str, Any]] | None = None,
    flows: list[Any] | None = None,
) -> dict[str, Any]:
    """Full stage: extract → stitch → payload (artifact-shaped).

    Returns the FULL payload; the caller stores a compact view in
    ``scan_meta["e2e_truth"]`` (see :func:`scan_meta_view`) and writes
    the payload as the per-scan artifact.
    """
    journeys, tele = extract_e2e_journeys(Path(repo_root))
    if not journeys:
        return {
            "enabled": True,
            "e2e_absent": True,
            "spec_files": tele["spec_files"],
            "journeys": 0,
            "runner_projects": tele["runner_projects"],
            "matched": [],
            "orphan_journeys": [],
            "uf_e2e_evidence": {},
            "naming_candidates": [],
            "counts": {"matched": 0, "orphans": 0, "match_rate": None},
        }
    stitched = stitch_journeys(journeys, user_flows, routes_index, flows)
    matched = stitched["matched"]
    orphans = sorted(
        (j.as_dict() for j in stitched["orphans"]),
        key=lambda d: d["journey_id"],
    )
    n = len(journeys)
    return {
        "enabled": True,
        "e2e_absent": False,
        "spec_files": tele["spec_files"],
        "journeys": n,
        "runner_projects": tele["runner_projects"],
        "matched": matched,
        "orphan_journeys": orphans,
        "uf_e2e_evidence": stitched["uf_e2e_evidence"],
        "naming_candidates": _naming_candidates(matched),
        "counts": {
            "matched": len(matched),
            "orphans": len(orphans),
            "match_rate": round(len(matched) / n, 3),
        },
    }


def scan_meta_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact ``scan_meta["e2e_truth"]`` view: counts + orphan titles
    (the recall-hole headline), full detail lives in the artifact."""
    titles = [
        " > ".join(o["title_chain"]) for o in payload["orphan_journeys"]
    ]
    return {
        "enabled": payload["enabled"],
        "e2e_absent": payload["e2e_absent"],
        "spec_files": payload["spec_files"],
        "journeys": payload["journeys"],
        "runner_projects": payload["runner_projects"],
        "counts": payload["counts"],
        "orphan_titles": titles[:50],
        # no silent caps: full list always lives in the stage artifact
        "orphan_titles_truncated": max(0, len(titles) - 50),
    }


# --------------------------------------------------------------------------
# Orphan-journey → synthesized UF (Track C — recall of maintainer journeys)
# --------------------------------------------------------------------------
#
# ``run_e2e_truth`` only REPORTS orphan journeys (recall holes the
# maintainer named). This half BRINGS THEM INTO DETECTION: each groundable
# orphan becomes a tagged, PF-bound ``UserFlow`` so the board/panel sees the
# journey. Deterministic, $0 LLM, purely additive.
#
# Trust-invariant contract (the reason these are safe to append):
#   * I21 (no orphan UF) — every minted UF carries a resolvable
#     ``product_feature_id``; an orphan whose routes bind to no PF is
#     DROPPED, never emitted null-bound.
#   * I7 (>=1 member flow) — these are RECALL HOLES: by definition no flow
#     was detected for them, so they are member-less. Tagged
#     ``synthesis_reason="e2e_journey_recall"`` — the SAME "member-less,
#     intentional, verifier-reviewable seed" class the D9-carve already
#     exempts for ``system_flow_recall`` (the carve set extends to this
#     reason; see the Track-C report). They carry ``routes`` (the
#     maintainer-navigated URLs) as their code-surface evidence.
#   * I15/I16/I19 — gated only for UFs with member flows; member-less by
#     construction ⇒ structurally exempt.
#   * I6 (no test-origin flow) — untouched: the flow graph is NOT mutated;
#     the spec file is never cited as a path/entry.
# ``binding_confidence="low"`` + ``name_confidence="low"`` flag every one
# for verifier review (the PF binding is only as good as the current PF
# segmentation).

E2E_ORPHAN_UF_ENV = "FAULTLINE_E2E_ORPHAN_UF"

#: Tag distinguishing e2e-authored recall seeds from the system-flow seeds
#: (``system_flow_recall``) and the PF-UF backstop reasons. Interactive by
#: nature (playwright drives a browser) — never a system journey.
E2E_ORPHAN_REASON = "e2e_journey_recall"

#: Cap on minted journeys per scan — a bound on additive output size, NOT a
#: tuned threshold (the grain is the maintainer's own journey label; this
#: only stops a pathological spec suite from flooding the board).
_ORPHAN_UF_CAP = 60


def orphan_uf_enabled() -> bool:
    """Default ON; ``FAULTLINE_E2E_ORPHAN_UF=0`` disables orphan→UF
    synthesis (output byte-identical to e2e-truth-report-only)."""
    return os.environ.get(E2E_ORPHAN_UF_ENV, "1").strip().lower() not in {
        "0", "false",
    }


# Negative / error-path journeys — the maintainer scripts these to assert a
# GUARD (auth denial, validation error, hierarchy invariant), not a product
# capability. Not UF-grade (e2emerge flagged the "verify role hierarchy
# after promotion" / "cannot promote non-existent user" class). A tiny,
# universal error-vocabulary honesty filter — never a domain vocabulary.
_NEG_TITLE_RE = re.compile(
    r"\b(cannot|can'?t|can\s?not|should\s+not|shouldn'?t|must\s+not|do\s+not|"
    r"don'?t|invalid|errors?|fails?|failure|failing|unauthori[sz]ed|"
    r"forbidden|rejects?|rejected|denied|deny|non-?existent|not\s+found|"
    r"not\s+allowed|prevents?|prevented|blocks?|blocked|disallow|"
    r"without\s+(?:auth|permission|access|login)|no\s+access|"
    r"verify\s+.*\bafter\b|handles?\s+.*\berror|negative)\b",
    re.IGNORECASE,
)

_LABEL_TAG_RE = re.compile(r"^\s*\[([^\]]+)\]")


def _is_negative_journey(title_chain: tuple[str, ...]) -> bool:
    return bool(_NEG_TITLE_RE.search(" > ".join(title_chain)))


def clean_route_pattern(pattern: str) -> str:
    """Framework-filename route pattern → clean URL path for family keying.

    Remix flat-routes encode the URL in the FILENAME: ``+`` terminates a
    folder segment, ``.`` separates nested URL segments, ``$param`` is
    dynamic, ``_index``/``_layout`` are markers, and a leading-underscore
    segment (``_authenticated``, ``_recipient``) is a PATHLESS layout group.
    The routes index stores that raw form, so its family key
    (``t-team-url``/``documents-index``) never matches the CLEAN URL an e2e
    spec navigates (``/t/:id/documents`` → ``t``/``document``). Normalizing
    the pattern here reconciles them. Already-clean patterns (Next
    ``[id]``, FastAPI ``{id}``, Rails ``:id``) pass through — their dynamic
    glyphs are dropped by ``_pattern_key_chain`` downstream — so this is a
    no-op for non-Remix stacks. [[rule-no-magic-tuning]]: pure structural
    normalization, no thresholds.
    """
    segs: list[str] = []
    for raw in str(pattern).split("/"):
        if not raw:
            continue
        for part in raw.split("."):
            part = part.rstrip("+")
            if not part:
                continue
            if part[0] in "$[{":       # $teamUrl / [id] / {id} dynamic → drop
                continue
            if part[0] == "_":          # _authenticated group, _index, _layout
                continue
            segs.append(part)
    return "/" + "/".join(segs)


def _journey_label(title_chain: tuple[str, ...]) -> str:
    """The maintainer's own journey label — the bracketed tag
    (``[BULK_ACTIONS]``) or the prefix before the first ``:`` / `` - `` /
    `` > `` separator. Collapses many ``it`` cases of one describe/tag into
    a single journey (the natural product grain)."""
    t = (title_chain[0] if title_chain else "").strip()
    m = _LABEL_TAG_RE.match(t)
    if m:
        return m.group(1).strip()
    for sep in (" - ", ": ", ":", " > "):
        if sep in t:
            head = t.split(sep, 1)[0].strip()
            if head:
                return head
    return t


def _label_to_name(label: str) -> str:
    """Provisional display name from a journey label. ALL-CAPS / snake tags
    (``BULK_ACTIONS``) title-case; already-phrased labels
    (``Find Documents UI``) are kept. The naming contract polishes/overrides
    downstream via the authored channel."""
    s = re.sub(r"[_\-]+", " ", label).strip()
    if not s:
        return label.strip() or "Journey"
    if s.replace(" ", "").isupper() or s.islower():
        s = s.title()
    return s


#: UNIVERSAL CRUD/action VERBS + framework route-structure markers (Next/Remix
#: ``index``/``layout``) — never the product NOUN a journey is "about". Skipped
#: when picking a UF resource from a route family (``(t, document, edit)`` →
#: ``document``). Universal-only, NO repo/domain vocabulary — a domain noun a
#: SPECIFIC repo happens to route on (documenso's ``envelope``/``folder``/sign-
#: status segments) is a legitimate resource elsewhere; hardcoding it here would
#: violate [[rule-no-repo-specific-paths]] / [[rule-no-magic-tuning]]. Version
#: segments (``v1``…) never reach here — ``_pattern_key_chain`` drops them.
_ACTION_SEGMENTS = frozenset({
    "create", "new", "add", "edit", "update", "delete", "remove",
    "list", "view", "index", "layout",
})


def _resource_from_fam(fam: tuple[str, ...]) -> str | None:
    """Deepest product-noun segment of a route family. Skips generic
    action verbs / framework markers (``_ACTION_SEGMENTS``) and single-char
    route shorthands (Remix folder-route ``f`` etc. — never a product noun;
    a universal length rule, not a vocabulary)."""
    for tok in reversed(fam):
        if len(tok) > 1 and tok not in _ACTION_SEGMENTS:
            return tok
    return fam[-1] if fam else None


_AUTHOR_RE = re.compile(
    r"\b(create|creating|add|adding|upload|uploading|new|sign\s?up|signup|"
    r"register|compose|draft|generate|import)\b", re.IGNORECASE)
_BROWSE_RE = re.compile(
    r"\b(view|viewing|browse|browsing|list|find|finding|search|searching|"
    r"see|render|rendering|display|visible|preview|open|read)\b",
    re.IGNORECASE)
_MANAGE_RE = re.compile(
    r"\b(delete|deleting|remove|edit|editing|update|updating|manage|"
    r"managing|rename|move|moving|promote|assign|configure|settings|"
    r"archive|restore|duplicate|share|resend)\b", re.IGNORECASE)
_BULK_RE = re.compile(
    r"\b(bulk|multiple|batch|select\s+all|checkbox(?:es)?)\b", re.IGNORECASE)
_EXPORT_RE = re.compile(r"\b(export|download|downloading)\b", re.IGNORECASE)


def _journey_intent(text: str) -> str:
    """UF intent from the journey text (author|browse|manage|bulk|export|
    execute). Deterministic verb heuristic over a universal action
    vocabulary — never a domain vocabulary."""
    if _BULK_RE.search(text):
        return "bulk"
    if _EXPORT_RE.search(text):
        return "export"
    if _AUTHOR_RE.search(text):
        return "author"
    if _MANAGE_RE.search(text):
        return "manage"
    if _BROWSE_RE.search(text):
        return "browse"
    return "execute"


def _pf_key(pf: Any) -> str:
    return str(_uf_get(pf, "id", None) or _uf_get(pf, "name", "") or "")


def synthesize_orphan_journeys(
    payload: dict[str, Any],
    product_features: list[Any],
    developer_features: list[Any],
    routes_index: list[dict[str, Any]] | None,
    user_flows: list[Any],
) -> dict[str, Any]:
    """Mint tagged, PF-bound ``UserFlow`` rows from groundable orphan
    journeys. Deterministic, $0 LLM, additive.

    Pipeline: filter negative/error journeys → key each journey's URLs to
    route families (Remix-normalized) → resolve to handler files →
    majority file→PF owner (drop if unbound: I21) → group by
    ``(PF, journey-label)`` → one tagged UF per group.

    Returns ``{"minted": [(UserFlow, [authored_titles]), ...],
    "tele": {...}}``. The caller appends the UFs and renumbers the
    provisional ``UF-000`` ids.
    """
    from faultline.models.types import UserFlow

    orphans = payload.get("orphan_journeys") or []
    tele: dict[str, Any] = {
        "enabled": True,
        "orphans_in": len(orphans),
        "filtered_negative": 0,
        "dropped_no_route_ev": 0,
        "dropped_unbound_pf": 0,
        "dropped_dup_existing": 0,
        "groups": 0,
        "minted": 0,
        "minted_names": [],
        "capped": 0,
    }
    if not orphans:
        return {"minted": [], "tele": tele}

    vocab = load_spine_vocab()
    version_re = re.compile(vocab.get("version_segment_pattern") or r"^v\d+$")

    # file → PF owner (dev features' owned paths; shared/facet excluded).
    from faultline.pipeline_v2.conservation import (
        build_file_pf_owner, dev_views_for,
    )
    pf_keys = frozenset(_pf_key(p) for p in product_features) - {""}
    file_pf = build_file_pf_owner(
        dev_views_for(developer_features), real_pf_keys=pf_keys)

    # route family → handler files (normalized pattern).
    route_fam_files: dict[tuple[str, ...], set[str]] = {}
    for r in routes_index or []:
        fam = _fam(clean_route_pattern(r.get("pattern") or ""),
                   vocab, version_re)
        if fam:
            route_fam_files.setdefault(fam, set()).add(str(r.get("file") or ""))

    # Existing UF surfaces to dedup against: (pf_key, resource-key).
    existing_surfaces: set[tuple[str, str]] = set()
    for uf in user_flows:
        pfk = str(_uf_get(uf, "product_feature_id", "") or "")
        res = normalize_anchor_key(str(_uf_get(uf, "resource", "") or ""))
        if pfk and res:
            existing_surfaces.add((pfk, res))

    def _resolve(
        o: dict[str, Any],
    ) -> tuple[str | None, tuple[str, ...] | None, set[str]]:
        """(bound PF key, dominant route family, resolved route-handler
        files) for one orphan, or (None, _, files). B23: the files used to
        vote the PF owner are RETURNED (previously discarded) — they are
        the journey's code surface and feed the marker's ``surface_files``
        coordinates downstream (``synth_quality.attach_marker_surface_coords``)."""
        j_fams = {
            f for f in (_fam(u, vocab, version_re)
                        for u in (o.get("urls_touched") or [])) if f
        }
        files: set[str] = set()
        overlapping: set[tuple[str, ...]] = set()
        for jf in j_fams:
            for rfam, rfiles in route_fam_files.items():
                if _fam_overlap(jf, rfam) > 0:
                    files |= rfiles
                    overlapping.add(rfam)
        if not overlapping:
            return None, None, files
        # dominant = deepest resolved route family; TOTAL-ORDER tie-break
        # (length, then tuple) so the pick is independent of set-iteration
        # order (PYTHONHASHSEED-invariant) — never bare max(..., key=len).
        dominant = max(overlapping, key=lambda t: (len(t), t))
        owners: dict[str, int] = {}
        for f in files:
            owner = file_pf.get(f)
            if owner:
                owners[owner] = owners.get(owner, 0) + 1
        if not owners:
            return None, dominant, files
        # majority owner; ties broken lexicographically (determinism).
        best = sorted(owners.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        return best, dominant, files

    # Group orphans by (PF, journey label). Insertion order is stable
    # (orphans arrive sorted by journey_id from the payload).
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for o in orphans:
        chain = tuple(o.get("title_chain") or [])
        if _is_negative_journey(chain):
            tele["filtered_negative"] += 1
            continue
        pfk, dominant, rfiles = _resolve(o)
        if dominant is None:
            tele["dropped_no_route_ev"] += 1
            continue
        if not pfk:
            tele["dropped_unbound_pf"] += 1
            continue
        label = _journey_label(chain)
        key = (pfk, normalize_anchor_key(label) or label.lower())
        g = groups.setdefault(key, {
            "pf": pfk, "label": label, "titles": [], "urls": set(),
            "fams": set(), "files": set(),
        })
        g["titles"].append(" > ".join(chain))
        g["urls"].update(o.get("urls_touched") or [])
        if dominant:
            g["fams"].add(dominant)
        g["files"] |= rfiles  # B23 — journey code surface, kept for coords

    tele["groups"] = len(groups)
    minted: list[tuple[Any, list[str]]] = []
    for key in sorted(groups):
        g = groups[key]
        pfk = g["pf"]
        # dominant family = the deepest resolved route family in the group;
        # TOTAL-ORDER tie-break (length, then tuple) — g["fams"] is a set, so
        # a bare max(..., key=len) would be PYTHONHASHSEED-dependent on ties.
        fam = max(g["fams"], key=lambda t: (len(t), t)) if g["fams"] else ()
        resource = _resource_from_fam(fam) if fam else None
        if not resource:
            resource = normalize_anchor_key(g["label"]).split("-")[-1] or "journey"
        if (pfk, normalize_anchor_key(resource)) in existing_surfaces:
            tele["dropped_dup_existing"] += 1
            continue
        if len(minted) >= _ORPHAN_UF_CAP:
            tele["capped"] += 1
            continue
        name = _label_to_name(g["label"])
        intent = _journey_intent(" ".join(g["titles"][:6]) + " " + name)
        routes = sorted(g["urls"])
        uf = UserFlow(
            id="UF-000",                 # provisional — caller renumbers
            name=name,
            resource=resource,
            domain=None,
            product_feature_id=pfk,
            intent=intent,
            member_flow_ids=[],
            member_count=0,
            routes=routes,
            refined=True,                # 6.7b already ran; skip re-refine
            category="interactive",      # playwright drives a browser
            name_confidence="low",
            binding_confidence="low",
            synthesized=True,
            synthesis_reason=E2E_ORPHAN_REASON,
            # B23 — the resolver's route-handler files (the journey's code
            # surface). NEVER serialized (excluded field): consumed by
            # ``synth_quality.attach_marker_surface_coords`` at 6.98, which
            # applies the claimed-file + measured-loc honesty gates before
            # any span reaches the output.
            surface_candidate_files=sorted(
                f for f in g["files"] if f) or None,
        )
        minted.append((uf, g["titles"]))
        if len(tele["minted_names"]) < 50:
            tele["minted_names"].append(f"{name} → {pfk}")

    tele["minted"] = len(minted)
    return {"minted": minted, "tele": tele}


def matched_authored_names(payload: dict[str, Any]) -> dict[str, list[str]]:
    """``{uf_id: [authored journey labels]}`` for MATCHED journeys — the
    maintainer's own names, fed to the naming contract's authored channel
    so an existing UF's display can prefer the authored label over a
    derived template (Track C-2, authored priority; deterministic).

    ROUTE-EVIDENCE ONLY: a journey is used to name a UF exactly when it was
    matched by route-family overlap (``via == "route"``). Content-token
    name-fallback matches (``via == "name"``) are the engine's WEAKEST tie
    (documenso stitches API journeys onto a "forks" UF that way) — feeding
    their titles would MISNAME the UF, so they are excluded. Negative/error
    and weak journey titles are also dropped.
    """
    per_uf: dict[str, list[str]] = {}
    for row in payload.get("matched") or []:
        if row.get("via") != "route":
            continue
        uid = str(row.get("uf_id") or "")
        if not uid:
            continue
        chain = tuple(row.get("title_chain") or [])
        if not chain or _is_negative_journey(chain):
            continue
        if " > ".join(chain).strip().lower() in _WEAK_TITLES:
            continue
        lbl = _label_to_name(_journey_label(chain))
        if not lbl or lbl.strip().lower() in _WEAK_TITLES:
            continue
        labels = per_uf.setdefault(uid, [])
        if lbl not in labels:
            labels.append(lbl)
    return {uid: labels[:3] for uid, labels in sorted(per_uf.items()) if labels}
