"""Top-level scan-result cache — full-pipeline reproducibility short-circuit.

Why this exists
=``temperature=0`` on Anthropic is **not** bit-exact: the same prompt can
produce a slightly different completion run-to-run. Several LLM stages of
``pipeline_v2`` (Stage 3 flow detection, the 6.7b/6.7c user-flow stages,
and the Stage 8 product clusterer) are therefore non-deterministic across
runs even on an *unchanged* repo — a "fresh" re-scan of fastapi drifts
(48 vs 53 user-flows, 14 vs 16 product-features) although the deterministic
Layer 1 developer features are identical.

Rather than cache every LLM stage individually, this module caches the
**final FeatureMap** keyed on everything that determines it:

  * **repo content identity** — ``git rev-parse HEAD`` plus a hash of any
    dirty/uncommitted state (``git status --porcelain=v1`` + the diff of
    modified tracked files). A clean checkout at commit *X* hashes to *X*;
    a dirty tree hashes distinctly. Non-git dirs fall back to hashing the
    tree's source files.
  * **engine version** — pyproject / installed-distribution version.
  * **scan config signature** — model, days, subpath, max_tree_depth,
    llm_reconcile, feature_history, and the Stage-6.7d abstraction flags.
    Everything that changes output; NOTHING that varies per run
    (``run_id``, timestamps, output path, org/thread identity, cost).

Same ``(repo-state, engine-version, config)`` → the orchestrator replays
the **byte-identical** stored FeatureMap ($0, instant) and skips the whole
pipeline. Because the key excludes per-run values, it is a deterministic
reproducibility cache — not per-repo memory — and is ``rule-cold-scan`` safe.

Design contract
========  * **Opt-in.** ``FAULTLINE_SCAN_CACHE`` (default OFF). When off, the
    orchestrator never takes the cache path and behaviour is byte-identical
    to today.
  * **Bypass.** ``FAULTLINE_SCAN_CACHE_BYPASS=1`` forces a fresh scan (skip
    the HIT read) while STILL storing the fresh result — a cheap "refresh".
  * **Byte-exact.** We store and replay the *raw bytes* of the written
    FeatureMap file, never a re-serialised dict, so a HIT reproduces run A
    exactly (including run A's ``scan_meta`` timestamps).
  * **Robust.** Every read/write fault is swallowed (log + fall through to a
    normal scan). A corrupt / unparseable / partial entry is treated as a
    MISS. Writes are atomic (temp file + ``os.replace``) so a crashed write
    never leaves a partial entry that could be served. NEVER crashes a scan.

No LLM. No network. Pure local-disk + git.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from faultline.cache.backend import CacheKind, _safe_component
from faultline.cache.paths import faultline_base_dir

logger = logging.getLogger(__name__)

#: Opt-in gate. Empty / ``"0"`` → cache disabled (default). Any other value
#: (typically ``"1"``) → enabled.
ENV_ENABLE = "FAULTLINE_SCAN_CACHE"
#: Force a fresh scan: skip the HIT read but STILL store the result.
ENV_BYPASS = "FAULTLINE_SCAN_CACHE_BYPASS"
#: Stage-6.7d abstraction env flags — output-affecting, so part of the key.
ENV_6_7D_ABSTRACTION = "FAULTLINE_STAGE_6_7D_LLM_ABSTRACTION"
ENV_6_7D_ABSTRACTION_MODEL = "FAULTLINE_STAGE_6_7D_ABSTRACTION_MODEL"

# Every env flag that gates a pipeline stage on/off and thus materially changes
# product_features[] / user_flows[]. Any of these toggled between two scans of
# the same tree MUST miss the cache (audit Bug 2 — else a toggle-and-rescan, the
# exact eval workflow the stale-cache rule forbids, serves a stale result). We
# store the RAW env value for each (unset vs "0" vs "1" all distinct) — safe
# over-invalidation beats a stale serve.
ENV_OUTPUT_FLAGS = (
    "FAULTLINE_SEED_SYSTEM_UFS",
    "FAULTLINE_STAGE_6_3_MEMBER_BACKFILL",
    "FAULTLINE_STAGE_8_6_NONSOURCE_DROP",
    "FAULTLINE_STAGE_8_6_5_SCAFFOLD_FILTER",
    "FAULTLINE_STAGE_8_6_7_DI_ATTRIBUTION",
    "FAULTLINE_STAGE_8_7_DESINK",
    "FAULTLINE_STAGE_8_8_SHARED_MEMBERS",
    "FAULTLINE_STAGE_8_9_SUBDECOMPOSE",
    "FAULTLINE_PF_ANCHOR_NAME_GUARD",
    "FAULTLINE_STAGE_8_9_5_LLM_COMPONENT_SPLIT",
    "FAULTLINE_STAGE_6_7D_PF_UF_BACKSTOP",
    "FAULTLINE_STAGE_6_7D_RESIDUAL_GUARD",
    "FAULTLINE_STAGE_6_7D_UF_RESHARE",
    "FAULTLINE_STAGE_6_7D_SHELL_ABSORB",
    # Wave 3 — naming contract + keeper + personas (§4.7/§4.8): each
    # shapes the emitted display layer / surface lanes, so cached scan
    # results must key on them like every other output-shaping flag.
    "FAULTLINE_NAMING_CONTRACT",
    "FAULTLINE_KEEPER",
    "FAULTLINE_PERSONA_LABELER",
    "FAULTLINE_PERSONA_LABELER_MODEL",
    "FAULTLINE_PERSONA_ADJUDICATOR",
    "FAULTLINE_PERSONA_VERIFIER",
    "FAULTLINE_PERSONA_ESCALATION_MODEL",
    # Wave 5 — journey lattice (catch-all partition + subset-dup merge):
    # reshapes the emitted user_flows[] layer.
    "FAULTLINE_JOURNEY_LATTICE",
    # Wave 5.1 — lattice thin-child fold-back + LOC-worthy PF backstop:
    # both reshape the emitted user_flows[] layer.
    "FAULTLINE_LATTICE_THIN_FOLD",
    "FAULTLINE_LOC_WORTHY_BACKSTOP",
    # B4 (2026-07-08) — synthesized-journey quality: demotes member-less
    # system_flow_recall seeds out of user_flows[] and regrounds single-member
    # backstop journey names. Reshapes the emitted user_flows[] layer.
    "FAULTLINE_SYNTH_QUALITY",
    # B13 (2026-07-09) — backstop own-entry cover: the synthesize arm bundles
    # only own-entry flows (else a member-less seed) and every member-less
    # I8-cover seed carries an honest coverage-marker name + flag. Reshapes
    # the emitted user_flows[] layer.
    "FAULTLINE_BACKSTOP_OWNED_COVER",
    # B15 (2026-07-09) — shared-leaf role consistency: high-cross-PF-fan-in,
    # no-surface, already-shared member files are forced role="shared"
    # everywhere. Reshapes member_files[].role (the I23 anchor-body view).
    "FAULTLINE_SHARED_LEAF_CONSISTENCY",
    # B15b (2026-07-09) — data-file shared-leaf rail: large pure-data blobs
    # (locale packs) consumed by >=2 PFs, no surface, are forced role="shared".
    # Reshapes member_files[].role.
    "FAULTLINE_DATA_LEAF",
    # B16 (2026-07-10) — PF dev-grain suffix law: a route-dir-naming leak
    # ('policy-page' -> 'Policy Page') is stripped to the capability
    # ('Policy') at the display channel. Reshapes product_features[].display_name.
    "FAULTLINE_PF_NAME_LAW",
    # B16 Part 1b (2026-07-10) — UF-level dev-grain suffix law: strips
    # "View detections page" -> "View detections". Reshapes user_flows[].name.
    "FAULTLINE_UF_DEVGRAIN_NAME",
    # B16 Part 2 (2026-07-10) — sibling-anchor unification: co-identity sibling
    # route PFs (investigation / investigations-page / investigation-flow)
    # collapse to one. Reshapes product_features[] + developer/user-flow
    # product_feature_id links.
    "FAULTLINE_PF_SIBLING_UNIFY",
    # B19 (2026-07-10) — transport-package lane: a ws-package named after its
    # own external dependency family (packages/trpc -> @trpc/*) with no product
    # surface lanes as a technology_instrument. Reshapes product_features[] +
    # the platform_infrastructure lane. (Sibling tech-instrument flags were an
    # unkeyed cache-correctness gap — registering this one per the B4 precedent.)
    "FAULTLINE_TECH_TRANSPORT_LANE",
    # B20 (2026-07-10) — path_index-aware I16 journey re-home: a majority-foreign
    # UF re-homes to its strict-majority entry-owner PF. Reshapes
    # user_flows[].product_feature_id.
    "FAULTLINE_I16_REHOME_B20",
    # B22a (2026-07-10) — cross-app fold guard: the mint's ancestor-walk rung
    # may not annex a dev across a workspace-unit boundary (documenso trpc
    # annexation). Reshapes developer_features[].product_feature_id +
    # path_index ownership + the platform_infrastructure lane.
    "FAULTLINE_FOLD_CROSSAPP_GUARD",
    # B58 (2026-07-13) — container-anchor annexation guard: Seg A fences the
    # mint's entry/span/walk rescue rungs by the target's CANONICAL unit (a
    # unit-coherent flowful dev never force-binds across a workspace unit —
    # plane Issue i18n 125K, cal.com Bookings/apps/api/v2); Seg B bars
    # dev-artifact-UNIT anchors from minting (novu playground Notifications).
    # Reshapes product_features[] + developer_features[].product_feature_id +
    # the platform_infrastructure lane. Appended WITHOUT a KEY_SCHEMA bump —
    # reconciled at merge.
    "FAULTLINE_ANNEXATION_GUARD",
    # B58-v2 (2026-07-14) — same-unit domain-dir cap: Seg A extends the
    # B53 drain with donor class 2 (a container-anchor PF sheds member
    # files annexed from SAME-unit foreign domain-dirs whose name
    # echo-matches exactly ONE existing PF; nav-only matches are
    # telemetry, never a move); Seg B adds the page-surface
    # canonical-unit rung to the B58 fence (a multi-unit route anchor
    # resolves to its PAGE unit — plane route:space). Reshapes
    # developer_features[] (carve devs + donor path surgery) +
    # product_features[] member ownership + path_index + downstream
    # user_flows[] homes (I16). Appended WITHOUT a KEY_SCHEMA bump —
    # reconciled at merge.
    "FAULTLINE_SAMEUNIT_DOMAIN_CAP",
    # B22 (2026-07-10) — transport-lane journey-conservation handoff (Stage
    # 6.985): the transport prong marks candidates at 6.86 and the handoff
    # re-homes their journeys/devs post-journey-layer before laning the PF
    # (all-or-nothing conservation gate). Reshapes product_features[] +
    # user_flows[].product_feature_id + the platform lane when candidates
    # exist. Sub-flag of the plurality rung keys alongside it.
    "FAULTLINE_TRANSPORT_LANE_HANDOFF",
    "FAULTLINE_TRANSPORT_HANDOFF_PLURALITY",
    # B23 (2026-07-10) — marker surface coordinates: member-less coverage
    # markers carry their uncovered trigger surface as whole-file spans
    # (user_flows[].surface_files, honest loc>0 via 6.97b) and Track-C e2e
    # markers keep their maintainer-authored labels instead of the
    # 'Uncovered: <PF> routes' rename. Reshapes the emitted user_flows[]
    # layer. Sub-flag of FAULTLINE_BACKSTOP_OWNED_COVER (lock-step: spans
    # exist only where the B13 marker flag exists).
    "FAULTLINE_MARKER_SURFACE_COORDS",
    # B24 (2026-07-10) — Stage 6.986 mega-PF nav-area re-home + floor-gated
    # mint: a board-dominating umbrella PF's journeys re-home onto their
    # nav-area sibling PFs (attach-floor / all-rung-I16-rail gated) and an
    # above-floor area with no sibling mints its own PF (supabase
    # 'projects' -> 'database'). Reshapes user_flows[].product_feature_id
    # + product_features[] + developer_features[] carve chunks. Default
    # OFF.
    "FAULTLINE_MEGA_PF_NAV_REHOME",
    # B26 (2026-07-10) — hub plumbing child: the dir-per-vendor child
    # filter tests the segment against the plumbing/stop vocabularies
    # NORMALIZED (underscore-stripped, singularized; vendor-beats-
    # plumbing guard), and the 6.86 mint bar backstops with
    # ``hub_plumbing_child`` — a shared-helper dir inside a connector hub
    # (cal.com ``app-store/_utils``) never mints a PF; its devs fold to
    # the enclosing package / hub core. Reshapes product_features[] +
    # developer_features[].product_feature_id on affected repos.
    "FAULTLINE_HUB_PLUMBING_CHILD",
    # B27 (2026-07-10) — package-manifest PF display names: a package-dir-
    # anchored PF (hub:-vendor / ws:) takes its display from the package's
    # OWN declared metadata (config.json "name" / metadata-module name /
    # package.json displayName / authored name), with a mechanical
    # letter/digit word-split of the dir slug as the rung below. Reshapes
    # product_features[].display_name (+ the UF display templates derived
    # from it).
    "FAULTLINE_PF_MANIFEST_NAME",
    # B30 (2026-07-10) — deterministic verb+resource flow naming: route-slug
    # path-echo flow names (``api-account-passkeys-flow``) are renamed to
    # verb+resource (``manage-account-passkeys-flow``) at the very end of the
    # finalize phase. Reshapes flows[].name/display_name/short_label (the
    # operator-visible name channel). Default ON.
    "FAULTLINE_FLOW_NAME_V2",
    # B31 (2026-07-10) — distinct recall-row display names: every synthesized
    # recall row (e2e / route-group / backstop) in a display-name collision
    # group is re-derived from its own (authored label | intent+resource |
    # route-terminal) evidence at Stage 6.98 — per-board uniqueness by
    # construction. Reshapes user_flows[].name.
    "FAULTLINE_RECALL_ROW_NAMES",
    # B34 (2026-07-10) — lazy-import edges (Tier 1, artifact side-channel)
    # + dispatch-registry system-flow seeds (Tier 2, appends flows[] rows
    # for uncovered string-dispatched connectors). Tier 2 reshapes
    # flows[] / developer_features[].flows[] and, transitively, the UF
    # recall layer (flowless-PF markers dissolve). Both default OFF.
    "FAULTLINE_LAZY_IMPORT_EDGES",
    "FAULTLINE_DISPATCH_REGISTRY_FLOWS",
    # B28 (2026-07-10) — non-product app scope (Shape E lane + S1g types-only
    # prong; journeys ride to non_product_surfaces). Default ON.
    "FAULTLINE_NONPRODUCT_SCOPE",
    # B28 (2026-07-10) — Shape D docs re-anchor (majority-dir election).
    # Reshapes product_features[].anchor_id only. Default ON.
    "FAULTLINE_DOCS_REANCHOR",
    # B25 (2026-07-10) — journey-lattice verifier-revert slot release: a pf
    # whose split plan the Draft Verifier fully reverted re-runs the
    # Phase-2b action detection once (one extra verifier batch, hard-capped
    # at one release per pf per scan). Reshapes the emitted user_flows[]
    # layer on keyed scans (keyless is unreachable — no verifier, no
    # reverts).
    "FAULTLINE_JOURNEY_LATTICE_B25",
    # B38 (2026-07-11) — marker coordinate integrity: member-less coverage
    # markers with zero attached surface spans are suppressed from
    # user_flows[] (gap claims with no evidence; wave15 breach cal.com 20 /
    # midday 1 / typebot 1). Default OFF. Appended WITHOUT a KEY_SCHEMA
    # bump per wave convention — reconciled at merge.
    "FAULTLINE_MARKER_COORDS_REQUIRED",
    # B41 (2026-07-11) — pages-surface named-export fallback: react-router
    # trees under src/pages with NAMED-export components get real
    # (symbol, line) anchors instead of symbol-less hollow seeds (novu:
    # 54% of flows hollow). Default ON; inert on Next trees (default
    # exports always match first). Appended WITHOUT a KEY_SCHEMA bump —
    # reconciled at merge.
    "FAULTLINE_PAGES_NAMED_EXPORT_FALLBACK",
    # B43 (2026-07-11) — routed-seed anchor completeness: HOC default
    # exports unwrap to the page component; range-less symbols anchor at
    # the extractor 'default' range, else line 1 — hollow page seeds
    # (supabase keyless 91) get real anchors. Default ON. Appended
    # WITHOUT a KEY_SCHEMA bump — reconciled at merge.
    "FAULTLINE_PAGES_ANCHOR_FALLBACK",
    # B33 v2 (2026-07-11) — post-UF devgrain-leaf demote (Stage 6.987): a
    # route:/fdir:-anchored PF whose leaf normalizes to a plumbing/
    # journey-step token (welcome, getting-started, access-denied,
    # redirect-*, *-callback, *-onboarding), is NOT nav-declared, and whose
    # final journey profile is micro (<=2 UFs, member_count <=3) demotes —
    # PF row removed, micro-UFs dropped, devs re-pointed to the nearest
    # surviving ancestor. Rich journey sets veto (conservation); board-wide
    # abstain when the nav parse is unreadable. Reshapes product_features[]
    # + user_flows[] + developer_features[].product_feature_id on affected
    # repos. Default ON (flipped 2026-07-12 after the keyed proof on
    # papermark — Welcome demoted, I9=0 via the B37-ph2 homing rider,
    # platform lane 43->42; KEY_SCHEMA v28, coupled with
    # FAULTLINE_DISPATCH_HOMING_B37P2 — without the rider the demote
    # re-creates I9). =0 restores the pre-B33 board byte-identically.
    "FAULTLINE_FDIR_DEVGRAIN_GATE",
    # B45 (2026-07-11) — coverage_gaps[] gap channel: member-less I8-cover
    # markers leave user_flows[] for a dedicated top-level ``coverage_gaps``
    # array. Default FULL (flipped 2026-07-12 after the keyed proof on
    # papermark + cal.com; KEY_SCHEMA v27): unset = gaps emitted and the
    # marker rows REMOVED from user_flows[]; dual = gaps emitted AND the
    # marker rows stay (bijection instrument); explicit off ("off"/"0"/
    # "false") restores the pre-B45 byte-identical output (key absent).
    # Reshapes user_flows[] (full) + adds the coverage_gaps[] key (dual/full).
    "FAULTLINE_COVERAGE_GAP_CHANNEL",
    # B40 (2026-07-11) — provenance-graded name_confidence + name_evidence[]
    # audit trail. Arms the nav / registry / structural-route rungs in Law C
    # and the singular-folded multi-member agreement in synth_quality; stamps
    # UserFlow.name_evidence. Default ON (flipped 2026-07-12 after the keyed
    # proof on papermark + cal.com; KEY_SCHEMA v27): may raise name_confidence
    # and adds name_evidence[] — UF NAMES stay byte-stable either way. =0
    # restores the pre-B40 rubric + serialized output byte-identically
    # (name_evidence key absent).
    "FAULTLINE_NAME_EVIDENCE_RUNGS",
    # B46 (2026-07-11) — UF-name hygiene: kills the garbage-name sources —
    # the doubled route/file-stem token concat ('settings accounts settings
    # accounts', root in flow_name_v2._resource_tokens), the GLUED plain-slug
    # seed echo on the UF-label side, the bare pluralized dir-stem leaf on an
    # ungrounded 'other'-intent slot ('onboardings' -> 'Manage onboarding'),
    # and an inherited Stage-5.5 ordinal on a UF label ('… action 3').
    # Default ON (flipped 2026-07-12 after the keyed proof on papermark +
    # cal.com; KEY_SCHEMA v27): reshapes flow.name (concat root) +
    # user_flows[].name (garbage rows only). =0 restores the pre-B46 flow/UF
    # names byte-identically.
    "FAULTLINE_UF_NAME_HYGIENE",
    # B37-ph2 (2026-07-12) — dispatch-mint homing: a predominantly-dispatch
    # user flow re-homes to the PF that OWNS the mint's target file
    # (path_index dev→PF first — the i16 ruler — anchor-chain fallback for
    # unowned targets; never the dev-of-first-attribution), AFTER the final
    # path_index refresh + flowless-PF backstops and BEFORE the
    # synth_quality gap arbitration; the same target-owner machinery homes
    # a demoted PF's FLOWFUL devs in the Stage 6.987 devgrain I9 rider
    # (flowful → owner, never the platform lane). Reshapes
    # user_flows[].product_feature_id (+ developer_features[].
    # product_feature_id under the devgrain gate). Default ON (flipped
    # 2026-07-12, coupled with FAULTLINE_FDIR_DEVGRAIN_GATE; keyed papermark
    # proof I9=0 + keyless byte-identical no-op supabase/midday; KEY_SCHEMA
    # v28). =0 restores the pre-B37-ph2 homes byte-identically.
    "FAULTLINE_DISPATCH_HOMING_B37P2",
    # B47 Arm B (2026-07-12) — keyless journey recall: attaches route-matched
    # LIVE flows as members to a groundable e2e orphan journey, graduating it
    # from a member-less coverage_gaps[] marker to a real member-ful journey
    # (name = authored_label; name_confidence low→medium on route grounding).
    # Honest-gap law: an orphan with no covering flow stays member-less.
    # Default OFF; when ON reshapes user_flows[] (member_flow_ids/member_count
    # /name_confidence on e2e_journey_recall rows) + removes those rows from
    # coverage_gaps[]. =0/unset restores byte-identical output. No KEY_SCHEMA
    # bump (flip is a separate later commit per flip-protocol).
    "FAULTLINE_KEYLESS_JOURNEY_RECALL",
    # B44 (2026-07-12) — Stage-3 keyless surface blind spot, part 1: a
    # React-Router-framework / Remix workspace (``react-router.config.*`` +
    # ``app/routes/**``, the Remix successor stack) is route-EXTRACTED
    # (routes_index populated) but wins the DefaultProfile, which emits no
    # ``flow_entries`` — so its live routes never seed keyless flows
    # (documenso apps/remix: 121 routes, 0 flows). Registers a deterministic
    # ``react-router-fw`` profile that seeds one flow per ``app/routes/**``
    # entry (default-export symbol → real (file,line) anchor, B41/B43 chain).
    # Default OFF; when ON a react-router-framework unit re-homes from
    # ``default`` to ``react-router-fw`` (composite name gains the arm) and
    # its route files seed flows (feeds the B47 arm-B e2e-orphan bridge).
    # =0/unset leaves the profile unregistered → byte-identical. No
    # KEY_SCHEMA bump (flip is a separate later commit per flip-protocol).
    "FAULTLINE_REACT_ROUTER_FW_PROFILE",
    # B44 (2026-07-12) — Stage-3 keyless surface blind spot, part 2: the
    # react-router-SPA extractor resolves ``@/`` / ``~/`` import aliases only
    # against a hard-coded ``src/`` root, so a Vite SPA that maps ``~/`` →
    # ``app/`` (tsconfig ``paths`` / vite ``resolve.alias``) resolves NO
    # mounted page components → 0 route buckets → empty board (outline:
    # ``~/*`` → ``./app/*``, react-router in ``app/routes/*``, 0 PF/UF/flows).
    # When ON the SPA index reads the workspace's real alias map from
    # ``tsconfig.json`` ``compilerOptions.paths`` and resolves aliases against
    # their true roots, AND the SPA extractor stamps ``AnchorCandidate.routes``
    # so its branches populate ``routes_index``. =0/unset restores the
    # ``src/``-only resolution + route-less anchors byte-identically. No
    # KEY_SCHEMA bump.
    "FAULTLINE_ROUTER_ALIAS_RESOLVE",
    # B48 (2026-07-12) — ws-library / name-dep transport lane: a
    # broadly-imported zero-surface (no route/page, not nav-confirmed)
    # ws-package that imports <=1 in-repo unit (S2 library) OR is named
    # after its own external dependency family (S1 name-dep transport)
    # lanes as a technology instrument — the corroboration-free extension
    # of the S2 prong (compound/generic names: twenty-ui, novu dal/
    # framework, documenso/cal.com trpc). Rides the B19/B22
    # transport-handoff channel for journey conservation (never mint-time
    # laning). Reshapes product_features[] (laned rows leave) +
    # user_flows[].product_feature_id (re-homed journeys) + the platform
    # lane when candidates exist. Default OFF; =0/unset byte-identical.
    # No KEY_SCHEMA bump (flip is a separate later commit per flip-protocol).
    "FAULTLINE_WS_LIBRARY_LANE",
    # B50 Seg1-2 (2026-07-12) — UF/PF display de-grime: kills adjacent-token
    # echoes ('Ingest ingest', 'case case ids', 'chat chatids') and glyph-less
    # route-param leaks ('teamurl documents', PF 'URL') at the display JOINER.
    # DISPLAY-ONLY (uf.name / pf.display_name); identity / membership /
    # product_feature_id / paths / cluster keys / the resource field / lineage
    # untouched. Default OFF; =0/unset ⇒ serialized output byte-identical. No
    # KEY_SCHEMA bump (flip is a separate later commit per flip-protocol).
    "FAULTLINE_UF_NAME_DEGRIME",
    # B50 Seg3 (2026-07-12) — earned resource rung: a low UF carrying
    # missing:resource (verb grounded) earns resource-grounding ONLY from a
    # real evidence rung (member-file domain noun / param-free route segment /
    # mapped test-file noun), each stamping a distinct name_evidence entry.
    # Adds OR-sources to Law C's res_grounded (bar UNCHANGED, no lowered
    # threshold, never invents missing:verb grounding). CONFIDENCE channel
    # only. Default OFF; =0/unset ⇒ confidence + serialized output
    # byte-identical. No KEY_SCHEMA bump (flip is a separate later commit).
    "FAULTLINE_UF_RESOURCE_RUNG",
    # B49 (2026-07-12) — transport namespace-echo (r2.6 rung): an in-lane
    # tRPC router seed that abstains at r2 (typed-proxy consumption → no
    # product consumers → zero_product_votes) votes its span mass for the
    # EXISTING product PF whose anchor-identity its namespace token echoes
    # (``normalize_anchor_key`` — the same normalization as the S3-nav
    # echo: ``apiKeys`` → ``api-key`` → the ``API Keys`` PF). Unique match
    # only; ambiguous / generic (``viewer``/``utils``) abstains; NEVER
    # mints (re-homes onto existing PFs only, conservation judge
    # untouched). Reshapes user_flows[].product_feature_id + which
    # transport candidates clear the all-or-nothing gate to lane. Default
    # OFF; =0/unset byte-identical. No KEY_SCHEMA bump (flip is a separate
    # later commit per flip-protocol).
    "FAULTLINE_TRANSPORT_NAMESPACE_ECHO",
    # B51 (2026-07-13) — transport router-mega decomposition: a FLOW-BEARING
    # transport candidate (a dev whose flows I9 forbids laning — cal.com
    # `trpc`: 66 flows) is decomposed per tRPC sub-router. Each sub-router
    # group whose namespace token echoes an EXISTING product PF (the SAME
    # `NamespaceEcho` matcher as r2.6: eventTypes→`event-types`,
    # apiKeys→`api-keys`, webhook→`webhooks`) has its flows + routers-tree
    # files carved into a product-owned chunk re-homed to that PF (I22
    # marker), BEFORE the conservation gate; the carved files are LIFTED out
    # of the lane so the existing r1 ladder drains their journeys (no new
    # rung). Residue (unmatched sub-routers + non-routers `[trpc].ts`
    # handler / middleware flows) stays flowful and keeps a REDUCED tile —
    # an honest abstain, never forced onto a product surface. Conserves
    # flows/journeys (moved, never dropped or minted). Reshapes
    # developer_features[] (carved chunks) + product_features[] (a fully
    # drained candidate lanes) + user_flows[].product_feature_id (drained
    # journeys). Default OFF; =0/unset byte-identical. No KEY_SCHEMA bump
    # (flip is a separate later commit per flip-protocol).
    "FAULTLINE_TRANSPORT_ROUTER_DECOMP",
    # B52 (2026-07-13) — flow-bearing transport lane (Option A; the operator
    # 'трпц A' mandate: the trpc tile disappears ENTIRELY). The ONE cycle
    # switch (it also drives the B51 decomposition pass, in drain-then-lane
    # mode + the (c) `api/trpc/<domain>/` handler grain): a ws-anchored
    # transport candidate ALWAYS leaves product_features[]. Matched groups
    # re-home onto EXISTING PFs WITH their journeys (r1 over the post-drain
    # state; a receiver that would end journey-less pulls its carve back —
    # the B51 I8 exhibit's structural fix); the flowful RESIDUE lanes (the
    # validator I9 ws:-anchor exemption, engine-aligned); transport-
    # intrinsic journeys stay in user_flows[] with product_feature_id=None
    # + lane_ref=<lane-row uuid> + surface_scope='platform_infrastructure',
    # and the lane row carries flow_ids[] + journeys[] (additive, non-empty
    # only). Conservation: Σflows == product-homed + lane flow_ids, ΣUF ==
    # product-homed + lane_ref rows — nothing dropped, nothing minted.
    # Reshapes product_features[] / developer_features[] / user_flows[] /
    # platform_infrastructure[]. Default OFF; =0/unset byte-identical. No
    # KEY_SCHEMA bump (flip is a separate later commit per flip-protocol).
    "FAULTLINE_FLOWFUL_TRANSPORT_LANE",
    # B53 (2026-07-13) — ws-app blob domain drain. Seg A re-attributes a
    # ws-blob donor's internal domain-dir members (``<pkg>/<container>/
    # <domain>/**``) onto the EXISTING PF whose identity the domain name
    # echoes (same NamespaceEcho matcher; NO mints), moving them at the dev
    # level so Stage 6.97 owned-LOC + the path_index rebuild + Stage 6.99
    # I16 journey re-home follow for free. Seg B lanes dev-artifact ws-
    # packages (docs-content / devDependency-only tooling / scaffolder) off
    # the product layer via the dev_artifact_units channel. ONE flag gates
    # BOTH segments. Reshapes product_features[] membership/LOC +
    # user_flows[].product_feature_id. Default OFF; =0/unset byte-identical.
    # No KEY_SCHEMA bump (flip is a separate later commit per flip-protocol).
    "FAULTLINE_WS_BLOB_DOMAIN_DRAIN",
    # B56 (2026-07-13) — full-name display law for abbreviations: a shape-
    # flagged abbreviation display tile ('Pbac', 'Sso', 'Ooo', 'I18n', 'Wp')
    # takes its repo-grounded full form ('Single Sign-On (SSO)', 'Out of
    # Office (OOO)') from an ALLOWED source (code identifiers, i18n KEY names,
    # JSX labels, package manifest, route segments — NEVER locale values, NEVER
    # comments/README); shape-flagged-but-no-evidence keeps its display and is
    # honest debt (missing:expansion, measured not invented). UF names inherit
    # the same expansion. DISPLAY CHANNEL ONLY (product_features[].display_name
    # + user_flows[].name); no identity field moves. Default OFF; =0/unset is
    # byte-identical. No KEY_SCHEMA bump (flip is a separate later commit per
    # flip-protocol).
    "FAULTLINE_PF_FULLNAME_LAW",
    # B57 Seg1 (2026-07-13) — Law C rung-source expansion: four additional
    # deterministic evidence sources for the existing resource/verb rungs —
    # (a) nav-cluster (ALL authored nav labels voted onto the owning PF),
    # (b) i18n KEYS referenced in member source files (keys only; translated
    # VALUES are a forbidden source — operator rule 2026-07-13), (c) member
    # routes' declared HTTP method → verb family, (d) assertion labels inside
    # MAPPED member test files. Same Law C bar — extra OR-sources, each with
    # a provenance tag in name_evidence. Reshapes user_flows[].
    # name_confidence / name_evidence only (UF NAMES byte-stable — B40 law).
    # Default OFF; =0/unset byte-identical. No KEY_SCHEMA bump (flip is a
    # separate later commit per flip-protocol).
    "FAULTLINE_UF_RUNG_SOURCES_V2",
    # B57 Seg2 (2026-07-13) — Stage 6.7e journey-evidence adjudicator
    # (keyed-only; Sonnet batch): selects non-high UFs + same-PF dup
    # candidates after Law C v1, collects a deterministic evidence package
    # (member files+spans, routes, nav-cluster labels, i18n KEYS — never
    # translated VALUES, neighbors), and applies ONLY deterministically
    # VERIFIED verdicts — rung_evidence (Law C re-score via
    # rescore_uf_confidence; adjudicated:* tags), rename (cited
    # identifier-shaped strings only; B50 degrime + collision-safe chain),
    # merge (identical / strict-subset member sets on the SAME non-None PF;
    # union + lineage), demote (row → typed coverage_gaps[]
    # kind="adjudicated_noise"; never a silent drop). Fake / foreign-file /
    # locale-VALUE citations are rejected. Keyless (no client) ⇒ hard no-op
    # byte-identity. Reshapes user_flows[] (confidence/evidence/names/
    # membership) + coverage_gaps[] on keyed scans. The model env keys
    # alongside (raw value) per the persona-model precedent. Default OFF;
    # =0/unset byte-identical. No KEY_SCHEMA bump (flip is a separate later
    # commit per flip-protocol).
    "FAULTLINE_STAGE_6_7E_ADJUDICATOR",
    "FAULTLINE_STAGE_6_7E_MODEL",
    # B61 Seg1 (2026-07-13) — evidence-born verb-snap: a deterministic
    # post-pass that REPLACES a UF display's leading verb when its
    # action-family is absent from the member verb-composition (B57
    # member_verb_composition — HTTP-methods / page-kinds), snapping it to
    # the canonical verb of the composition's dominant family (mutation
    # outranks read). The FIRST flag permitted to change a UF NAME; carries
    # its own kill-switch so the B40 UF-NAMES byte-stable law under the
    # rung/adjudicator flags is preserved. Reshapes user_flows[].name (and,
    # via Law C's structural:verb-composition rung, name_confidence/
    # name_evidence). Default OFF; =0/unset byte-identical. No KEY_SCHEMA
    # bump (flip is a separate later commit per flip-protocol).
    "FAULTLINE_UF_VERB_SNAP",
    # B59 (2026-07-13) — artifact-ink accounting drain. Stage 6.97 reclassifies
    # a feature's OWNED non-authorial "ink" LOC (locale catalogs / generated
    # schemas / test data / dev seeders) OUT of product ``loc`` into a separate
    # ``artifact_ink_loc`` field + a ``scan_meta.artifact_ink`` lane aggregate,
    # so ``loc`` reads as an HONEST product-code size. ACCOUNTING ONLY —
    # membership / path_index / line coordinates / flows / user_flows are
    # untouched (journeys I15/I16 provably unchanged). Default OFF; =0/unset is
    # byte-identical. Appended WITHOUT a KEY_SCHEMA bump — the bump rides the
    # separate later flip commit only (flip-protocol).
    "FAULTLINE_ARTIFACT_INK_LANE",
    # B64 — dynamic-dispatch resolver. Additively resolves (a) lazy dynamic
    # imports (`const X = lazy(() => import("./Y"))`) into the Stage 6.3
    # import-tree traversal so lazily-loaded route sub-trees (outline's
    # `lazy(() => import("./authenticated"))` — ~87% of the product) become
    # reachable; (b) one-level const-folds pure literal-returning route
    # helpers/consts (`draftsPath()` → "/drafts", `{path: ROUTES.home}`) so
    # react-router SPA routes whose path is a helper call/const resolve into
    # routes_index; (c) object/Map registries ({key: Component}) in a
    # route-binding file → one route/import edge per Component. Free vars /
    # conditionals / non-literal returns → honest skip (B63 metric measures
    # the residual). Default OFF; =0/unset byte-identical. No KEY_SCHEMA
    # bump (own later flip per flip-protocol).
    "FAULTLINE_DISPATCH_RESOLVER",
    # B67 — background-job / cron entry extractor. Emits routes_index entries
    # (synthetic JOB/CRON method) for background handlers (@Processor/BullMQ
    # Worker/node-cron TS/JS, celery/APScheduler/rq Python, vercel/actions/k8s
    # manifest-cron) so their flows/journeys mint. Default OFF; =0/unset is
    # byte-identical. Appended WITHOUT a KEY_SCHEMA bump — the bump rides the
    # separate later flip commit only (flip-protocol).
    "FAULTLINE_JOBS_ENTRIES",
    # B68 — terminal 4-way classification of the coverage-gap band (Stage
    # 6.995, operator doctrine 2026-07-14: the gap channel is an internal
    # state, never a final board category). Each gap row decomposes BY
    # MEMBERS into (1) not-a-feature e2e/test labels (audit trace, off the
    # board), (2) part-of-existing-PF (NamespaceEcho / live-flow owner —
    # claim dissolves), (3) own-PF worthiness evaluation (records only —
    # member-less mints stay forbidden, B23/B33), (4) dev-infrastructure
    # (existing predicates: dev_artifact_units / instruments / test /
    # generated / artifact-ink / lane / shared-leaf); the ONLY legal
    # residue is (5) a row stamped ``why_unresolved`` naming a known lexer
    # hole (data/terminal-classification.yaml). Reshapes coverage_gaps[]
    # (rows removed / trimmed / stamped) + scan_meta. Default ON since the
    # 2026-07-16 horizon-1 flip (KEY_SCHEMA 30; keyed proof documenso + plane
    # green, B68 — gap rows typed, zero unmapped-silence). ``=0``/false/off
    # restores the pre-B68 byte-identical board (kill-switch).
    "FAULTLINE_TERMINAL_CLASSIFICATION",
    # B69-v2 (2026-07-15) — PF-homing hygiene family, FINAL composition
    # (re-convoy ruling): Stage 6.99b post-UF rehome rail (anchor-breadth
    # ruler; rename-on-rehome for synthesized rows; fold-into-existing;
    # home-tie guards) + B31 pf-display echo-guard + 6.7e Law-A telemetry
    # preservation. Reshapes user_flows[] homes/names + scan_meta
    # telemetry. Default ON since the 2026-07-16 horizon-1 flip (KEY_SCHEMA
    # 30; keyed proof papermark + cal green, B69-v2 — pm churn=1 fold, cal
    # no-op). ``=0`` restores the pre-B69-v2 byte-identical board
    # (kill-switch). Read in TWO modules (naming_contract + stage_6_86); both
    # defaults flip in lock-step.
    "FAULTLINE_HOMING_HYGIENE",
    # B69-v2 SPLIT ruling (2026-07-15) — seed-birth hygiene pair, banked
    # for its OWN cycle: same-(pf,resource) route-group seed coalescence +
    # method-derived seed intent (route_group_recall). Board-wide blast
    # radius at seeding (the keyed A/B showed the pair driving churn on
    # both repos while the 6.99b rail was exactly-one-action) — hence its
    # own flag, own seed-grain gates, own keyed A/B. Reshapes user_flows[]
    # (seed rows / names / intents). Default OFF; =0/unset byte-identical.
    # Appended WITHOUT a KEY_SCHEMA bump — flip-protocol.
    "FAULTLINE_SEED_HYGIENE",
    # B69-v2 THIRD split (2026-07-15) — the bare-verb/dev-grain-token
    # display law, BANKED for the B70 member-evidence redesign: the
    # vocabulary-driven implementation false-positives on verb-homonym
    # resources ('Manage download', 'Browse webhook') and misses
    # evidence-less tokens ('View mupdf'); each ban cascades into retries/
    # collisions/B31 parentheticals (the keyed pair's entire off-rail
    # churn). B70 member-evidence redesign landed. Reshapes UF display names
    # when armed. Default ON since the 2026-07-16 horizon-1 flip (KEY_SCHEMA
    # 30; keyed proof papermark + cal green, B70 — law-attributed churn, zero
    # bare names). ``=0`` restores the pre-B70 byte-identical law list
    # (kill-switch).
    "FAULTLINE_NAMING_LAW",
    # B66 — code-first server API-entry extractor. Emits routes_index entries
    # (real HTTP methods for NestJS/koa/hono; synthetic QUERY/MUTATION/
    # SUBSCRIPTION for GraphQL/tRPC) for decorator-/DSL-routed backends whose
    # URL lives in code, not the filesystem (NestJS controllers, GraphQL
    # code-first resolvers, tRPC procedures, koa/hono routers) so their
    # flows/journeys mint. Default ON since the 2026-07-16 horizon-1 flip
    # (KEY_SCHEMA 30; keyed proof twenty + hoppscotch (meter) + cal (trpc)
    # green, B66 — nestjs/graphql/trpc/koa meter ~0). ``=0``/false/off keeps
    # the extractor inert AND unregistered, byte-identical to pre-B66
    # (kill-switch).
    "FAULTLINE_SERVER_API_ENTRIES",
    # B70 (2026-07-15) — capitalize the B31 route-terminal parenthetical
    # qualifier ('Manage links (general)' -> '(General)') so it matches the
    # proper-cased PF-display qualifier on the same recall row. The B31
    # recall-row naming (FAULTLINE_RECALL_ROW_NAMES) is itself default ON, so
    # this display-casing fix is gated separately. Default ON since the
    # 2026-07-16 horizon-1 flip (KEY_SCHEMA 30; keyed proof documenso + rallly
    # green, B70 — '(term)'->'(Term)' only). ``=0`` keeps the lowercase
    # qualifier byte-identical (kill-switch).
    "FAULTLINE_RECALL_QUAL_CASING",
    # B58-v3 (2026-07-15) — grain wave, ONE flag gates both segments (the
    # B53 precedent): Seg C Stage 6.9c schema-monolith member strip (a
    # whole-DB schema file + its package's plumbing leave every
    # FOREIGN-anchored claimant's ledgers — documenso team.verify 84%
    # prisma) + Seg A fdir internal-lib candidacy in the B48 ws-library
    # lane (broadly-imported zero-surface feature-dir modules —
    # twenty-front src/modules/apollo, cal.com apps/web/modules/data-table
    # — ride the SAME 6.985 transport-handoff conservation channel; the
    # handoff resolves fdir: anchors for grain-wave candidates). Reshapes
    # developer_features[] ledgers + product_features[] membership/LOC +
    # the platform lane + user_flows[].product_feature_id when candidates
    # exist. Default ON since the 2026-07-16 horizon-1 flip (KEY_SCHEMA 30;
    # keyed proof documenso green, B58-v3 — team.verify ~200 LOC, fdir-lane).
    # ``=0`` restores the pre-B58-v3 byte-identical board (kill-switch).
    "FAULTLINE_GRAIN_WAVE",
    # B66-v2 (2026-07-16) — ownership/LOC-truth + dispatch tails, ONE flag
    # gates four segments (the B58-v3 precedent). Seg A: an entry-mint
    # (server-api-entry / jobs-entry source) exclusively owns its module
    # subtree at the loc-attribution pass — a route-anchor dev whose members
    # collapsed to loc=0 under primary-owner fan-in (hoppscotch 27 resolvers
    # -> one 'team' uuid, 0 LOC, mis-parented) recovers its owned LOC without
    # touching membership or journeys (attribution layer). Seg B: a static
    # asset/data member (json/svg/lottie) reaching a dev feature only through
    # import fan-in neither credits membership nor inflates the file count
    # (genuine shared CODE survives — the documenso packages/lib anti-case).
    # Seg C: a python-module dispatch extractor (registry/handler-map dicts,
    # entry_points, __main__ CLI, celery tasks) emits routes_index kind
    # py-dispatch so python entry surfaces stop reading as perpetual
    # Uncovered. Seg D: the tRPC collector resolves lazy handler-cache
    # routers (UNSTABLE_HANDLER_CACHE / getHandler + a relatively-imported
    # ``router``) that the @trpc/server import gate skipped. Default ON since
    # the 2026-07-16 horizon-1 flip (KEY_SCHEMA 30; keyed proof hoppscotch +
    # cal green, B66-v2 — ghost 0-LOC drop, team owned>0). ``=0`` keeps every
    # segment inert, byte-identical to the merged B66+B67 world (kill-switch).
    "FAULTLINE_OWNERSHIP_V2",
    # B71 Seg D (2026-07-16) — flow-grain laws T1-T4. Re-grains the flow store
    # to the journey grain at the Stage 6.7 rollup boundary (before UF, so grain
    # disease is not amplified onto user_flows[]): T1 drops empty-span flows
    # (reverse-lookup contract break), T2 re-anchors barrel/re-export entries
    # onto their definition site, T3 folds a flow whose span-set is a subset of a
    # same-entry sibling's (Soc0 cases.py / hopp kernel index.ts twins), T4 folds
    # a same-entry fanout that shares an identical dominant span (documenso
    # rate-limits.ts x14). Folds union the loser's spans/paths into the winner
    # (conservation, merged_from lineage). Reshapes flows[] + feature_flow_edges[]
    # + user_flows[] membership. Default ON since the 2026-07-16 horizon-1 flip
    # (KEY_SCHEMA 30; keyed proof documenso + novu green, B71 — loc=0 flows -> 0).
    # ``=0`` keeps the flow store byte-identical (the block never runs;
    # kill-switch).
    "FAULTLINE_FLOW_GRAIN",
    # B71 Seg A-C (2026-07-16) — the naming pack: L-A1/L-A2 PF display route-
    # grammar + provenance-tier, L-B1 leaf-collision qualification, L-C1..L-C4 UF
    # synth echo-fold / verb-phrase integrity / same-noun-head families / board
    # name uniqueness, and the degraded-scan confidence scoping (auth-fail no
    # longer blanket-downgrades cache-validated UF domains). DISPLAY + confidence
    # channels (pf.display_name / uf.name / name_confidence / name_evidence /
    # name_provenance telemetry); identity/membership/paths untouched. Default
    # ON since the 2026-07-16 horizon-1 flip (KEY_SCHEMA 30; keyed proof
    # documenso + novu green, B71 — echo-fold in the rich boards, anti-cases
    # live). ``=0`` restores the pre-B71 byte-identical naming channels
    # (kill-switch).
    "FAULTLINE_NAMING_PACK",
    # B65-v3 (2026-07-16) — SPA router extraction, ONE flag gates both
    # segments (the B66 precedent): Seg A vue file-based pages
    # (vite-plugin-pages / unplugin-vue-router ``pages/**/*.vue`` in a
    # non-Nuxt Vue SPA — hoppscotch routes_index=1 at 33 real vue pages)
    # + Seg B react-router code config (JSX ``<Route path=...>`` trees +
    # ``createBrowserRouter`` object arrays; lazy-import target = entry —
    # Soc0 App.tsx 60+ routes invisible). Emits routes_index rows
    # method=PAGE kind=spa-page, so flows/journeys mint and the B65
    # partition surface-detect sees SPA product surfaces. Registration is
    # flag-gated at the registry (extractor_hits key parity, B67 lesson).
    # Default **ON** since the B65-v4 re-flip (2026-07-18, KEY_SCHEMA 31;
    # first flip v30 was R1-reverted at f6bd5d6 — route-page anchors won
    # mint/ownership off feature-dirs; re-landed with the mint-priority
    # fix chain: authored-subtree fence+floor predicate, spa-born mass
    # fence, member-twin bar, barrel-hop, template-literal honest skip.
    # Proof: full convoy2 on f38760e + operator panel PASS 2026-07-18).
    # Explicit =0 restores pre-B65-v3 byte-identically (kill-switch
    # forever); unset ≡ explicit "1".
    "FAULTLINE_SPA_ROUTER_ENTRIES",
    # S1 (2026-07-18) — owner-oracle: ONE deterministic file→owner election
    # (owner_oracle.elect_primary_owners — the Stage 6.97 rule: module-subtree
    # > non-facet > dir-count > flows > slug) replaces the two ORDER-SENSITIVE
    # first-claimant owner resolutions (indexes.build_path_index R1 by
    # features-list order + conservation.build_file_pf_owner R2 by dev-path
    # order). The census probe measured their split at PF grain 0.85%
    # documenso / 1.23% cal / 3.08% novu (dev grain 6.47/2.55/19.14) — the
    # inter-version "spilling" a feature insert/reorder caused with no change
    # in evidence. The oracle is computed ONCE post-devgrain (before the
    # emission path_index rebuild) and consumed by the path_index refresh (R1),
    # the terminal-home conservation votes (R2), and transitively i16 / dispatch
    # (they read the refreshed path_index). Facet/shared exclusion survives as
    # a COVERAGE VIEW over the election — same owner, filtered visibility, not
    # a separate rule. The oracle NEVER moves membership (READ-resolution only;
    # conservation ladder untouched). Reshapes path_index[].feature_uuid on
    # contested files + the UF homes those owners drive. Default ON (flipped
    # 2026-07-19, S*-pack, KEY_SCHEMA 32 — census-disagreement 0, UF-057
    # rehome, keyed A/B novu, panel ON >= OFF strict); =0/false/off restores
    # the first-claimant resolutions byte-identically — explicit off stays a
    # valid kill-switch forever.
    "FAULTLINE_OWNER_ORACLE",
    # S2 Seg D (2026-07-18) — degradation-honesty stamp: a scan that finished in
    # a visibly-degraded LLM state (the refiner's whole fresh batch failing at
    # cost==0 = a dead key mid-scan, or the 6.7d journey abstraction leaving
    # applied=False with real candidates) records typed records in
    # scan_meta.degradations[] with severity="failed" so validate_scan /
    # keyed_proof FAIL the proof gate instead of scoring a fail-open board that
    # self-reports healthy (264->78 Soc0 fail-open, probe 2026-07-18). Default
    # ON (flipped 2026-07-19, S*-pack, KEY_SCHEMA 32 — telemetry-only, 4-stage
    # live-fire stamp proof); =0/false/off appends nothing → degradations[]
    # byte-identical — explicit off stays a valid kill-switch forever.
    "FAULTLINE_DEGRADATION_STAMP",
    # S2 Seg B' (2026-07-18) — uf_refiner per-UF output-token budget: the fixed
    # 1500-token DEFAULT truncates a large domain's structured JSON response
    # mid-object -> json_parse_failed -> the whole domain keeps deterministic
    # names (Soc0 13:25Z: the 3 degraded domains were EXACTLY the 3 largest —
    # network 26 / service 18 / detector 17 UFs; next-largest admin 11 refined
    # cleanly). ON scales max_tokens by a structural per-UF allowance floored at
    # DEFAULT so large domains parse. Reshapes user_flows[].name/description/
    # intent/ui_tier/acceptance on the previously-degraded (largest) domains
    # only. Default ON (flipped 2026-07-19, S*-pack, KEY_SCHEMA 32 — 3 domains
    # refine clean, no truncation); =0/false/off -> max_tokens==DEFAULT + cache
    # key unchanged -> byte-identical — explicit off stays a valid kill-switch
    # forever.
    "FAULTLINE_UF_REFINE_TOKEN_SCALE",
    # S2 Seg A (2026-07-18) — deterministic UF pre-clustering: journey STRUCTURE
    # is computed deterministically (Stage 6.7a: one conservation-complete
    # cluster per rollup domain) and the LLM layer only NAMES it (6.7b refiner);
    # the structural LLM stages (6.7c mega-split, 6.7d journey rewrite) are
    # skipped. UF-COUNT becomes invariant to LLM death (fail-open 264-vs-78
    # class) and to resampling (−26% whole-batch class) — probe 2026-07-18.
    # Reshapes user_flows[] (membership/grain/ids) + downstream homes.
    # Default ON since the 2026-07-21 pack-3 flip (KEY_SCHEMA 34) — the
    # S2-A RETURN: flipped ON in the 2026-07-19 S*-pack (KEY_SCHEMA 32),
    # UN-flipped the same day (corpus regression audit 4x WORSE — the
    # det-cluster naming layer emitted bare 'Manage <plural>' bins
    # corpus-wide: twenty 143 / midday 29 / documenso 'Manage os/ts' /
    # novu 61% bare), and RE-flipped after the R5 corpus naming wave +
    # spray-generalization cured the collapse (ledger §S2-A-V3: twenty
    # spray 17→0, settings-PF 36→22, conservation 328==328, 0 false, I14
    # dangling 0; pair flag FAULTLINE_SPRAY_GENERALIZED flips together).
    # ``=0``/false/off restores the LLM-structured path byte-identically —
    # explicit off stays a valid kill-switch forever (unset ≡ explicit
    # "1").
    "FAULTLINE_UF_DET_AGGREGATION",
    # S2 Seg C (2026-07-18) — canonical LLM batch composition: volatile pure
    # counts leave the 6.7d prompt canon (digest n_dev_features; Call-2 per-row
    # n_files) and the digest's UF ordering uses log2 weight-buckets instead of
    # raw member_count, so a count-only drift (the 1-of-980 resample trigger)
    # no longer flips the whole-batch cache key; real content changes still do.
    # Adds the flag-gated scan_meta.llm_batch_canon hit-rate telemetry.
    # Reshapes the 6.7d prompts (hence keyed user_flows[]/product_features[]
    # composition on cache-miss worlds). Default ON (flipped 2026-07-19,
    # S*-pack, KEY_SCHEMA 32 — cache-hit-rate on the 1/980 drift); =0/false/
    # off keeps digest, prompts and keys byte-identical — explicit off stays
    # a valid kill-switch forever.
    "FAULTLINE_LLM_BATCH_CANON",
    # S5a (2026-07-18) — mega-PF decomposition ARMING, ONE flag gates the
    # three ship-grade grain segments (the B53/B58-v3 precedent). Seg A: the
    # B24 TargetGrainIndex derives ADDITIONAL route roots from the
    # routes_index population (a non-dialect central/backend router file —
    # Soc0 backend/routers/admin.py — contributes its parent dir as a root
    # when >=2 distinct product route files cluster there; the flat-leaf
    # stem rule keys each nav group). Seg B: a route-GROUP target with no
    # exact-anchor PF resolves to the SIBLING PF whose core-identity token
    # it UNIQUELY echoes (compliance group -> compliance-page) instead of
    # minting a twin. Seg C: the Stage 6.99b organic re-home candidates
    # (telemetry-only under B69-v2) become B24-class moves routed through
    # the S3 overturn ledger (rung mega). Reshapes user_flows[].
    # product_feature_id + product_features[] (mints/feeds) +
    # developer_features[] carve chunks on repos where B24 fires. Default
    # ON (flipped 2026-07-19, S*-pack, KEY_SCHEMA 32 — healthy novu pair +
    # re-panel after the K2 final); =0/false/off -> both grain params False
    # + the 6.99b organic branch stays a continue -> byte-identical —
    # explicit off stays a valid kill-switch forever. (The trigger-shape
    # Seg D/E ride a separate mandate.)
    "FAULTLINE_MEGA_DECOMP_ARM",
    # S5a-it2 (2026-07-18) — generated-code CONTENT-marker probe inside the
    # existing Stage 6.9b channel (the module docstring's promised
    # follow-up): a file whose head banner carries the universal codegen
    # markers (Go-spec "Code generated … DO NOT EDIT" line class /
    # @generated marker / generated-declaration + do-not-edit admonition
    # pair — Speakeasy/orval/openapi-generator/graphql-codegen classes)
    # strips like a filename-convention match. Kills the novu
    # libs/internal-sdk react-query client (923 hand-named .ts, ~107K LOC)
    # at the SOURCE: its flows/UF garbage never survive 6.9b. Reshapes
    # developer_features[]/flows[]/user_flows[] on repos vendoring codegen
    # output. Default ON (flipped 2026-07-19, S*-pack, KEY_SCHEMA 32 —
    # healthy novu pair + re-panel); =0/false/off -> filename-only
    # predicate, byte-identical — explicit off stays a valid kill-switch
    # forever.
    "FAULTLINE_GENERATED_CONTENT_MARKER",
    # S4-a (2026-07-18) — App-Router keyless route extractor: emits
    # routes_index entries for app/**/page.tsx + app/**/route.ts trees whose
    # scope is not cleanly next-app-router-tagged (the monorepo residue where
    # the composite replaced the stock route source — cal apps/web 0/939 — and
    # the polyglot leftover scanned with the js-generic root tag — onyx
    # web/src/app 0/114). Keys on the App-Router CONVENTION (page/route leaf
    # under an app/ or src/app/ root run, matched anywhere) via its own source
    # name (route-approuter), so Stage-1 replace-by-name never narrows it and
    # the js-generic leftover tag never suppresses it. Contained: emits ONLY
    # explicit routes_index rows (the App-Router profile already anchors the
    # files' features/flows via the same first-segment slug — same-slug merge,
    # no twin); build_routes_index folds them LAST so a clean next-app-router
    # repo's stock rows win the dedup byte-identically. Default ON (flipped
    # 2026-07-19, S*-pack, KEY_SCHEMA 32 — cal 0->249 / onyx 0->106 routes,
    # conservation 0; KS-sets of both class forms + wave-17); =0/false/off
    # keeps the extractor unregistered (extractor_hits byte-identical — the
    # B67 kill-switch lesson) — explicit off stays a valid kill-switch
    # forever.
    "FAULTLINE_APPROUTER_KEYLESS",
    # S4b (2026-07-18) — Go extraction repair. On real Go repos the shipped
    # go-router extractor mints HTTP-header names and JSON/struct keys as
    # "features": the chi/gin/echo ``route_call`` patterns match any bare
    # ``.Get("s")`` / ``.Set("s")`` and Go code is saturated with
    # ``req.Header.Get("Content-Type")`` etc. (traefik VERIFIED: 19/19
    # go-router anchors were header garbage, 0 real routes), while its actual
    # ``/api/**`` + ``/debug/**`` surface — registered via the gorilla/mux
    # fluent chain ``router.Methods(..).Path("/x").HandlerFunc(..)`` — is
    # invisible (no gorilla signature). Armed, the extractor (a) adds the
    # gorilla/mux registration signature and (b) requires every matched string
    # to be a URL PATH (``route_must_be_path``: starts with ``/`` or a
    # method-prefixed ``"GET /x"`` ServeMux pattern), dropping the header/key
    # false positives with no per-repo vocabulary, plus excludes testdata/
    # example fixtures (dev-artifact law). Reshapes go-router anchors only —
    # go-package directory anchors are untouched. Default ON (flipped
    # 2026-07-19, S*-pack, KEY_SCHEMA 32 — traefik 0->1 PF/51 routes,
    # ollama 0->2/76); the flag is read at collect time so the cached
    # pattern bundle serves OFF and ON alike — =0/false/off restores the
    # shipped board byte-identically — explicit off stays a valid
    # kill-switch forever.
    "FAULTLINE_GO_EXTRACTION",
    # S5b (2026-07-19) — leaf-route dissolution + platform promotion, ONE
    # arbiter wave (the S5a precedent). Seg B re-homes a route:-leaf black
    # hole's annexed member devs to real siblings (novu duplicate-workflow
    # 315/2 -> subscribers/analytics/domains/…; unhomed devs lane, freeing
    # their pages); Seg C promotes buried product surfaces — platform-lane
    # residents with PAGE evidence (a-lite mirror; P1 page-cohort ∪ P2
    # lane-token↔freed-page bridge) — by birth (S5a birth-law) or
    # merge-into-sibling (the notifications class). Reshapes
    # developer_features[].product_feature_id + product_features[]
    # (births/merges/leaf-shed) on repos where a leaf hole or a buried
    # surface exists. Default OFF; =0/unset keeps the stage un-entered,
    # byte-identical. Appended WITHOUT a KEY_SCHEMA bump — the bump rides
    # the separate flip commit (flip-protocol).
    "FAULTLINE_LEAFROUTE_PROMOTION",
    # display-cross gate (2026-07-19) — B71 provenance-ladder consumer. Armed,
    # the nav authored label feeding a PF's display is kept only on identity
    # evidence (tokens intersect the PF name/anchor OR its member-dominant path
    # tokens); a foreign label (cal ``insights`` display 'Bookings',
    # ``organization`` -> 'directory_sync') is reverted to the honest basename,
    # equal-vote ties prefer the PF's own anchor-page self-link, and the
    # survivor is title-cased. Reshapes product_features[].display_name +
    # scan_meta.naming_contract.pf_display_provenance only. Default ON since
    # the 2026-07-21 pack-2 flip (KEY_SCHEMA 33; keyed proof cal + novu green
    # — sim==engine 7/21 exact, cal 5→0 false displays, novu footprint exactly
    # 1 row; ledger display-cross pack 2026-07-19). ``=0``/false/off restores
    # the shipped display byte-identically — explicit off stays a valid
    # kill-switch forever (unset ≡ explicit "1").
    "FAULTLINE_PF_DISPLAY_EVIDENCE_GATE",
    # workspace UNION gate (2026-07-19, onyx shape) — when the declared
    # workspaces span a strict MINORITY of tracked files (scale-invariant
    # covered*2 < total), they are unioned with the non-overlapping
    # synthesise_workspaces results so the undeclared product bulk (onyx
    # web/ Next app + cli/ Python) scopes to its own stack instead of
    # dissolving into the js-generic leftover pass. Reshapes the Stage-1
    # workspace partition and therefore every downstream layer on
    # minority-declared repos; high-coverage declared monorepos
    # (langfuse/supabase/typebot) are ratio-gate inert. Default ON since
    # the 2026-07-21 pack-2 flip (KEY_SCHEMA 33; keyed A/B onyx green —
    # fallback 57.1→36.5%, web-residual 2114→0; anti-cases langfuse/
    # typebot byte-ident even armed; ledger onyx union-gate cycle).
    # ``=0``/false/off restores the pre-gate Stage 1 byte-identically —
    # explicit off stays a valid kill-switch forever (unset ≡ explicit
    # "1"). NOTE: registered at flip time — the add-cycle omitted the
    # ENV_OUTPUT_FLAGS registration (cache-keying gap found at the flip
    # audit; the v33 bump invalidates every entry cached under the gap).
    "FAULTLINE_WORKSPACE_UNION",
    # B73 organic-move (2026-07-19, fork-A ruling) — the ratified
    # strict+gated organic UF re-home rule at the 6.99b rail: armed, it
    # REPLACES the S5a mega-organic handling of the rail's candidates with
    # home_share==0.0 ∧ rival_share>=0.8 (inclusive) + the product→dev
    # direction-gate; PURE arbiter moves (rung organic-move), no renames,
    # no folds, no I8 orphan-guard (explicit ruling — sole-UF from-PFs are
    # the disease). Reshapes user_flows[].product_feature_id on repos with
    # strict candidates. Default ON since the 2026-07-21 pack-2 flip
    # (KEY_SCHEMA 33; keyed proof Soc0 green — moved=0, UF-051 blocked
    # reason='prior-hold' hold='cross_app_target', conservation
    # 399→399/0/0; typebot keyed-evidence trio byte-inert, KS ON == it1
    # digest; ledger §B73-IT2). ``=0``/false/off keeps the branch
    # un-entered, byte-identical (including the mega-armed path) —
    # explicit off stays a valid kill-switch forever (unset ≡ explicit
    # "1").
    "FAULTLINE_ORGANIC_MOVE",
    # R5 corpus naming-wave master (2026-07-19) — one flag gates the five R5
    # naming segments' NEW display-channel behaviors: identity-parity
    # reject/qualify (a PF display folding to ANOTHER live PF's canonical slug),
    # own-resource echo-hub templating, member-evidence dir-token humanization,
    # the compose-joint qualifier normalizer, and the negative confidence rungs
    # (census-shape name_confidence caps + name_evidence stamps). Reshapes
    # product_features[].display_name + user_flows[].name/name_confidence/
    # name_evidence + scan_meta.naming_contract telemetry only — canonical
    # identity untouched. Default ON since the 2026-07-21 pack-2 flip
    # (KEY_SCHEMA 33; keyed A/B twenty + papermark green — phase-1 cures
    # hold paren-high 17→0 / dups 3→0, brand-echo stamps ×5, measured
    # demote high 101→92, keyed PF layer only 2 medium = no over-demotion;
    # ledger §R5-PHASE2). ``=0``/false/off restores the pre-R5 emission
    # byte-identically — explicit off stays a valid kill-switch forever
    # (unset ≡ explicit "1").
    "FAULTLINE_NAMING_WAVE_R5",
    # S2-A-v3 (2026-07-19) — spray-generalization: the generalized R5-2 spray
    # predicate over the UNPARENTHESIZED tech-dir-suffix form the
    # det-aggregation regrain channel mints ('Manage setting AI components/
    # constants/…' — twenty-b exhibit): >=3 same-PF siblings sharing a
    # >=2-token prefix (G1) whose tail token names the members' own leaf
    # directory (singular(tail)==singular(leaf-dir) on >=50% member paths)
    # collapse — whole group, structural misses ride along — into ONE
    # own-resource parent row ('Manage AI settings' form; member-union
    # conservation, I14 repoint). Paren-tail rows are R5-2's class and are
    # never touched (G0). Reshapes user_flows[] (row count/names/membership)
    # + the scan_meta.spray_generalized telemetry on armed worlds only.
    # Default ON since the 2026-07-21 pack-3 flip (KEY_SCHEMA 34; census
    # twenty (R5+DET_AGG world) — spray 17→0, parents 'Manage AI settings'
    # mc38 / 'Manage application settings' mc52 / 'Manage data model
    # settings' mc25, settings-PF 36→22, conservation 328==328, 0 false,
    # I14 dangling 0; KS byte-ident typebot+openstatus; flips together
    # with its pair FAULTLINE_UF_DET_AGGREGATION — ledger §S2-A-V3).
    # ``=0``/false/off keeps the pass un-entered, byte-identical —
    # explicit off stays a valid kill-switch forever (unset ≡ explicit
    # "1").
    "FAULTLINE_SPRAY_GENERALIZED",
    # B74 Seg C (2026-07-19, probe-canon tune-first) — home-pure container
    # inherit: a journey member whose HOME PF is a monorepo ws-pkg CONTAINER
    # (anchored-mint ``anchor_id`` "ws:" marker — Form A only; mass/ratio
    # forms refuted) is inheritable like lane/unowned on the CITED channels
    # (Pass-1 from_flows + Pass-2a cited devs). The 2b whole-pool rescue and
    # the route backfill stay home-STRICT. Reshapes user_flows[] membership
    # (twenty: 'Sign in and authenticate' 0->11 members) + the 6.7d
    # uf_home_filtered telemetry on ws-container repos only. Default ON
    # since the 2026-07-21 pack-3 flip (KEY_SCHEMA 34; keyed proof twenty
    # green — 'Sign in and authenticate' mints 14 members on the live
    # channel, filter 24,226→2,081, dropped exactly {blocklist by-design,
    # SSO}, members 561→801 (+240 rescue), husks 0, degradations 0;
    # ledger §B74 SEG C). ``=0``/false/off keeps the strict filter
    # byte-identical — explicit off stays a valid kill-switch forever
    # (unset ≡ explicit "1"); armed on a repo WITHOUT ws-containers is
    # inert (KS: openstatus).
    "FAULTLINE_HOME_PURE_CONTAINER_INHERIT",
    # B74 Seg A (2026-07-20) — SPA route-table extraction: exported enum /
    # flat-const route tables (twenty AppPath/SettingsPath, novu ROUTES)
    # consumed in path-position by a router file emit their URL patterns
    # (method=PAGE, kind=spa-page) + owner-page anchor evidence, so
    # capability journeys mint on SPA repos whose routes live in tables.
    # Reshapes routes_index + spa-page candidates on armed worlds only.
    # Default ON since the 2026-07-21 pack-3 flip (KEY_SCHEMA 34; keyed
    # proof twenty + novu green — twenty routes_index +114 PAGE
    # (AppPath/SettingsPath consumption-primary canon), novu +86 incl.
    # /auth/sign-in; 0/2,042 false candidates; container-PF guard holds
    # — 'twenty-shared' 13.8K phantom dead; ledger §B74 SEG A).
    # ``=0``/false/off keeps the arm un-entered, byte-identical —
    # explicit off stays a valid kill-switch forever (unset ≡ explicit
    # "1").
    "FAULTLINE_SPA_ROUTE_TABLE",
    # B74 Seg B (2026-07-20, F3′ re-entry probe SHIP/high) — post-grain
    # flow re-derivation at Stage 6.865: stage-8-born / re-membered dev
    # features (non-test path-set ≠ the stage-3 unit; removal-only
    # deltas excluded) re-run the EXISTING Stage-3 flow machinery
    # between the 6.86 mint window and the Stage 6.7 UF family — the
    # single existing mint sees the new flows naturally (no new mint
    # channel). Healthy flow-density is a secondary EXCLUDE-only filter
    # (< repo flowful median / _OVERSIZED_MEDIAN_MULT); chunk
    # eligibility is ratio-triggered (exports/paths prompt caps)
    # independent of the global oversized cut; locale-births die on the
    # existing MIN_EXPORTS gate. Reshapes flows[] /
    # developer_features[].flows[] / feature_flow_edges[] and,
    # transitively, the minted user_flows[] layer on repos with
    # post-stage-3 grain changes; armed no-fire boards are
    # byte-identical (telemetry key only on fire — Seg C inertness
    # law). Default ON since the 2026-07-21 pack-3 flip (KEY_SCHEMA 34;
    # keyed proof twenty green — 894 live calls, +3,712 flows
    # (1400→3673), UF 120→169, members 561→1211; golden targets taken:
    # tasks 34m, workflows 44m (was 2-loc), AI-assistant 13m,
    # calendar/data-model/marketplace/auth-tokens; husks 0,
    # degradations 0; ledger §B74 SEG B; the UF-giant blocker resolved
    # by B75 cases-split — same pack). ``=0``/false/off keeps the stage
    # un-entered, byte-identical — explicit off stays a valid
    # kill-switch forever (unset ≡ explicit "1").
    "FAULTLINE_FLOW_REDERIVE_POSTGRAIN",
    # S5b Seg H (2026-07-21, probe-canon tune-first) — digest stratification:
    # the 6.7d Call-1 digest stops starving the page surface under the caps.
    # M1-ADDITIVE appends page-anchored UFs BEYOND the mass-sorted UF cap
    # (reservation append — displacement=0; the fixed-cap form starved 5/67
    # cached proposals and was refuted); M2-HYGIENE-QUOTA reserves half the
    # route budget for the hygienic page stream under route pressure
    # (storybook/dev-artifact paths + filename-echo pseudo-routes demoted
    # out of the quota only). Digest/prompt/cache-key change on pressured
    # repos → user_flows[] + product_features[] reshape via Call-1. No-
    # pressure repos are byte-identical (inertness law). Default ON since
    # the 2026-07-21 pack-3 flip (KEY_SCHEMA 34; keyed proof novu green —
    # 'Sign in to existing account' mints (3m; 0/12 draws for 8 days
    # before), 'View analytics and activity charts' 1→5m, UF 88→92,
    # members 472→565, degradations 0; telemetry 46 page-anchored/35
    # appended/73 page-routes; ledger §S5b-H). ``=0``/false/off keeps
    # both cuts byte-identical — explicit off stays a valid kill-switch
    # forever (unset ≡ explicit "1").
    "FAULTLINE_DIGEST_STRATIFICATION",
    # B76 (2026-07-21) — metrics recompute-on-emission: re-runs the Stage-6
    # commit-metric sweep over the FINAL membership after the last
    # membership-mutating finalize stage (mint-time metric zeroing class:
    # tc=0 rows wearing inherited authors/health; PF sum-over-contributors
    # both losing zeroed mass and double-counting shared commits — Alerts
    # 5 vs ~63). PF metrics recompute from each PF's OWN path-set with
    # per-commit dedup; the four mint factories stamp an honest null-state
    # instead of deep-copying identity. Reshapes total_commits/bug_fixes/
    # bug_fix_ratio/authors/last_modified/health_score/health_confidence
    # on developer_features[] + product_features[] (metric channel only —
    # membership and hotspot_files untouched). Default ON since the
    # 2026-07-21 pack-3 flip (KEY_SCHEMA 34; census ×3 repos on the
    # 7-flag battery, same-world — "impossible" tc=0∧authors>0 rows
    # 60+17/162+42/84+9 → 0+0 on ALL, hotspots byte-ident (full join 0
    # diffs), dedup law dev-sum ≤ repo commits; both mechanism halves
    # load-bearing; ledger §B76). ``=0``/false/off keeps the pass
    # un-entered and the factory inheritance byte-identical — explicit
    # off stays a valid kill-switch forever (unset ≡ explicit "1").
    "FAULTLINE_METRICS_RECOMPUTE",
    # B77 (2026-07-21, forensics-canon §ФОРЕНЗИКА 502M) — residual
    # citability: the 6.7c mega-split RESIDUAL bucket is marked
    # structurally (``UserFlow.residual``) and stops being a wholesale
    # Pass-1 ``from_flows`` inherit source (its members stay in the
    # bucket / the token-gated grounding channels — no-orphan); Pass-1
    # container-inherit gains Pass-2a's own ``& utok`` content-token
    # affinity gate; a built UF majority-voting for >1 real PF home with
    # no common majority is carved per home (mint-side, existing
    # member_votes mechanism); a ws-container PF is not a valid
    # conservation resettle target. Reshapes user_flows[] membership on
    # armed composite worlds (the class lives ONLY on the FLOW_REDERIVE ×
    # CONTAINER_INHERIT interaction: twenty 502m/278m/216m mass-transfer
    # giants). Default ON since the 2026-07-21 pack-3 flip (KEY_SCHEMA
    # 34; replay census on the twenty capture — 502-class 542→7 (target),
    # AI-agents 216→7, marketplace 278→25; buckets keep members as honest
    # residual rows — 0/2211 losses (the OFF world LOST 957); anti-cases
    # 23/26/6 intact; KS byte-ident both worlds; ledger §B77).
    # ``=0``/false/off keeps 6.7c/6.7d/conservation byte-identical —
    # explicit off stays a valid kill-switch forever (unset ≡ explicit
    # "1").
    "FAULTLINE_RESIDUAL_CITABILITY",
    # B75 (2026-07-21, probe-canon supports/tune-first) — UF-giant
    # cases-split: giant catch-all journeys (mc >= the census band edge
    # 30) re-grain into >= K dir-tree surface cases + a residual that
    # keeps the parent row (survivor-id law); vendored-dir guard (S3
    # no-product-surface ∧ S1 dependency-family echo) rejects technical
    # children BEFORE extraction (tracecat tiptap/ai exhibit). Reshapes
    # the emitted user_flows[] layer + flow backpointers on boards that
    # carry giants; 0-giant boards are byte-identical (inertness law).
    # Default ON since the 2026-07-21 pack-3 flip (KEY_SCHEMA 34;
    # sim-canon on the composite keyed twenty world — 209m 'Browse object
    # record' → 10 children / 131m → 11 (settings names), 86m golden
    # 'Create and edit records' = 4 recognizable cases + residual
    # lineage, 502m/220m honestly KEPT (B77's class — division of labor
    # proven by simulation); keyless inertness ×3 topologies byte-ident;
    # suite² failset-diff empty; ledger §B75). ``=0``/false/off keeps
    # the seam un-entered, byte-identical — explicit off stays a valid
    # kill-switch forever (unset ≡ explicit "1").
    "FAULTLINE_UF_CASES_SPLIT",
)

#: Bump when the KEY composition changes so old entries can't be served
#: against a new key layout (they simply won't match — silent invalidation).
#: v3 (W5.1): added FAULTLINE_LATTICE_THIN_FOLD + FAULTLINE_LOC_WORTHY_BACKSTOP.
#: v4 (B4): added FAULTLINE_SYNTH_QUALITY.
#: v5 (B13): added FAULTLINE_BACKSTOP_OWNED_COVER.
#: v6 (B15): added FAULTLINE_SHARED_LEAF_CONSISTENCY.
#: v7 (B15b): added FAULTLINE_DATA_LEAF.
#: v8 (B16): added FAULTLINE_PF_NAME_LAW.
#: v9 (B16 Part 2): added FAULTLINE_PF_SIBLING_UNIFY.
#: v10 (B19): added FAULTLINE_TECH_TRANSPORT_LANE.
#: v11 (B20): added FAULTLINE_I16_REHOME_B20.
#: v12 (B22a): added FAULTLINE_FOLD_CROSSAPP_GUARD.
#: v13 (B22): added FAULTLINE_TRANSPORT_LANE_HANDOFF + FAULTLINE_TRANSPORT_HANDOFF_PLURALITY.
#: v14 (B23): added FAULTLINE_MARKER_SURFACE_COORDS.
#: v15 (B24): added FAULTLINE_MEGA_PF_NAV_REHOME.
#: v16 (B24 flip): FAULTLINE_MEGA_PF_NAV_REHOME default OFF -> ON — the
#: default flip changes what "unset" means, so cached entries keyed under
#: unset must not be served across it (B4 precedent).
#: v17 (B26+B27): added FAULTLINE_HUB_PLUMBING_CHILD + FAULTLINE_PF_MANIFEST_NAME.
#: v18 (B30): added FAULTLINE_FLOW_NAME_V2.
#: v19 (B31): added FAULTLINE_RECALL_ROW_NAMES.
#: v20 (B34): added FAULTLINE_LAZY_IMPORT_EDGES + FAULTLINE_DISPATCH_REGISTRY_FLOWS.
#: v21 (B34 flip): both B34 flags default OFF -> ON — the default flip changes
#: what "unset" means, so cached entries keyed under unset must not be served
#: across it (B4/B24 precedent).
#: v22 (B25): added FAULTLINE_JOURNEY_LATTICE_B25.
#: v23 (B34 dispatch revert): FAULTLINE_DISPATCH_REGISTRY_FLOWS default ON -> OFF
#: (supabase 328 hollow UI-demo mints; re-flip after B34-b rails + keyed proof).
#: v24 (B28): added FAULTLINE_NONPRODUCT_SCOPE + FAULTLINE_DOCS_REANCHOR.
#: v25 (B34-b re-flip): FAULTLINE_DISPATCH_REGISTRY_FLOWS default OFF -> ON
#: (rails merged; hollow=0 proof on keyed supabase + Soc0).
#: v26 (B38): added FAULTLINE_MARKER_COORDS_REQUIRED + default flip ON at merge.
#: v27 (B45+B40+B46 flip): FAULTLINE_COVERAGE_GAP_CHANNEL default off -> full,
#: FAULTLINE_NAME_EVIDENCE_RUNGS default OFF -> ON, FAULTLINE_UF_NAME_HYGIENE
#: default OFF -> ON — default flips change what "unset" means, so cached
#: entries keyed under unset must not be served across them (v16/v21/v25
#: precedent). Keyed proof: papermark + cal.com green (2026-07-12).
#: v28 (B33+B37-ph2 flip): FAULTLINE_FDIR_DEVGRAIN_GATE default OFF -> ON +
#: FAULTLINE_DISPATCH_HOMING_B37P2 default OFF -> ON — COUPLED: B33's 6.987
#: demote is only I9-safe with the homing rider (flowful demoted devs home
#: to their target-owner PF instead of the platform lane). Default flips
#: change what "unset" means, so cached entries keyed under unset must not
#: be served across them (v16/v21/v25/v27 precedent). Keyed proof:
#: papermark green (Welcome demoted, I9=0, lane 43->42, 2026-07-12).
#: v29 (B62 flip): the proven campaign set flips default OFF -> ON in ONE
#: commit (14 flags): FAULTLINE_REACT_ROUTER_FW_PROFILE, _ROUTER_ALIAS_RESOLVE,
#: _KEYLESS_JOURNEY_RECALL, _WS_LIBRARY_LANE, _UF_NAME_DEGRIME,
#: _UF_RESOURCE_RUNG, _FLOWFUL_TRANSPORT_LANE, _WS_BLOB_DOMAIN_DRAIN,
#: _PF_FULLNAME_LAW, _UF_RUNG_SOURCES_V2, _STAGE_6_7E_ADJUDICATOR,
#: _ANNEXATION_GUARD, _ARTIFACT_INK_LANE, _UF_VERB_SNAP. Default flips change
#: what "unset" means, so cached entries keyed under unset must not be served
#: across them (v16/v21/v25/v27/v28 precedent). Every flag keeps its X=0
#: kill-switch (explicit "0"/"false"/"off" still disables — inverted-
#: kill-switch unit per flag). B49/B51 transport flags stay OFF (superseded
#: by B52 FLOWFUL_TRANSPORT_LANE). DISPATCH_RESOLVER (B64) NOT in this flip.
#: scan_meta.key_schema=29 emitted so downstream rulers gate new-world logic.
#: v30 (horizon-1 flip, 2026-07-16 — plan docs/anchor-arc/flip-pack-20260716.md):
#: the horizon-1 pack flips 10 previously-default-OFF flags to default ON, each
#: in its own commit (per flip-protocol), the bump riding this final commit:
#: FAULTLINE_TERMINAL_CLASSIFICATION (B68), _SERVER_API_ENTRIES (B66),
#: _HOMING_HYGIENE (B69-v2), _NAMING_LAW (B70), _RECALL_QUAL_CASING (B70),
#: _GRAIN_WAVE (B58-v3), _OWNERSHIP_V2 (B66-v2), _SPA_ROUTER_ENTRIES (B65-v3),
#: _NAMING_PACK (B71), _FLOW_GRAIN (B71). Default flips change what "unset"
#: means, so cached entries keyed under unset must not be served across them
#: (v16/v21/v25/v27/v28/v29 precedent). Every flag keeps its X=0 kill-switch
#: (explicit "0"/"false"/"off" still disables — inverted-kill-switch unit per
#: flag). NOT flipped (stay OFF): FAULTLINE_JOBS_ENTRIES (awaits B24 PF-assembly
#: guard), _SEED_HYGIENE (no own gates), _BLOB_PARTITION (parked). B49/B51
#: transport flags stay OFF (superseded). scan_meta.key_schema=30 emitted so
#: downstream rulers gate new-world logic.
#: v31 (B65-v4 re-flip, 2026-07-18): FAULTLINE_SPA_ROUTER_ENTRIES default
#: OFF -> ON again — the R1 revert (f6bd5d6) had returned its unset
#: semantics to OFF under schema 30, so 21 cold boards are cached with
#: unset=OFF; the re-flip changes what "unset" means and cached entries
#: keyed under it must not be served across (v16/v21/v25/v27/v28/v29/v30
#: precedent). Re-land proof: B65-v4 mint-priority fix chain (authored-
#: subtree fence+floor predicate, spa-born mass fence, member-twin bar,
#: barrel-hop, template-literal honest skip), full convoy2 on f38760e +
#: operator panel PASS (2026-07-18). Explicit "0"/"false"/"off" stays a
#: valid kill-switch forever (inverted-kill-switch unit: unset ≡
#: explicit "1" byte-identical).
#: v32 (S*-pack flip, 2026-07-19 — plan
#: docs/anchor-arc/flip-pack-s-strategy-20260718.md, operator-ratified):
#: the S* strategy pack flips 10 previously-default-OFF flags to default ON,
#: each flag/group in its own commit (per flip-protocol), the ONE bump riding
#: the FIRST commit of the pack: FAULTLINE_DEGRADATION_STAMP (S2-D,
#: telemetry-only, safest first), _OWNER_ORACLE (S1, panel ON >= OFF strict),
#: _UF_DET_AGGREGATION + _UF_REFINE_TOKEN_SCALE + _LLM_BATCH_CANON (S2
#: quartet — designed as a stack; keyless obstacle baselines re-recorded for
#: the S2 regrain 141->85 class), _OVERTURN_ARBITER (S3 — byte-ident x3
#: pre-flip, the flip is the overturns telemetry), _APPROUTER_KEYLESS (S4a,
#: cal 0->249 / onyx 0->106), _GO_EXTRACTION (S4b, traefik 0->1 PF/51
#: routes, ollama 0->2/76), _MEGA_DECOMP_ARM + _GENERATED_CONTENT_MARKER
#: (S5a, healthy novu pair + re-panel). Default flips change what "unset"
#: means, so cached entries keyed under unset must not be served across them
#: (v16/v21/v25/v27/v28/v29/v30/v31 precedent). Every flag keeps its X=0
#: kill-switch (explicit "0"/"false"/"off" still disables — inverted-
#: kill-switch unit per flag: unset ≡ explicit "1" byte-identical). NOT
#: flipped (stay OFF): S4c (precondition: the shards-orphan rule) and S4d
#: (precondition: its own keyed UF-proof) — each rides a later cycle.
#: scan_meta.key_schema=32 emitted so downstream rulers gate new-world logic.
#: v32 amendment (same day, it2): FAULTLINE_UF_DET_AGGREGATION UN-flipped
#: back to default OFF — the mandatory corpus regression audit (7 pairs)
#: came back 1 BETTER / 2 MIXED / 4 WORSE with the single root corpus-wide
#: in the A-flip: the det-cluster naming layer (regrain it3-it6) was
#: panel-hardened only on Soc0/novu and emits bare 'Manage <plural>' bins
#: elsewhere (twenty 143, midday 29 bins, documenso 'Manage os/ts', novu
#: 61% bare + paren 0->33). The other 9 pack flags STAY ON (their wins are
#: real: agents 187K->80K, twenty-front 88K->40K, Soc0 BETTER). KEY_SCHEMA
#: stays 32 — unset semantics changed twice within one unreleased schema
#: generation; the naming layer returns via R5 corpus hardening in its own
#: cycle with its own flip.
#: v33 (flip-pack №2, 2026-07-21 — plan docs/anchor-arc/flip-pack-2-20260719.md,
#: operator-ratified 2026-07-21): the pack flips 4 previously-default-OFF flags
#: to default ON, each in its own commit (per flip-protocol), the ONE bump
#: riding the pack's FIRST commit: FAULTLINE_PF_DISPLAY_EVIDENCE_GATE
#: (display-cross; keyed proof cal + novu — sim==engine 7/21 exact, cal 5→0
#: false displays, novu footprint 1 row), _WORKSPACE_UNION (onyx union-gate;
#: keyed A/B onyx fallback 57.1→36.5%, web-residual 2114→0; anti-cases
#: langfuse/typebot byte-ident even armed), _NAMING_WAVE_R5 (phase2 merged;
#: keyed A/B twenty+papermark — paren-high 17→0, dup 3→0, brand-echo ×5,
#: measured demote 101→92 high), _ORGANIC_MOVE (B73-it2; keyed Soc0 moved=0,
#: UF-051 prior-hold, conservation 399→399; typebot evidence-trio byte-inert).
#: Default flips change what "unset" means, so cached entries keyed under
#: unset must not be served across them (v16/v21/v25/v27/v28/v29/v30/v31/v32
#: precedent). Every flag keeps its X=0 kill-switch (explicit "0"/"false"/
#: "off" still disables — inverted-kill-switch unit per flag: unset ≡
#: explicit "1" byte-identical). scan_meta.key_schema=33 emitted so
#: downstream rulers gate new-world logic.
#: v34 (flip-pack №3, 2026-07-21 — ledger «ПАК №3 ПОВНІСТЮ ЗІБРАНИЙ» /
#: «ПАК №3 КОМПЛЕКТНИЙ», operator-ratified 2026-07-21): the pack flips 9
#: previously-default-OFF flags to default ON, each in its own commit (per
#: flip-protocol), the ONE bump riding the pack's FIRST commit:
#: FAULTLINE_HOME_PURE_CONTAINER_INHERIT (B74-C; keyed twenty — 'Sign in
#: and authenticate' 14m, filter 24,226→2,081, +240 rescue),
#: _SPA_ROUTE_TABLE (B74-A; twenty +114 PAGE / novu +86, 0/2,042 false),
#: _FLOW_REDERIVE_POSTGRAIN (B74-B; keyed twenty 894 live calls, +3,712
#: flows, golden tasks/workflows/AI taken), _DIGEST_STRATIFICATION
#: (S5b-H; keyed novu 'Sign in to existing account' mints, UF 88→92),
#: _UF_CASES_SPLIT (B75; sim-canon 209→10/131→11 children, composite
#: keyed twenty target), _RESIDUAL_CITABILITY (B77; replay census 502-class
#: 542→7, no-orphan 0/2211), _METRICS_RECOMPUTE (B76; impossible metric
#: rows →0 ×3 repos, hotspots byte-ident), and the S2-A return pair
#: _UF_DET_AGGREGATION + _SPRAY_GENERALIZED (the 04cf47f un-flip REVERSED:
#: the naming collapse is cured by R5 + spray-generalization — ledger
#: §S2-A-V3: twenty spray 17→0, settings-PF 36→22, conservation 328==328,
#: 0 false, I14 dangling 0). Default flips change what "unset" means, so
#: cached entries keyed under unset must not be served across them
#: (v16/v21/v25/v27/v28/v29/v30/v31/v32/v33 precedent). Every flag keeps
#: its X=0 kill-switch (explicit "0"/"false"/"off" still disables —
#: inverted-kill-switch unit per flag: unset ≡ explicit "1"
#: byte-identical). scan_meta.key_schema=34 emitted so downstream rulers
#: gate new-world logic.
KEY_SCHEMA_VERSION = 34

#: Directory / file-size guards for the non-git tree-hash fallback. Kept
#: scale-invariant (not tuned to any one repo) — they only bound work.
_NONGIT_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".next", "dist", "build", "target", ".turbo", "vendor",
    ".idea", ".vscode", "coverage", ".ruff_cache",
}
_NONGIT_MAX_FILE_BYTES = 2 * 1024 * 1024  # skip files larger than 2 MiB


# ── gate helpers ─────────────────────────────────────────────────────────


def _flag(env: str) -> bool:
    return os.environ.get(env, "0").strip() not in ("", "0")


def is_enabled() -> bool:
    """``True`` when the operator opted into the scan-result cache."""
    return _flag(ENV_ENABLE)


def is_bypassed() -> bool:
    """``True`` when a forced-fresh scan was requested (still stores)."""
    return _flag(ENV_BYPASS)


# ── engine version ───────────────────────────────────────────────────────


def _pyproject_version() -> str:
    """Version from the nearest ``pyproject.toml`` (walk up from this file).

    Reflects the *code* version (1.39.0) even when the installed dist
    metadata is stale. Empty string on any failure.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            try:
                data = tomllib.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                return ""
            proj = data.get("project")
            if isinstance(proj, dict):
                v = proj.get("version")
                if isinstance(v, str) and v:
                    return v
            return ""
    return ""


