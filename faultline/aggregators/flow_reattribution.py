"""Flow re-attribution by name-token match.

Primary scan's flow_judge sometimes attaches a flow to the wrong
feature when both features share some semantic neighbourhood. Real
example: documenso's ``enterprise-billing-flow`` was attached to
``organisation-management`` instead of the standalone
``ee/enterprise-billing`` feature.

This aggregator post-processes the built FeatureMap and MOVES each
flow to whichever feature's name best matches the flow's name
tokens. Pure deterministic — works on any stack.

Algorithm:

  For each flow ``F`` attached to feature ``A``:
    1. Tokenise the flow name (verb-noun split, lowercase, drop
       stop tokens including the ``-flow`` suffix).
    2. Score each feature ``X`` in the FeatureMap by how many of
       the flow's tokens appear in ``X``'s tokenised name.
    3. If some ``B != A`` has a strictly higher score AND that
       score is meaningful (≥ 1 token match), move ``F`` to ``B``.

Conservative when ties — keep the flow with the current owner so
we don't churn placements. The win-by-strict-greater rule ensures
deterministic output regardless of feature iteration order.

Generic per ``memory/rule-no-repo-specific-paths`` and
``memory/rule-no-magic-tuning`` — no per-stack hardcoding, no
magic-number thresholds, only structural rules (token-set
intersection + strict ordering).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# Tokens that carry no domain meaning — skip during scoring so a
# flow named ``manage-billing-flow`` doesn't artificially "match"
# every feature whose name contains ``flow``.
_STOP_TOKENS = frozenset({
    "flow", "the", "a", "an", "of", "for", "and", "to", "in", "on",
    "manage", "use", "configure",
})


def _tokens(s: str) -> set[str]:
    """Tokenise a name into lowercase content tokens.

    Splits on CamelCase boundaries plus ``- _ / . space``. Stop
    tokens are filtered. Tokens shorter than 3 chars are dropped
    so two-letter accidents (``ee``, ``ui``) don't cause spurious
    matches.
    """
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)
    spaced = re.sub(r"[-_/.\s]+", " ", spaced).lower()
    return {
        t for t in spaced.split()
        if t and t not in _STOP_TOKENS and len(t) >= 3
    }


def _score(feature_name: str, flow_tokens: set[str]) -> int:
    """How many flow tokens appear in the feature name's tokens."""
    return len(_tokens(feature_name) & flow_tokens)


@dataclass(slots=True)
class FlowReattribution:
    """Move flows whose name better matches a different feature.

    Mutates ``feature_map.features[*].flows`` in place. Returns the
    number of flows moved.
    """

    def reattribute(self, feature_map) -> int:
        feature_names = [f.name for f in feature_map.features]
        if len(feature_names) < 2:
            return 0

        by_name = {f.name: f for f in feature_map.features}

        # Collect (current_owner_name, flow, new_owner_name) moves
        # before mutating, so concurrent iteration is safe.
        pending_moves: list[tuple[str, object, str]] = []

        for feat in feature_map.features:
            flows = list(feat.flows or [])
            for flow in flows:
                f_tokens = _tokens(flow.name)
                if not f_tokens:
                    continue
                current_score = _score(feat.name, f_tokens)
                best_alt_score = current_score
                best_alt_name = feat.name
                for alt_name in feature_names:
                    if alt_name == feat.name:
                        continue
                    s = _score(alt_name, f_tokens)
                    if s > best_alt_score:
                        best_alt_score = s
                        best_alt_name = alt_name
                # Move only when a strictly-better alt exists AND
                # it has at least one real token match.
                if best_alt_name != feat.name and best_alt_score >= 1:
                    pending_moves.append((feat.name, flow, best_alt_name))

        if not pending_moves:
            return 0

        # Apply moves: drop from current owners, append to new owners.
        for current_owner_name, flow, new_owner_name in pending_moves:
            current = by_name.get(current_owner_name)
            new_owner = by_name.get(new_owner_name)
            if current is None or new_owner is None:
                continue
            # Remove by identity (flow object) from current owner.
            current.flows = [fl for fl in current.flows if fl is not flow]
            new_owner.flows = list(new_owner.flows) + [flow]

        logger.info(
            "flow-reattribution: moved %d flow(s) to better-matching features",
            len(pending_moves),
        )
        return len(pending_moves)


__all__ = ["FlowReattribution", "_tokens", "_score"]
