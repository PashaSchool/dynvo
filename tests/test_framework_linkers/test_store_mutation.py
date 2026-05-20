"""Tests for :mod:`faultline.framework_linkers.store_mutation` (Sprint C6)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from faultline.framework_linkers.store_mutation import (
    StoreMutationLinker,
    _detect_libraries,
    _detect_jotai_atoms,
    _detect_redux_slice,
    _detect_zustand_store,
)
from faultline.models.types import Feature
from faultline.pipeline_v2.run_logger import StageLogger


# ── Helpers ────────────────────────────────────────────────────────────────


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _ws(name: str, path: str, deps: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        path=path,
        package_json={"name": name, "dependencies": deps},
        stack=None,
        files=[],
    )


def _ctx(
    repo: Path,
    *,
    workspaces: list[SimpleNamespace] | None = None,
    stack: str = "next-app-router",
    audited: str | None = "next-app-router",
) -> SimpleNamespace:
    tracked: list[str] = []
    for f in repo.rglob("*"):
        if f.is_file() and "/.git" not in str(f):
            try:
                tracked.append(f.relative_to(repo).as_posix())
            except ValueError:
                continue
    return SimpleNamespace(
        repo_path=repo,
        tracked_files=tuple(tracked),
        run_dir=None,
        stack=stack,
        audited_stack=audited,
        secondary_stacks=(),
        monorepo=bool(workspaces),
        workspaces=workspaces or [],
    )


def _new_feature(name: str, paths: list[str]) -> Feature:
    return Feature(
        name=name, paths=list(paths), authors=[], total_commits=0,
        bug_fixes=0, bug_fix_ratio=0.0,
        last_modified=datetime.now(timezone.utc),
        health_score=80.0, layer="developer",
    )


def _log(tmp_path: Path) -> StageLogger:
    return StageLogger(tmp_path, 6, "store_mutation_test")


# ── Library detection ──────────────────────────────────────────────────────


def test_detect_zustand_via_workspace_deps(tmp_path: Path) -> None:
    ws = _ws("web", "apps/web", {"zustand": "^4.5.0"})
    ctx = _ctx(tmp_path, workspaces=[ws])
    assert "zustand" in _detect_libraries(ctx)


def test_detect_redux_toolkit(tmp_path: Path) -> None:
    ws = _ws("web", "apps/web", {"@reduxjs/toolkit": "^2.0.0", "react-redux": "^9.0.0"})
    ctx = _ctx(tmp_path, workspaces=[ws])
    libs = _detect_libraries(ctx)
    assert "redux" in libs


def test_detect_root_package_json(tmp_path: Path) -> None:
    _w(tmp_path / "package.json", json.dumps(
        {"dependencies": {"jotai": "2.0.0"}}
    ))
    ctx = _ctx(tmp_path, workspaces=[])
    assert "jotai" in _detect_libraries(ctx)


def test_no_store_lib_means_empty_set(tmp_path: Path) -> None:
    _w(tmp_path / "package.json", json.dumps({"dependencies": {"vue": "3.4"}}))
    ctx = _ctx(tmp_path, workspaces=[])
    assert _detect_libraries(ctx) == set()


# ── Activation ─────────────────────────────────────────────────────────────


def test_inactive_on_plain_react_repo(tmp_path: Path) -> None:
    _w(tmp_path / "package.json", json.dumps({"dependencies": {"react": "18"}}))
    ctx = _ctx(tmp_path, workspaces=[])
    linker = StoreMutationLinker()
    assert linker.is_active(ctx) is False


def test_inactive_on_vue_repo(tmp_path: Path) -> None:
    _w(tmp_path / "package.json", json.dumps({"dependencies": {"vue": "3.4"}}))
    ctx = _ctx(tmp_path, workspaces=[], stack="vue-spa", audited="vue-spa")
    assert StoreMutationLinker().is_active(ctx) is False


def test_active_per_workspace(tmp_path: Path) -> None:
    """Only one workspace has Zustand — linker still active globally."""
    ws_web = _ws("web", "apps/web", {"zustand": "4.5"})
    ws_api = _ws("api", "apps/api", {"express": "5.0"})
    ctx = _ctx(tmp_path, workspaces=[ws_web, ws_api])
    linker = StoreMutationLinker()
    assert linker.is_active(ctx) is True
    assert "zustand" in linker.telemetry.libraries_detected


def test_active_when_multiple_libraries_coexist(tmp_path: Path) -> None:
    ws = _ws("web", "apps/web", {"zustand": "4.5", "jotai": "2.0"})
    ctx = _ctx(tmp_path, workspaces=[ws])
    linker = StoreMutationLinker()
    assert linker.is_active(ctx) is True
    assert "zustand" in linker.telemetry.libraries_detected
    assert "jotai" in linker.telemetry.libraries_detected


# ── Zustand registry ───────────────────────────────────────────────────────


def test_zustand_store_definition_parsed() -> None:
    text = (
        'import { create } from "zustand"\n'
        'export const useUserStore = create<UserState>((set, get) => ({\n'
        '  role: null,\n'
        '  setRole: (role) => set({ role }),\n'
        '  reset: () => set({ role: null }),\n'
        '}))\n'
    )
    mutators, fields = _detect_zustand_store("stores/user.ts", text)
    mut_names = {m.mutator for m in mutators}
    assert "setRole" in mut_names
    assert "reset" in mut_names
    assert any(f.store_name == "useUserStore" for f in fields)


def test_zustand_set_imported_from_lodash_ignored() -> None:
    """File with `set` imported from lodash but no `create()` is not a store."""
    text = (
        'import set from "lodash/set"\n'
        'export function helper(obj, path, value) { return set(obj, path, value) }\n'
    )
    mutators, fields = _detect_zustand_store("utils/helpers.ts", text)
    assert mutators == []
    assert fields == []


# ── Redux Toolkit registry ─────────────────────────────────────────────────


def test_redux_slice_parsed() -> None:
    text = (
        'import { createSlice } from "@reduxjs/toolkit"\n'
        'const userSlice = createSlice({\n'
        '  name: "user",\n'
        '  initialState: { role: null },\n'
        '  reducers: {\n'
        '    setRole: (state, action) => { state.role = action.payload },\n'
        '    clear: (state) => { state.role = null },\n'
        '  },\n'
        '})\n'
        'export const { setRole, clear } = userSlice.actions\n'
    )
    mutators, _ = _detect_redux_slice("store/userSlice.ts", text)
    names = {m.mutator for m in mutators}
    assert "setRole" in names
    assert "clear" in names
    # Skip framework keys.
    assert "initialState" not in names
    assert "reducers" not in names


# ── Jotai registry ─────────────────────────────────────────────────────────


def test_jotai_writable_atom_parsed() -> None:
    text = (
        'import { atom } from "jotai"\n'
        'export const userAtom = atom({ role: null })\n'
        'export const setRoleAtom = atom(null, (get, set, role) => {\n'
        '  set(userAtom, { ...get(userAtom), role })\n'
        '})\n'
    )
    mutators, fields = _detect_jotai_atoms("stores/user.ts", text)
    mut_names = {m.mutator for m in mutators}
    assert "setRoleAtom" in mut_names
    field_names = {f.store_name for f in fields}
    assert "userAtom" in field_names


# ── End-to-end link emission — Zustand ─────────────────────────────────────


def test_zustand_link_for_mutator_call(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "stores/user.ts",
       'import { create } from "zustand"\n'
       'export const useUserStore = create((set) => ({\n'
       '  role: null,\n'
       '  setRole: (role) => set({ role }),\n'
       '}))\n')
    _w(repo / "components/UserMenu.tsx",
       'import { useUserStore } from "../stores/user"\n'
       'export function UserMenu() {\n'
       '  const setRole = useUserStore(s => s.setRole)\n'
       '  return <button onClick={() => setRole("admin")}>X</button>\n'
       '}\n')
    _w(repo / "package.json", json.dumps({"dependencies": {"zustand": "4.5"}}))

    ctx = _ctx(repo, workspaces=[])
    feature = _new_feature("user", [
        "stores/user.ts",
        "components/UserMenu.tsx",
    ])
    linker = StoreMutationLinker()
    assert linker.is_active(ctx) is True
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))

    kinds = {link.link_kind for link in links}
    assert "store-mutation" in kinds
    assert "store-read" in kinds

    mut_link = next(l for l in links if l.link_kind == "store-mutation")
    assert mut_link.target_file == "stores/user.ts"
    assert mut_link.target_symbol == "setRole"
    # Mutation call site lives in the consumer file.
    assert mut_link.source_file == "components/UserMenu.tsx"

    read_link = next(l for l in links if l.link_kind == "store-read")
    assert read_link.target_file == "stores/user.ts"


# ── End-to-end — Redux ─────────────────────────────────────────────────────


def test_redux_dispatch_call_links_to_reducer(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "store/userSlice.ts",
       'import { createSlice } from "@reduxjs/toolkit"\n'
       'const userSlice = createSlice({\n'
       '  name: "user",\n'
       '  initialState: {role: null},\n'
       '  reducers: {\n'
       '    setRole: (state, action) => { state.role = action.payload },\n'
       '  },\n'
       '})\n'
       'export const { setRole } = userSlice.actions\n'
       'export default userSlice.reducer\n')
    _w(repo / "components/Profile.tsx",
       'import { useDispatch } from "react-redux"\n'
       'import { setRole } from "../store/userSlice"\n'
       'export function Profile() {\n'
       '  const dispatch = useDispatch()\n'
       '  return <button onClick={() => dispatch(setRole("admin"))}>X</button>\n'
       '}\n')
    _w(repo / "package.json", json.dumps({"dependencies": {"@reduxjs/toolkit": "2.0", "react-redux": "9.0"}}))

    ctx = _ctx(repo, workspaces=[])
    feature = _new_feature("user", [
        "store/userSlice.ts", "components/Profile.tsx",
    ])
    linker = StoreMutationLinker()
    assert linker.is_active(ctx) is True
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))

    mut_links = [l for l in links if l.link_kind == "store-mutation"]
    assert mut_links, "expected at least one dispatch->reducer link"
    assert any(
        l.target_symbol == "setRole" and l.source_file == "components/Profile.tsx"
        for l in mut_links
    )


# ── End-to-end — Jotai mixed with Zustand ──────────────────────────────────


def test_multiple_libraries_both_emit_links(tmp_path: Path) -> None:
    repo = tmp_path
    _w(repo / "stores/zust.ts",
       'import { create } from "zustand"\n'
       'export const useCounter = create((set) => ({\n'
       '  count: 0,\n'
       '  inc: () => set((s) => ({ count: s.count + 1 })),\n'
       '}))\n')
    _w(repo / "stores/jot.ts",
       'import { atom } from "jotai"\n'
       'export const themeAtom = atom("light")\n')
    _w(repo / "components/App.tsx",
       'import { useCounter } from "../stores/zust"\n'
       'import { useAtomValue } from "jotai"\n'
       'import { themeAtom } from "../stores/jot"\n'
       'export function App() {\n'
       '  const inc = useCounter(s => s.inc)\n'
       '  const theme = useAtomValue(themeAtom)\n'
       '  return <button onClick={() => inc()}>{theme}</button>\n'
       '}\n')
    _w(repo / "package.json", json.dumps({"dependencies": {"zustand": "4.5", "jotai": "2.0"}}))

    ctx = _ctx(repo, workspaces=[])
    feature = _new_feature("app", [
        "stores/zust.ts", "stores/jot.ts", "components/App.tsx",
    ])
    linker = StoreMutationLinker()
    assert linker.is_active(ctx) is True
    links = linker.link_for_feature(feature, ctx, _log(tmp_path))

    # Both libraries should contribute at least one link.
    libs_in_reasons = {
        l.reason.split(" ", 1)[0] for l in links if l.reason
    }
    assert "zustand" in libs_in_reasons
    assert "jotai" in libs_in_reasons
