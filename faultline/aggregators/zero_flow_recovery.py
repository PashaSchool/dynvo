"""Zero-flow recovery (Sprint 6.3).

Many features come out of the pipeline with zero flows attached.
This happens for two reasons:

  1. **Library shape** — better-auth, fastapi, axios, apprise have
     no routes/pages, so the flow detector returns nothing. Library
     APIs ARE the user-facing surface; each public exported callable
     is a developer-integration journey.

  2. **Critique-discovered features** — recall critique appends
     features as ``Feature`` objects with empty ``flows``. The
     primary flow detector never gets a chance to see them.

Both cases leave the dashboard with feature cards that say
"no flows detected" — useless for engineers and PMs alike.

This aggregator walks every feature whose ``flows`` is empty,
parses each path for exported callables, and synthesises one
``Flow`` per callable. Pure deterministic — no LLM cost.

Generic per ``memory/rule-no-repo-specific-paths`` /
``rule-no-magic-tuning``:

  - Works on .ts, .tsx, .js, .jsx, .py, .rb files (the
    languages our extractors already support).
  - Caps emitted flows per feature using the same
    ``FlowConsolidator``-shaped MAX (default 5) so the recovered
    flow lists stay readable.
  - Skips obvious non-callables (private functions starting with
    ``_``, internal helpers, test fixtures, type-only declarations).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# NO default cap. Per memory/rule-no-magic-tuning, the recovered
# count is whatever the source-file callable extraction yields —
# that IS the structural truth for this feature. Caller can pass
# ``max_flows_per_feature`` explicitly for ad-hoc CLI use, but the
# engine's normal pipeline uses None (no cap).


# Patterns to find exported callables in TypeScript / JavaScript.
# Matches:
#   export async function NAME(
#   export function NAME(
#   export const NAME = async (...) => / function (
#   export class NAME ... { method NAME() }
_TS_EXPORT_FN_RE = re.compile(
    r"""
    \bexport\s+
    (?:default\s+)?
    (?:async\s+)?
    (?:function\s+(?P<n1>[A-Za-z_$][A-Za-z0-9_$]*)
       | (?:const|let|var)\s+(?P<n2>[A-Za-z_$][A-Za-z0-9_$]*)
         (?:\s*:\s*[^=]+?)?\s*=\s*(?:async\s+)?
         (?:function\b | \([^)]*\)\s*=> | [A-Za-z_$][A-Za-z0-9_$]*\s*=>)
       | class\s+(?P<n3>[A-Za-z_$][A-Za-z0-9_$]*)
    )
    """,
    re.VERBOSE,
)

# Python: def NAME(  / async def NAME(  / class NAME
_PY_DEF_RE = re.compile(
    r"""
    ^[ \t]*
    (?:async\s+)?
    (?:def\s+(?P<n1>[A-Za-z_][A-Za-z0-9_]*)
       | class\s+(?P<n2>[A-Za-z_][A-Za-z0-9_]*))
    """,
    re.VERBOSE | re.MULTILINE,
)

# Ruby: def NAME / class NAME / module NAME
_RB_DEF_RE = re.compile(
    r"""
    ^[ \t]*
    (?:def\s+(?:self\.)?(?P<n1>[A-Za-z_][A-Za-z0-9_!?=]*)
       | class\s+(?P<n2>[A-Z][A-Za-z0-9_]*)
       | module\s+(?P<n3>[A-Z][A-Za-z0-9_]*))
    """,
    re.VERBOSE | re.MULTILINE,
)


# Names that are pure scaffolding / non-action and shouldn't become flows.
_SKIP_NAMES = frozenset({
    "default", "main", "app", "init", "__init__",
    "config", "constants", "types",
    "render", "Page", "Layout", "loader", "action",
    "metadata", "viewport",
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
    "handler", "handle",
    # React / TS noise
    "Component", "memo", "forwardRef",
    # Test fixtures
    "describe", "it", "test", "expect", "beforeEach", "afterEach",
    "fixture", "setup", "teardown",
})


# React hook convention: ``useFoo`` (lowercase ``use`` followed by
# an uppercase letter) is a React hook, not a user-facing flow.
# Skip them entirely from zero-flow recovery.
_REACT_HOOK_RE = re.compile(r"^use[A-Z]")


def _is_react_hook(name: str) -> bool:
    return bool(_REACT_HOOK_RE.match(name))


def _humanize_callable(name: str) -> str:
    """Convert a callable name into a flow-style verb-noun phrase.

    Examples:
      ``createInvoice`` → ``create-invoice-flow``
      ``getUserById``   → ``get-user-by-id-flow``
      ``snake_method``  → ``snake-method-flow``
      ``ClassName``     → ``manage-class-name-flow`` (class verbs are implicit)
    """
    # Insert dashes at CamelCase boundaries.
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", name)
    spaced = re.sub(r"[_\s]+", "-", spaced).lower()
    spaced = re.sub(r"-+", "-", spaced).strip("-")
    if not spaced:
        return ""
    # If first token isn't a verb, prepend "use" so it reads
    # like a flow rather than a noun.
    first = spaced.split("-", 1)[0]
    verb_starts = {
        "create", "make", "add", "new",
        "edit", "update", "modify", "change", "rename", "patch",
        "delete", "remove", "drop", "destroy",
        "get", "find", "fetch", "load", "read", "list", "browse",
        "search", "query", "filter",
        "save", "store", "persist",
        "send", "deliver", "submit", "post",
        "verify", "validate", "confirm", "check",
        "configure", "setup", "init", "register",
        "connect", "link", "integrate", "sync",
        "export", "import",
        "use", "run", "execute", "call", "invoke", "apply",
        "build", "render", "generate",
        "open", "close", "toggle",
        "handle", "process",
        # Lifecycle verbs (Sprint 6.3 corpus tuning)
        "cancel", "renew", "pause", "resume", "start", "stop",
        "enable", "disable", "activate", "deactivate",
        "charge", "refund", "approve", "reject", "review",
        "publish", "archive", "restore", "revoke",
        "upload", "download", "stream", "broadcast",
        "subscribe", "unsubscribe", "follow", "unfollow",
        "schedule", "trigger", "fire", "dispatch",
    }
    # If first token isn't a verb, prepend "manage" — reads more
    # human than "use-X" (which looks like a React hook to readers).
    if first not in verb_starts:
        spaced = f"manage-{spaced}"
    if not spaced.endswith("-flow"):
        spaced = f"{spaced}-flow"
    return spaced


def _extract_callables_from_file(path: Path) -> list[str]:
    """Return exported callable names found in the given file."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    out: list[str] = []
    seen: set[str] = set()

    if path.suffix in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        for m in _TS_EXPORT_FN_RE.finditer(text):
            name = m.group("n1") or m.group("n2") or m.group("n3")
            if name and name not in seen and name not in _SKIP_NAMES:
                seen.add(name)
                out.append(name)
    elif path.suffix == ".py":
        for m in _PY_DEF_RE.finditer(text):
            name = m.group("n1") or m.group("n2")
            # Python convention: leading underscore is private — skip.
            if not name or name.startswith("_") or name in _SKIP_NAMES:
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
    elif path.suffix == ".rb":
        for m in _RB_DEF_RE.finditer(text):
            name = m.group("n1") or m.group("n2") or m.group("n3")
            if not name or name in _SKIP_NAMES:
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append(name)
    return out