def engine_version() -> str:
    """Best-effort engine version for the cache key.

    Prefers pyproject (source-of-truth code version), then installed-dist
    metadata, then the module ``__version__``. Returns ``"0"`` only when
    everything fails — the key stays stable, it just loses version
    granularity (never crashes).
    """
    v = _pyproject_version()
    if v:
        return v
    try:
        import importlib.metadata as md

        for dist in ("dynvo", "faultlines", "faultline"):
            try:
                return md.version(dist)
            except md.PackageNotFoundError:
                continue
    except Exception:  # noqa: BLE001 — metadata is best-effort
        pass
    try:
        from faultline import __version__

        return __version__ or "0"
    except Exception:  # noqa: BLE001
        return "0"


# ── repo content identity ────────────────────────────────────────────────


def _git(repo_path: Path, *args: str, binary: bool = False) -> Any | None:
    """Run a git command scoped to ``repo_path``; ``None`` on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            check=True,
            timeout=30,
            text=not binary,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        logger.debug("scan_result_cache: git %s failed (%s)", args, exc)
        return None
    return out.stdout


def _is_git_repo(repo_path: Path) -> bool:
    res = _git(repo_path, "rev-parse", "--is-inside-work-tree")
    return isinstance(res, str) and res.strip() == "true"


def _dirty_hash(repo_path: Path) -> str:
    """Hash of uncommitted state; empty string for a clean tree.

    Combines ``git status --porcelain=v1`` (untracked + modified + staged
    status lines) with ``git diff HEAD`` (the actual content diff of tracked
    modifications, staged + unstaged). A clean checkout → both empty → ``""``.
    """
    # ``--untracked-files=all`` expands untracked DIRECTORIES into individual
    # file entries so each new file gets its own ``?? path`` line.
    status = _git(repo_path, "status", "--porcelain=v1", "--untracked-files=all")
    diff = _git(repo_path, "diff", "HEAD")
    status_s = status or ""
    diff_s = diff or ""
    # Untracked NEW files appear as "?? path" in status but contribute NOTHING to
    # ``git diff HEAD`` — so their CONTENT is invisible to the key unless hashed
    # explicitly (audit Bug 1): editing an untracked file would keep the key
    # stable and serve a stale result.
    untracked_s = _untracked_content_hash(repo_path, status_s)
    if not status_s.strip() and not diff_s.strip() and not untracked_s:
        return ""
    h = hashlib.sha256()
    h.update(b"status\0")
    h.update(status_s.encode("utf-8", "replace"))
    h.update(b"\0diff\0")
    h.update(diff_s.encode("utf-8", "replace"))
    h.update(b"\0untracked\0")
    h.update(untracked_s.encode("utf-8"))
    return h.hexdigest()


def _untracked_content_hash(repo_path: Path, status_s: str) -> str:
    """Content hash of untracked (``?? path``) files listed in ``status_s``.
    Deterministic (sorted), bounded (2 MiB/file; huge files hashed by size)."""
    paths: list[str] = []
    for line in status_s.splitlines():
        if line.startswith("?? "):
            rel = line[3:].strip()
            if rel.startswith('"') and rel.endswith('"'):  # git quotes special names
                rel = rel[1:-1]
            paths.append(rel)
    if not paths:
        return ""
    h = hashlib.sha256()
    for rel in sorted(paths):
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        fp = repo_path / rel
        try:
            if fp.is_file():
                size = fp.stat().st_size
                if size <= 2 * 1024 * 1024:
                    h.update(fp.read_bytes())
                else:
                    h.update(f"size:{size}".encode())
        except OSError:
            h.update(b"\0unreadable")
        h.update(b"\0")
    return h.hexdigest()


def _nongit_tree_hash(repo_path: Path) -> str:
    """Deterministic hash of a non-git tree's source files.

    Walks regular files (skipping heavy build/vendor dirs and oversized
    files), hashing sorted ``(relpath, sha256(content))`` pairs. Stable for
    an unchanged tree, distinct when any hashed file changes.
    """
    entries: list[tuple[str, str]] = []
    for path in sorted(repo_path.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(repo_path)
        if any(part in _NONGIT_SKIP_DIRS for part in rel.parts):
            continue
        try:
            if path.stat().st_size > _NONGIT_MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        entries.append((rel.as_posix(), hashlib.sha256(data).hexdigest()))
    h = hashlib.sha256()
    for rel, digest in entries:
        h.update(rel.encode("utf-8", "replace"))
        h.update(b"\0")
        h.update(digest.encode("ascii"))
        h.update(b"\0")
    return h.hexdigest()


def repo_content_identity(repo_path: Path | str) -> dict[str, str]:
    """Return a stable identity for the repo's current content.

    Git tree: ``{"vcs": "git", "head": <sha>, "dirty": <hash-or-empty>}`` —
    a clean checkout at commit X always maps to ``(X, "")``. Non-git tree:
    ``{"vcs": "none", "head": "", "dirty": <tree-hash>}``.
    """
    repo_path = Path(repo_path)
    if _is_git_repo(repo_path):
        head = _git(repo_path, "rev-parse", "HEAD")
        return {
            "vcs": "git",
            "head": (head or "").strip(),
            "dirty": _dirty_hash(repo_path),
        }
    return {"vcs": "none", "head": "", "dirty": _nongit_tree_hash(repo_path)}


# ── config signature + key ───────────────────────────────────────────────


def scan_config_signature(
    *,
    model: str,
    days: int,
    subpath: str | None,
    max_tree_depth: int | None,
    llm_reconcile: bool,
    feature_history: bool,
) -> dict[str, Any]:
    """Everything about the run configuration that changes scan output.

    ``model`` should be the RESOLVED model id (so two aliases for the same
    model share a cache entry). The Stage-6.7d abstraction env flags are
    read here — they materially change ``product_features`` / ``user_flows``.
    Deliberately EXCLUDES run-varying values (run_id, out_path, timestamps,
    org_id, thread identity, cost caps).
    """
    return {
        "model": model or "",
        "days": int(days),
        "subpath": subpath or "",
        "max_tree_depth": (
            int(max_tree_depth) if max_tree_depth is not None else None
        ),
        "llm_reconcile": bool(llm_reconcile),
        "feature_history": bool(feature_history),
        "stage_6_7d_abstraction": _flag(ENV_6_7D_ABSTRACTION),
        "stage_6_7d_abstraction_model": os.environ.get(
            ENV_6_7D_ABSTRACTION_MODEL, "",
        ).strip(),
        # All stage-gating env flags (raw values) — see ENV_OUTPUT_FLAGS.
        "stage_flags": {
            f: os.environ.get(f, "").strip() for f in ENV_OUTPUT_FLAGS
        },
    }


def compute_scan_cache_key(
    repo_path: Path | str,
    *,
    engine_version: str,
    config_signature: dict[str, Any],
) -> str:
    """sha256 over repo identity + engine version + config signature.

    Stable for identical inputs; changes when any tracked file, the config,
    or the engine version changes.
    """
    identity = repo_content_identity(repo_path)
    payload = {
        "key_schema": KEY_SCHEMA_VERSION,
        "identity": identity,
        "engine_version": engine_version,
        "config": config_signature,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── storage (raw, byte-exact) ────────────────────────────────────────────


def _scan_cache_path(key: str, *, base_dir: Path | None = None) -> Path:
    """Resolve ``<base>/scan-cache/<safe-key>.json`` (matches CacheKind)."""
    base = base_dir if base_dir is not None else faultline_base_dir()
    return Path(base) / "scan-cache" / f"{_safe_component(key)}.json"


def load_cached_scan(key: str, *, base_dir: Path | None = None) -> str | None:
    """Return the raw stored FeatureMap TEXT, or ``None`` on miss/fault.

    A missing file, an OS error, or an unparseable (corrupt / partial) body
    all count as a MISS — we validate the JSON parses before returning so a
    truncated entry is NEVER served. The raw text (not the parsed dict) is
    returned so the caller can reproduce byte-identical output.
    """
    path = _scan_cache_path(key, base_dir=base_dir)
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("scan_result_cache: read failed %s (%s) — miss", path, exc)
        return None
    try:
        json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "scan_result_cache: corrupt entry %s (%s) — treating as miss",
            path, exc,
        )
        return None
    return raw


def store_scan_result(
    key: str, featuremap_path: Path | str, *, base_dir: Path | None = None,
) -> bool:
    """Copy the written FeatureMap file verbatim into the cache.

    Reads the raw bytes of ``featuremap_path`` and writes them atomically
    (temp file + ``os.replace``) so a crashed write never leaves a partial
    entry. Returns ``True`` on success, ``False`` on any fault (never raises).
    """
    dst = _scan_cache_path(key, base_dir=base_dir)
    try:
        raw = Path(featuremap_path).read_bytes()
    except OSError as exc:
        logger.warning(
            "scan_result_cache: cannot read result %s (%s) — not cached",
            featuremap_path, exc,
        )
        return False
    tmp = dst.with_name(f"{dst.name}.tmp-{os.getpid()}")
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(raw)
        os.replace(tmp, dst)
    except OSError as exc:
        logger.warning("scan_result_cache: write failed %s (%s)", dst, exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


# ── HIT serving ──────────────────────────────────────────────────────────


def _default_out_path(repo_path: Path | str) -> Path:
    """Mirror ``output.writer.write_feature_map``'s default naming."""
    slug = re.sub(r"[^a-z0-9]+", "-", Path(repo_path).name.lower()).strip("-")
    slug = slug or "repo"
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    return faultline_base_dir() / f"feature-map-{slug}-{ts}.json"


