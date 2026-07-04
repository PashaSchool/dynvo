"""Stage 6.7e — cross-resource semantic normalization (MISSION-92 cycle-3).

Covers the deterministic resource-family pre-fold that runs BEFORE the LLM
grouping call: token-prefix families, plural/singular + kebab variants,
vendor prefix/suffix folding (public vendor vocabulary — provenance =
``naming_validator.VENDOR_TOKENS`` + the module's additive public-brand
set), generic-head skipping, candidate pre-grouping fed to the prompt, and
the unchanged over-merge guards (token-consistency eviction still trips on
"Manage everything" blobs).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from faultline.models.types import UserFlow
from faultline.pipeline_v2.stage_6_7e_uf_lattice import (
    LATTICE_CACHE_VERSION,
    _build_from_llm_specs,
    _candidate_digest,
    _prefold_candidates,
    build_uf_lattice_llm,
    resource_family,
)


def _uf(uf_id: str, name: str, *, resource: str = "link",
        intent: str = "author", members: list[str] | None = None) -> UserFlow:
    members = members if members is not None else [f"{uf_id}-flow"]
    return UserFlow(
        id=uf_id, name=name, intent=intent, resource=resource,
        member_flow_ids=members, member_count=len(members),
        routes=[f"/{resource}"],
    )


# ── resource_family: token-prefix / plural / kebab fold ─────────────────────


def test_token_prefix_family_folds_route_variants() -> None:
    # The mission's canonical case: three route-grain-unique nouns, one family.
    assert resource_family("link") == "link"
    assert resource_family("link-tags") == "link"
    assert resource_family("links-analytics") == "link"


def test_plural_singular_and_kebab_variants_fold() -> None:
    assert resource_family("pages") == resource_family("page")
    assert resource_family("page_type") == resource_family("page-type")
    assert resource_family("Page Types") == "page"


def test_subtype_and_verb_head_fold_to_same_family() -> None:
    # "page", "page-type", "delete-page-type" = same product surface family.
    fams = {resource_family(r) for r in ("page", "page-type", "delete-page-type")}
    assert fams == {"page"}


def test_generic_scaffold_head_skipped() -> None:
    assert resource_family("api-cron-export-commission") == "cron"
    assert resource_family("custom-domain") == "domain"
    # ...but a lone generic token keeps itself (never returns "").
    assert resource_family("api") == "api"


def test_empty_resource_has_empty_family() -> None:
    assert resource_family(None) == ""
    assert resource_family("  ") == ""


# ── resource_family: vendor prefix/suffix fold ──────────────────────────────


def test_vendor_prefix_and_suffix_fold_to_core() -> None:
    assert resource_family("stripe-webhook") == "webhook"     # vendor prefix
    assert resource_family("meeting-zoom") == "meeting"       # vendor suffix
    assert resource_family("customerio-integration") == "integration"
    # vendor variants of one surface land in ONE family:
    assert resource_family("zoom-meeting") == resource_family("google-meeting")


def test_all_vendor_resource_keeps_itself() -> None:
    # A resource that is ONLY vendor tokens must not fold to "".
    assert resource_family("supabase") == "supabase"
    assert resource_family("stripe") == "stripe"


# ── pre-fold candidates ─────────────────────────────────────────────────────


def _family_leaves() -> list[UserFlow]:
    return [
        _uf("UF-001", "Create a link", resource="link", intent="author"),
        _uf("UF-002", "Browse link tags", resource="link-tags", intent="browse"),
        _uf("UF-003", "View link analytics", resource="links-analytics",
            intent="browse"),
        _uf("UF-004", "Export link analytics", resource="links-analytics",
            intent="export"),
        _uf("UF-005", "Connect Zoom meetings", resource="meeting-zoom",
            intent="execute"),
        _uf("UF-006", "Connect Google meetings", resource="google-meeting",
            intent="execute"),
        _uf("UF-007", "Sign a document", resource="", intent="execute"),
    ]


def test_prefold_groups_family_and_intent_class() -> None:
    cands = _prefold_candidates(_family_leaves())
    by_key = {(c["family"], c["intent_family"]):
              [u.id for u in c["leaves"]] for c in cands}
    # author/browse fold into the manage intent class → one link candidate…
    assert sorted(by_key[("link", "manage")]) == ["UF-001", "UF-002", "UF-003"]
    # …export stays its own intent class (judge-refuted cross-action merges).
    assert by_key[("link", "export")] == ["UF-004"]
    # vendor variants of one surface = one candidate.
    assert sorted(by_key[("meeting", "execute")]) == ["UF-005", "UF-006"]
    # no-resource leaf stays a singleton candidate.
    assert by_key[("", "execute")] == ["UF-007"]
    # surjectivity of the pre-fold: every leaf in exactly one candidate.
    all_ids = sorted(u.id for c in cands for u in c["leaves"])
    assert all_ids == [f"UF-{i:03d}" for i in range(1, 8)]


def test_prefold_is_input_order_invariant() -> None:
    leaves = _family_leaves()
    a = _candidate_digest(_prefold_candidates(leaves))
    b = _candidate_digest(_prefold_candidates(list(reversed(leaves))))
    assert a == b


def test_family_fold_is_hash_seed_invariant() -> None:
    """PYTHONHASHSEED-varied subprocess runs → identical candidate digest."""
    script = (
        "import json;"
        "from faultline.models.types import UserFlow;"
        "from faultline.pipeline_v2.stage_6_7e_uf_lattice import ("
        "_prefold_candidates,_candidate_digest);"
        "ufs=[UserFlow(id=f'UF-{i:03d}',name=f'n{i} thing',"
        "intent=['author','browse','execute','export'][i%4],"
        "resource=['link','link-tags','links-analytics','stripe-webhook',"
        "'meeting-zoom','api-cron-job',''][i%7],"
        "member_flow_ids=[f'f{i}'],member_count=1,routes=[f'/r{i}'])"
        " for i in range(29)];"
        "print(json.dumps(_candidate_digest(_prefold_candidates(ufs)),"
        "sort_keys=True))"
    )
    outs = set()
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-c", script], env=env,
                           capture_output=True, text=True, check=True)
        outs.add(r.stdout.strip())
    assert len(outs) == 1


# ── prompt integration (fake client) ────────────────────────────────────────


class _FakeMsg:
    def __init__(self, text: str):
        class _B:
            pass

        b = _B()
        b.text = text

        class _U:
            input_tokens = 10
            output_tokens = 10

        self.content = [b]
        self.usage = _U()


def _fake_client(payload: str):
    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        def create(self, **kwargs):
            self._outer.calls.append(kwargs)
            return _FakeMsg(payload)

    class _Client:
        def __init__(self):
            self.calls: list[dict] = []
            self.messages = _Messages(self)

    return _Client()


def test_grouping_call_receives_pre_grouped_candidates() -> None:
    payload = json.dumps({"capabilities": [
        {"name": "Manage links", "resource": "link", "intent": "manage",
         "member_ids": ["UF-001", "UF-002", "UF-003"]},
    ]})
    client = _fake_client(payload)
    leaves = _family_leaves()[:3]
    caps, tele = build_uf_lattice_llm(leaves, client=client)
    assert tele["grouping"] == "llm"
    assert tele["prefold_candidates"] == 1
    assert tele["prefold_multi_candidates"] == 1
    (call,) = client.calls
    user_msg = call["messages"][0]["content"]
    assert "Candidate groups" in user_msg
    assert '"family": "link"' in user_msg
    assert "PRE-GROUPED" in call["system"]
    node = next(c for c in caps if c.name == "Manage links")
    assert sorted(node.member_uf_ids) == ["UF-001", "UF-002", "UF-003"]


def test_family_merged_children_survive_token_consistency() -> None:
    """The frozen parent/child token guard must NOT evict legitimate family
    variants ("link-tags" child under a "Manage links" node shares the
    family token)."""
    specs = [{"name": "Manage links", "resource": "link", "intent": "manage",
              "member_ids": ["UF-001", "UF-002", "UF-003"]}]
    caps, tele = _build_from_llm_specs(specs, _family_leaves()[:3])
    assert tele["regroup_count"] == 0
    assert caps[0].member_count == 3


def test_over_merge_guard_still_trips_on_manage_everything() -> None:
    """Guards unchanged (mission point 3): a broad multi-resource blob with
    token-inconsistent children still dissolves via eviction + orphan
    rescue — never a silent mega-parent."""
    leaves = [
        _uf("UF-001", "Create a link", resource="link", intent="author"),
        _uf("UF-002", "Browse domains", resource="domain", intent="browse"),
        _uf("UF-003", "Rotate webhook secrets", resource="webhook",
            intent="manage"),
    ]
    specs = [{"name": "Manage everything", "resource": "workspace",
              "intent": "manage",
              "member_ids": ["UF-001", "UF-002", "UF-003"]}]
    caps, tele = _build_from_llm_specs(specs, leaves)
    # No child shares a token with "Manage everything"/"workspace" → all
    # evicted to degenerate nodes; the blob node itself vanishes.
    assert tele["regroup_count"] == 3
    assert tele["orphan_rescued"] == 3
    assert all(c.member_count == 1 for c in caps)
    assert not any(c.name == "Manage everything" for c in caps)


def test_cache_version_bumped_for_famfold_digest() -> None:
    """Stale lattice-1 groupings must never be replayed into the new
    candidate-digest prompt."""
    assert LATTICE_CACHE_VERSION != "lattice-1"
    assert "famfold" in LATTICE_CACHE_VERSION