@dataclass(slots=True)
class ZeroFlowRecovery:
    """Synthesise flows for features whose ``flows`` list is empty.

    Reads each path of the feature, extracts exported/public
    callables, converts each into a ``flow_name`` via verb-noun
    humanisation, and appends ``Flow`` objects to the feature.

    Caller passes the FeatureMap and the repo root (so paths can
    be resolved on disk).
    """

    max_flows_per_feature: int | None = None

    def recover(self, feature_map, repo_root: Path) -> tuple[int, int]:
        """In-place mutation. Returns
        ``(features_recovered, flows_added)``.

        A feature is "recovered" iff it had 0 flows AND we added
        at least 1.
        """
        from datetime import datetime, timezone
        from faultline.models.types import Flow

        repo_root = Path(repo_root)
        features_recovered = 0
        flows_added = 0
        now = datetime.now(tz=timezone.utc)

        cap = self.max_flows_per_feature  # may be None = no cap

        def _at_cap(n: int) -> bool:
            return cap is not None and n >= cap

        for feat in feature_map.features:
            if feat.flows:
                continue  # already has flows
            seen_flow_names: set[str] = set()
            new_flows: list = []
            for rel_path in (feat.paths or []):
                if _at_cap(len(new_flows)):
                    break
                p = repo_root / rel_path
                if not p.is_file():
                    continue
                for callable_name in _extract_callables_from_file(p):
                    flow_name = _humanize_callable(callable_name)
                    if not flow_name or flow_name in seen_flow_names:
                        continue
                    seen_flow_names.add(flow_name)
                    new_flows.append(Flow(
                        name=flow_name,
                        description=(
                            f"Recovered from {callable_name}() "
                            f"in {rel_path}"
                        ),
                        paths=[rel_path],
                        authors=[],
                        total_commits=0,
                        bug_fixes=0,
                        bug_fix_ratio=0.0,
                        last_modified=now,
                        health_score=99.0,
                    ))
                    if _at_cap(len(new_flows)):
                        break
            # Fallback when no callables were extracted: emit ONE
            # generic "manage-<feature-name>-flow" so the dashboard
            # never displays a feature card with literally zero
            # flows. Critique-discovered features whose paths are
            # configs / docs / SQL fall into this bucket.
            if not new_flows:
                fallback_name = _humanize_callable(feat.name)
                if fallback_name and fallback_name not in {fl.name for fl in (feat.flows or [])}:
                    new_flows.append(Flow(
                        name=fallback_name,
                        description=(
                            f"Recovered: feature surface for {feat.name} "
                            f"(no public callables matched the heuristic)"
                        ),
                        paths=list(feat.paths or [])[:1],
                        authors=[],
                        total_commits=0,
                        bug_fixes=0,
                        bug_fix_ratio=0.0,
                        last_modified=now,
                        health_score=99.0,
                    ))

            if new_flows:
                feat.flows = new_flows
                features_recovered += 1
                flows_added += len(new_flows)

        if features_recovered:
            logger.info(
                "zero-flow-recovery: recovered %d feature(s), %d flow(s)",
                features_recovered, flows_added,
            )
        return features_recovered, flows_added


__all__ = [
    "ZeroFlowRecovery",
    "_extract_callables_from_file",  # exposed for tests
    "_humanize_callable",
]