def serve_from_cache(
    raw_text: str,
    *,
    key: str,
    repo_path: Path | str,
    out_path: Path | str | None,
) -> dict[str, Any] | None:
    """Write the cached bytes to the requested path and build the return dict.

    Returns the same ``{"path": ..., **scan_meta}`` shape ``run_pipeline_v2``
    yields, with a ``scan_cache`` marker flagging the HIT. Returns ``None`` on
    any fault so the orchestrator falls through to a normal scan.
    """
    try:
        data = json.loads(raw_text)
    except (json.JSONDecodeError, ValueError):
        return None
    target = Path(out_path).resolve() if out_path else _default_out_path(repo_path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Byte-exact replay of run A's file.
        target.write_text(raw_text, encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "scan_result_cache: could not write served result to %s (%s) "
            "— falling through to a normal scan", target, exc,
        )
        return None
    meta = dict(data.get("scan_meta") or {})
    meta["scan_cache"] = {
        "enabled": True,
        "served_from_cache": True,
        "stored": False,
        "key": key,
    }
    logger.info(
        "scan_result_cache: HIT — scan served from cache (key=%s) → %s ($0)",
        key[:12], target,
    )
    return {"path": str(target), **meta}


__all__ = [
    "ENV_ENABLE",
    "ENV_BYPASS",
    "KEY_SCHEMA_VERSION",
    "is_enabled",
    "is_bypassed",
    "engine_version",
    "repo_content_identity",
    "scan_config_signature",
    "compute_scan_cache_key",
    "load_cached_scan",
    "store_scan_result",
    "serve_from_cache",
]
