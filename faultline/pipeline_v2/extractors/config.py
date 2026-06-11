"""ConfigAsProductExtractor — product manifests where config = product.

Per the ``config-as-product-extractor`` skill, several platform
manifests declare user-facing surfaces directly:

  - Tauri              : ``tauri.conf.json`` — ``windows`` + ``app.cli.commands``
  - Expo               : ``app.json`` / ``app.config.{js,ts}`` — ``scheme`` + ``plugins``
  - VS Code extension  : ``package.json#contributes`` (commands, views, menus)
  - Chrome MV3         : ``manifest.json`` — ``permissions`` + ``action``
  - Raycast extension  : ``package.json#commands``
  - Slack app          : ``manifest.yaml`` (Slack-app declaration)
  - Atlassian Forge    : ``manifest.yml``

Each declared surface is an explicit product capability — no
inference required. The extractor emits one anchor per top-level
declaration, slug derived from the declared name / id.

We don't parse YAML in this first pass — Slack and Forge manifests
yield empty results unless we detect them by filename and the user
adds an entry to the config. The JSON-formatted manifests above are
fully supported.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from faultline.pipeline_v2.data import load_stack_yaml
from faultline.pipeline_v2.extractors._util import (
    posix,
    read_json,
    slugify,
)
from faultline.pipeline_v2.extractors.base import AnchorCandidate

if TYPE_CHECKING:
    from faultline.pipeline_v2.stage_0_intake import ScanContext


# ── manifest tables (file names + JSON key markers) ─────────────────────────
#
# Loaded from ``stacks/config-manifests.yaml`` in the packaged data tree
# (authoring copy: ``eval/stacks/config-manifests.yaml``). The YAML holds
# the DATA (file names, key markers, slugs/labels, confidence); the JSON
# parsing code stays here.


@dataclass(frozen=True)
class _Tables:
    """Manifest-detection data for every supported format."""

    confidence: float
    tauri_filenames: tuple[str, ...]
    tauri_parent_keys: tuple[str, ...]
    tauri_window_label_keys: tuple[str, ...]
    expo_filenames: tuple[str, ...]
    expo_detect_keys: tuple[str, ...]
    expo_plugin_prefix_trim: str
    vscode_filenames: tuple[str, ...]
    vscode_engines_key: str
    chrome_filenames: tuple[str, ...]
    chrome_manifest_version: int
    raycast_filenames: tuple[str, ...]
    raycast_required_keys: tuple[str, ...]


_TABLES_CACHE: _Tables | None = None


def _strs(block: dict, key: str) -> tuple[str, ...]:
    return tuple(s for s in (block.get(key) or []) if isinstance(s, str))


def _load_tables() -> _Tables:
    """Parse config-manifests.yaml once into the historical structures.

    Hermetic: resolves via ``importlib.resources`` (see
    ``faultline.pipeline_v2.data``).
    """
    global _TABLES_CACHE
    if _TABLES_CACHE is not None:
        return _TABLES_CACHE

    config = load_stack_yaml("config-manifests")
    tauri = config.get("tauri") or {}
    expo = config.get("expo") or {}
    vscode = config.get("vscode") or {}
    chrome = config.get("chrome_mv3") or {}
    raycast = config.get("raycast") or {}

    _TABLES_CACHE = _Tables(
        confidence=float(config.get("confidence") or 0.85),
        tauri_filenames=_strs(tauri, "filenames"),
        tauri_parent_keys=_strs(tauri, "parent_keys"),
        tauri_window_label_keys=_strs(tauri, "window_label_keys"),
        expo_filenames=_strs(expo, "filenames"),
        expo_detect_keys=_strs(expo, "detect_keys"),
        expo_plugin_prefix_trim=str(expo.get("plugin_prefix_trim") or ""),
        vscode_filenames=_strs(vscode, "filenames"),
        vscode_engines_key=str(vscode.get("engines_key") or "vscode"),
        chrome_filenames=_strs(chrome, "filenames"),
        chrome_manifest_version=int(chrome.get("manifest_version") or 3),
        raycast_filenames=_strs(raycast, "filenames"),
        raycast_required_keys=_strs(raycast, "required_keys"),
    )
    return _TABLES_CACHE


def _strip_namespace(qualified: str) -> str:
    """``myExt.openSettings`` → ``openSettings``. Used for VS Code /
    Raycast command ids that prepend an extension name.
    """
    return qualified.split(".")[-1] if "." in qualified else qualified


def _emit_vscode_extension_anchors(
    pkg: dict,
    file_path: str,
    t: _Tables,
) -> list[tuple[str, str, str]]:
    """Return list of ``(slug, file_path, rationale)`` for VS Code
    extensions whose ``contributes`` block declares user-facing surfaces.

    VS Code extensions are detected by the presence of an
    ``engines.vscode`` field — not just ``contributes``, since other
    package.json files happen to have a ``contributes`` block on some
    projects.
    """
    engines = pkg.get("engines")
    if not (isinstance(engines, dict) and t.vscode_engines_key in engines):
        return []
    contributes = pkg.get("contributes")
    if not isinstance(contributes, dict):
        return []
    emitted: list[tuple[str, str, str]] = []
    cmds = contributes.get("commands")
    if isinstance(cmds, list):
        for cmd in cmds:
            if not isinstance(cmd, dict):
                continue
            cmd_id = cmd.get("command")
            if isinstance(cmd_id, str) and cmd_id:
                slug = slugify(_strip_namespace(cmd_id))
                if slug:
                    emitted.append(
                        (slug, file_path,
                         f"vscode contributes.commands[{cmd_id!r}]"),
                    )
    # ``views`` is a dict of view-container-id → list of {id,name}
    views = contributes.get("views")
    if isinstance(views, dict):
        for view_list in views.values():
            if isinstance(view_list, list):
                for view in view_list:
                    if isinstance(view, dict):
                        vid = view.get("id") or view.get("name")
                        if isinstance(vid, str) and vid:
                            slug = slugify(_strip_namespace(vid))
                            if slug:
                                emitted.append(
                                    (slug, file_path,
                                     f"vscode contributes.views {vid!r}"),
                                )
    return emitted


def _emit_raycast_anchors(
    pkg: dict, file_path: str, t: _Tables,
) -> list[tuple[str, str, str]]:
    """Raycast extensions declare ``commands`` at top level of
    package.json with a ``preferences`` array and an ``author`` field.
    The convention is documented in raycast/api.
    """
    # Heuristic detection: a Raycast extension has an "author" field
    # AND a top-level "commands" list. VS Code extensions also have a
    # "commands" key but it's nested under contributes — so they won't
    # match this check.
    if any(key not in pkg for key in t.raycast_required_keys):
        return []
    cmds = pkg.get("commands")
    if not isinstance(cmds, list):
        return []
    emitted: list[tuple[str, str, str]] = []
    for cmd in cmds:
        if not isinstance(cmd, dict):
            continue
        cmd_name = cmd.get("name") or cmd.get("title")
        if isinstance(cmd_name, str) and cmd_name:
            slug = slugify(cmd_name)
            if slug:
                emitted.append(
                    (slug, file_path, f"raycast command {cmd_name!r}"),
                )
    return emitted


def _emit_tauri_anchors(
    conf: dict, file_path: str, t: _Tables,
) -> list[tuple[str, str, str]]:
    emitted: list[tuple[str, str, str]] = []
    # Tauri 2.x : top-level ``app.windows``. Tauri 1.x : ``tauri.windows``.
    for parent_key in t.tauri_parent_keys:
        parent = conf.get(parent_key)
        if not isinstance(parent, dict):
            continue
        windows = parent.get("windows")
        if isinstance(windows, list):
            for w in windows:
                if isinstance(w, dict):
                    label = None
                    for label_key in t.tauri_window_label_keys:
                        label = w.get(label_key)
                        if label:
                            break
                    if isinstance(label, str) and label:
                        slug = slugify(label)
                        if slug:
                            emitted.append(
                                (slug, file_path,
                                 f"tauri window {label!r}"),
                            )
        cli = parent.get("cli")
        if isinstance(cli, dict):
            subcommands = cli.get("subcommands")
            if isinstance(subcommands, dict):
                for cmd_name in subcommands.keys():
                    slug = slugify(str(cmd_name))
                    if slug:
                        emitted.append(
                            (slug, file_path,
                             f"tauri cli subcommand {cmd_name!r}"),
                        )
    return emitted


def _emit_expo_anchors(
    conf: dict, file_path: str, t: _Tables,
) -> list[tuple[str, str, str]]:
    emitted: list[tuple[str, str, str]] = []
    expo = conf.get("expo") if isinstance(conf.get("expo"), dict) else conf
    if not isinstance(expo, dict):
        return []
    plugins = expo.get("plugins")
    if isinstance(plugins, list):
        for plugin in plugins:
            name: str | None = None
            if isinstance(plugin, str):
                name = plugin
            elif isinstance(plugin, list) and plugin and isinstance(plugin[0], str):
                name = plugin[0]
            if name:
                # Expo plugins are often scoped like ``expo-notifications``.
                # Trim the ``expo-`` prefix when present so the slug is
                # the capability, not the platform.
                trim = t.expo_plugin_prefix_trim
                trimmed = (
                    name[len(trim):] if trim and name.startswith(trim)
                    else name
                )
                slug = slugify(trimmed)
                if slug:
                    emitted.append(
                        (slug, file_path, f"expo plugin {name!r}"),
                    )
    return emitted


def _emit_chrome_mv3_anchors(
    manifest: dict, file_path: str, t: _Tables,
) -> list[tuple[str, str, str]]:
    # Detect by manifest_version == 3 (most reliable signal).
    if manifest.get("manifest_version") != t.chrome_manifest_version:
        return []
    emitted: list[tuple[str, str, str]] = []
    action = manifest.get("action")
    if isinstance(action, dict):
        title = action.get("default_title") or action.get("default_popup")
        if isinstance(title, str) and title:
            # default_popup is usually ``popup.html`` — slug from that
            # is just ``popup``. Acceptable as a single anchor.
            slug = slugify(title.replace(".html", ""))
            if slug:
                emitted.append((slug, file_path, "chrome action"))
    permissions = manifest.get("permissions")
    if isinstance(permissions, list):
        for perm in permissions:
            if isinstance(perm, str):
                slug = slugify(perm)
                if slug:
                    emitted.append(
                        (slug, file_path, f"chrome permission {perm!r}"),
                    )
    return emitted


def _is_chrome_mv3(manifest: dict, t: _Tables) -> bool:
    return manifest.get("manifest_version") == t.chrome_manifest_version


def _is_tauri(name: str, t: _Tables) -> bool:
    return any(name.endswith(fn) for fn in t.tauri_filenames)


class ConfigAsProductExtractor:
    """Manifest declarations → product-surface anchors."""

    name = "config"

    def extract(self, ctx: "ScanContext") -> list[AnchorCandidate]:
        repo_path = ctx.repo_path
        files = list(ctx.tracked_files)
        t = _load_tables()

        # Group emissions per slug; preserve the union of source files.
        buckets: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: {"paths": set(), "why": set()},
        )

        def _accept(triples: list[tuple[str, str, str]]) -> None:
            for slug, fp, why in triples:
                buckets[slug]["paths"].add(fp)
                buckets[slug]["why"].add(why)

        for raw in files:
            p = posix(raw)
            basename = p.rsplit("/", 1)[-1]

            if _is_tauri(p, t) or basename in t.tauri_filenames:
                conf = read_json(repo_path / p)
                if isinstance(conf, dict):
                    _accept(_emit_tauri_anchors(conf, p, t))
                continue

            if basename in t.expo_filenames:
                conf = read_json(repo_path / p)
                if isinstance(conf, dict) and any(
                    key in conf for key in t.expo_detect_keys
                ):
                    _accept(_emit_expo_anchors(conf, p, t))
                continue

            if basename in t.chrome_filenames:
                manifest = read_json(repo_path / p)
                if isinstance(manifest, dict) and _is_chrome_mv3(manifest, t):
                    _accept(_emit_chrome_mv3_anchors(manifest, p, t))
                continue

            if basename in t.vscode_filenames or basename in t.raycast_filenames:
                pkg = read_json(repo_path / p)
                if isinstance(pkg, dict):
                    if basename in t.vscode_filenames:
                        _accept(_emit_vscode_extension_anchors(pkg, p, t))
                    if basename in t.raycast_filenames:
                        _accept(_emit_raycast_anchors(pkg, p, t))
                continue

        out: list[AnchorCandidate] = []
        for slug, data in buckets.items():
            paths = tuple(sorted(data["paths"]))
            rationale = "; ".join(sorted(data["why"]))
            out.append(
                AnchorCandidate(
                    name=slug,
                    paths=paths,
                    source=self.name,
                    # Config declarations are explicit author intent —
                    # high precision when present.
                    confidence_self=t.confidence,
                    rationale=rationale,
                ),
            )
        return out


__all__ = ["ConfigAsProductExtractor"]
