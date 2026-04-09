"use client";

import React, { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

// ── Feature row type ─────────────────────────────────────────────
type Feature = {
  name: string;
  sub: string;
  health: number;
  cov: number | null;  // test coverage % (0-100), null = not measured
  ratio: number;
  commits: number;
  files: number;
  impact: "critical" | "high" | "medium" | "low" | "healthy";
  flows?: { name: string; health: number; cov: number | null; ratio: number; commits: number }[];
};

type RepoShowcase = {
  id: string;
  title: string;         // terminal header title, e.g. "~/cal.com"
  repoUrl: string;
  shortName: string;     // label shown in tab bar, e.g. "cal.com"
  langLabel: string;     // e.g. "TS monorepo · pnpm · 20 packages"
  detected: string;      // first bullet line after `$` command
  fileCount: string;     // "10,142 files"
  commitCount: string;   // "84,391 commits"
  featureCount: number;  // used in footer line
  flowCount: number;
  elapsed: string;       // "23m 34s"
  cost: string;          // "$2.14"
  outputFile: string;    // path shown in "Wrote..." line
  topNote: string;       // "top 6 of 282 shown" or "top 6 of 16 shown"
  features: Feature[];
};

// ── Helpers ──────────────────────────────────────────────────────
// Impact/color bands calibrated against real OSS health scores.
// With the revised sigmoid formula, a 50% bug-fix ratio → health ≈35.
// Healthy active features land at 40-70, only truly maintenance-
// dominant code (<25 health) gets danger red.
const colorFor = (health: number): string => {
  if (health < 25) return "var(--danger)";
  if (health < 45) return "var(--warning)";
  return "var(--success)";
};
const impactFor = (health: number): Feature["impact"] => {
  if (health < 15) return "critical";
  if (health < 25) return "high";
  if (health < 45) return "medium";
  if (health < 70) return "low";
  return "healthy";
};
// Coverage color: green if high, orange if moderate, red if low, dim if null
const covColorFor = (cov: number | null | undefined): string => {
  if (cov == null) return "var(--fg-muted)";
  if (cov >= 70) return "var(--success)";
  if (cov >= 40) return "var(--warning)";
  return "var(--danger)";
};
const covLabel = (cov: number | null | undefined): string =>
  cov == null ? "—" : `${cov}%`;
// Full word "tests" in header + percentage value in cells makes the
// column read as "tests: 72%" which is immediately clear even to
// someone who's never seen a coverage report.

const feat = (
  name: string,
  sub: string,
  health: number,
  ratio: number,
  commits: number,
  files: number,
  flows?: Feature["flows"],
  cov: number | null = null,
): Feature => ({
  name,
  sub,
  health: Math.round(health),
  cov,
  ratio: Math.round(ratio),
  commits,
  files,
  impact: impactFor(health),
  flows,
});

// ── AUTO-GENERATED from real scan JSONs — all data below is from
//    actual faultlines analyze runs, not hand-crafted. Features, flows,
//    health scores, bug-fix ratios, commits, and file counts are pulled
//    directly from tests/baseline/ and /tmp/ feature-map JSONs. Coverage
//    is null on all repos (no lcov/jest coverage data in any clone).
//    Health uses sigmoid formula: 100 / (1 + exp(8 * (ratio - 0.55))).
const REPOS: RepoShowcase[] = [
  {
    id: "calcom",
    title: "~/cal.com",
    repoUrl: "https://github.com/calcom/cal.com",
    shortName: "cal.com",
    langLabel: "TS monorepo · pnpm workspace · 20 packages",
    detected: "monorepo (pnpm workspace, 20 packages)",
    fileCount: "10,142 files",
    commitCount: "84,391 commits",
    featureCount: 282,
    flowCount: 725,
    elapsed: "23m 34s",
    cost: "$2.14",
    outputFile: "~/.faultlines/feature-map-calcom.json",
    topNote: "top 6 of 282 shown",
    features: [
      feat("web/settings", "apps/web/components", 23, 70, 206, 178, [
        { name: "manage-organization-roles-flow", health: 11, cov: null, ratio: 81, commits: 37 },
        { name: "manage-oauth-clients-flow", health: 15, cov: null, ratio: 77, commits: 13 },
        { name: "manage-out-of-office-flow", health: 21, cov: null, ratio: 71, commits: 14 },
      ], null),
      feat("trpc/viewer", "packages/trpc/server", 26, 68, 539, 728, undefined, null),
      feat("web/bookings", "apps/web/modules", 16, 76, 172, 109, undefined, null),
      feat("prisma", "packages/prisma/selects", 47, 56, 220, 21, undefined, null),
      feat("web/dashboard", "apps/web/pages", 27, 67, 175, 128, undefined, null),
      feat("lib/server", "packages/lib/server", 34, 63, 177, 48, undefined, null),
    ],
  },
  {
    id: "plane",
    title: "~/plane",
    repoUrl: "https://github.com/makeplane/plane",
    shortName: "plane",
    langLabel: "TS monorepo · open-source Jira alternative",
    detected: "monorepo (pnpm workspace, 12 packages)",
    fileCount: "4,932 files",
    commitCount: "6,214 commits",
    featureCount: 134,
    flowCount: 408,
    elapsed: "11m 42s",
    cost: "$0.68",
    outputFile: "~/.faultlines/feature-map-plane.json",
    topNote: "top 6 of 134 shown",
    features: [
      feat("web/issues", "apps/web/ce", 37, 62, 172, 447, [
        { name: "view-issues-in-layout-flow", health: 56, cov: null, ratio: 52, commits: 54 },
        { name: "filter-and-sort-issues-flow", health: 72, cov: null, ratio: 43, commits: 21 },
        { name: "delete-issue-flow", health: 53, cov: null, ratio: 54, commits: 43 },
      ], null),
      feat("editor/editor-extensions", "packages/editor/src", 20, 72, 104, 111, undefined, null),
      feat("editor/shared-ui", "packages/editor/src", 20, 72, 86, 42, undefined, null),
      feat("web/workspace", "apps/web/ce", 52, 54, 106, 100, undefined, null),
      feat("web/project", "apps/web/core", 45, 57, 87, 88, undefined, null),
      feat("web/pages", "apps/web/ce", 37, 62, 76, 99, undefined, null),
    ],
  },
  {
    id: "documenso",
    title: "~/documenso",
    repoUrl: "https://github.com/documenso/documenso",
    shortName: "documenso",
    langLabel: "TS monorepo · pnpm · open-source DocuSign",
    detected: "monorepo (pnpm workspace, 8 packages)",
    fileCount: "2,530 files",
    commitCount: "5,102 commits",
    featureCount: 49,
    flowCount: 191,
    elapsed: "8m 24s",
    cost: "$0.47",
    outputFile: "~/.faultlines/feature-map-documenso.json",
    topNote: "top 6 of 49 shown",
    features: [
      feat("lib/server-utils", "packages/lib/server-only", 55, 53, 152, 250, [
        { name: "send-document-flow", health: 60, cov: null, ratio: 50, commits: 42 },
        { name: "create-organisation-flow", health: 70, cov: null, ratio: 44, commits: 9 },
        { name: "invite-organisation-members-flow", health: 99, cov: null, ratio: 0, commits: 2 },
      ], null),
      feat("remix/shared-components", "apps/remix/app", 54, 53, 106, 105, undefined, null),
      feat("remix/dashboard", "apps/remix/app", 72, 43, 83, 84, undefined, null),
      feat("ui/document", "packages/ui/components", 56, 52, 67, 51, undefined, null),
      feat("lib/jobs", "packages/lib/jobs", 46, 57, 44, 49, undefined, null),
      feat("remix/envelope-editor", "apps/remix/server", 53, 53, 45, 19, undefined, null),
    ],
  },
  {
    id: "outline",
    title: "~/outline",
    repoUrl: "https://github.com/outline/outline",
    shortName: "outline",
    langLabel: "TS · team knowledge base & wiki",
    detected: "application (TypeScript + Koa backend)",
    fileCount: "2,390 files",
    commitCount: "13,488 commits",
    featureCount: 22,
    flowCount: 188,
    elapsed: "6m 18s",
    cost: "$0.31",
    outputFile: "~/.faultlines/feature-map-outline.json",
    topNote: "top 6 of 22 shown",
    features: [
      feat("rich-text-editor", "shared/components/EmojiText.tsx", 29, 66, 438, 249, [
        { name: "insert-table-flow", health: 48, cov: null, ratio: 56, commits: 41 },
        { name: "render-editor-flow", health: 29, cov: null, ratio: 66, commits: 56 },
        { name: "structure-content-flow", health: 85, cov: null, ratio: 33, commits: 3 },
      ], null),
      feat("api-backend", "server/collaboration/APIUpdateExtension.ts", 44, 58, 375, 275, undefined, null),
      feat("dashboard", "server/routes/app.ts", 33, 64, 318, 256, undefined, null),
      feat("document-editor", "app/components/DocumentBreadcrumb.tsx", 23, 70, 212, 64, undefined, null),
      feat("document-management", "app/components/DocumentExplorer", 38, 61, 218, 74, undefined, null),
      feat("plugins", "plugins/azure/plugin.json", 49, 55, 101, 95, undefined, null),
    ],
  },
  {
    id: "formbricks",
    title: "~/formbricks",
    repoUrl: "https://github.com/formbricks/formbricks",
    shortName: "formbricks",
    langLabel: "TS monorepo · open-source form builder",
    detected: "monorepo (pnpm workspace, 6 packages)",
    fileCount: "3,316 files",
    commitCount: "4,127 commits",
    featureCount: 33,
    flowCount: 136,
    elapsed: "7m 32s",
    cost: "$0.38",
    outputFile: "~/.faultlines/feature-map-formbricks.json",
    topNote: "top 6 of 33 shown",
    features: [
      feat("web/survey", "apps/web/modules", 54, 53, 289, 249, [
        { name: "create-survey-from-template-flow", health: 65, cov: null, ratio: 47, commits: 38 },
        { name: "take-survey-via-link-flow", health: 79, cov: null, ratio: 38, commits: 52 },
        { name: "take-contact-survey-flow", health: 87, cov: null, ratio: 31, commits: 13 },
      ], null),
      feat("web/shared-ui", "apps/web/modules", 70, 44, 331, 428, undefined, null),
      feat("web/ee", "apps/web/modules", 72, 43, 166, 181, undefined, null),
      feat("surveys/general", "packages/surveys/src", 36, 62, 98, 45, undefined, null),
      feat("web/organization", "apps/web/modules", 65, 47, 114, 94, undefined, null),
      feat("web/response-badges", "apps/web/modules", 73, 42, 111, 44, undefined, null),
    ],
  },
  {
    id: "excalidraw",
    title: "~/excalidraw",
    repoUrl: "https://github.com/excalidraw/excalidraw",
    shortName: "excalidraw",
    langLabel: "TS monorepo · virtual whiteboard · canvas-based",
    detected: "monorepo (pnpm workspace, 6 packages)",
    fileCount: "1,225 files",
    commitCount: "3,842 commits",
    featureCount: 15,
    flowCount: 28,
    elapsed: "3m 51s",
    cost: "$0.14",
    outputFile: "~/.faultlines/feature-map-excalidraw.json",
    topNote: "top 6 of 15 shown",
    features: [
      feat("excalidraw/shared-ui", "packages/excalidraw/components", 54, 53, 64, 132, [
        { name: "generate-diagram-flow", health: 50, cov: null, ratio: 55, commits: 20 },
        { name: "use-dropdown-menu-flow", health: 67, cov: null, ratio: 46, commits: 13 },
        { name: "edit-element-stats-flow", health: 60, cov: null, ratio: 50, commits: 12 },
      ], null),
      feat("common", "packages/common/debug.ts", 49, 56, 54, 20, undefined, null),
      feat("excalidraw/data", "packages/excalidraw/data", 35, 62, 40, 22, undefined, null),
      feat("excalidraw/renderer", "packages/excalidraw/renderer", 37, 62, 34, 16, undefined, null),
      feat("utils", "packages/utils/CHANGELOG.md", 60, 50, 24, 17, undefined, null),
      feat("math", "packages/math/README.md", 64, 48, 21, 26, undefined, null),
    ],
  },
  {
    id: "ghost",
    title: "~/ghost",
    repoUrl: "https://github.com/TryGhost/Ghost",
    shortName: "ghost",
    langLabel: "TS/JS monorepo · blogging & newsletter platform",
    detected: "monorepo (yarn workspace, 11 apps)",
    fileCount: "6,898 files",
    commitCount: "22,914 commits",
    featureCount: 101,
    flowCount: 281,
    elapsed: "14m 18s",
    cost: "$0.92",
    outputFile: "~/.faultlines/feature-map-ghost.json",
    topNote: "top 6 of 101 shown",
    features: [
      feat("admin-x-framework", "apps/admin-x-framework/.eslintrc.cjs", 13, 78, 203, 77, [
        { name: "manage-site-settings-flow", health: 15, cov: null, ratio: 77, commits: 13 },
        { name: "manage-staff-users-flow", health: 3, cov: null, ratio: 100, commits: 2 },
        { name: "manage-content-flow", health: 17, cov: null, ratio: 75, commits: 20 },
      ], null),
      feat("ghost/members", "ghost/core/core", 6, 90, 173, 112, undefined, null),
      feat("stats/stats", "apps/stats/src", 11, 82, 174, 44, undefined, null),
      feat("ghost/email", "ghost/core/core", 9, 83, 144, 62, undefined, null),
      feat("posts/post-analytics", "apps/posts/src", 9, 84, 139, 25, undefined, null),
      feat("shade/ui", "apps/shade/src", 12, 80, 143, 124, undefined, null),
    ],
  },
  {
    id: "trpc",
    title: "~/trpc",
    repoUrl: "https://github.com/trpc/trpc",
    shortName: "trpc",
    langLabel: "TS monorepo · end-to-end typesafe APIs",
    detected: "monorepo (pnpm workspace, 9 packages)",
    fileCount: "1,573 files",
    commitCount: "3,421 commits",
    featureCount: 14,
    flowCount: 37,
    elapsed: "1m 17s",
    cost: "$0.11",
    outputFile: "~/.faultlines/feature-map-trpc.json",
    topNote: "top 6 of 14 shown",
    features: [
      feat("www/documentation", "www/docs/.gitignore", 29, 66, 59, 91, [
        { name: "learn-trpc-basics-flow", health: 60, cov: null, ratio: 50, commits: 4 },
        { name: "setup-server-flow", health: 60, cov: null, ratio: 50, commits: 12 },
        { name: "setup-client-flow", health: 46, cov: null, ratio: 57, commits: 14 },
      ], null),
      feat("server/unstable-core-do-not-import", "packages/server/src", 13, 79, 42, 44, undefined, null),
      feat("next", "packages/next/README.md", 88, 31, 62, 23, undefined, null),
      feat("client/links", "packages/client/src", 14, 78, 18, 24, undefined, null),
      feat("openapi", "packages/openapi/tsdown.config.ts", 56, 52, 25, 7, undefined, null),
      feat("server/adapters", "packages/server/src", 20, 73, 11, 23, undefined, null),
    ],
  },
  {
    id: "axios",
    title: "~/axios",
    repoUrl: "https://github.com/axios/axios",
    shortName: "axios",
    langLabel: "JS · HTTP client library",
    detected: "library (package.json with main/exports)",
    fileCount: "329 files",
    commitCount: "2,814 commits",
    featureCount: 57,
    flowCount: 0,
    elapsed: "22s",
    cost: "$0.03",
    outputFile: "~/.faultlines/feature-map-axios.json",
    topNote: "top 6 of 57 shown · no flows",
    features: [
      feat("adapters", "lib/adapters/adapters.js", 9, 84, 25, 4, undefined, null),
      feat("index.d", "index.d.cts", 18, 74, 19, 2, undefined, null),
      feat("axios", "lib/axios.js", 23, 70, 10, 2, undefined, null),
      feat("axiosheaders", "lib/core/AxiosHeaders.js", 3, 100, 6, 1, undefined, null),
      feat("axioserror", "lib/core/AxiosError.js", 3, 100, 5, 1, undefined, null),
      feat("defaults", "lib/defaults/index.js", 12, 80, 5, 2, undefined, null),
    ],
  },
  {
    id: "gin",
    title: "~/gin",
    repoUrl: "https://github.com/gin-gonic/gin",
    shortName: "gin",
    langLabel: "Go · web framework · flat layout",
    detected: "library (go.mod, no cmd/ dir)",
    fileCount: "130 files",
    commitCount: "3,987 commits",
    featureCount: 22,
    flowCount: 0,
    elapsed: "15s",
    cost: "$0.01",
    outputFile: "~/.faultlines/feature-map-gin.json",
    topNote: "top 6 of 22 shown · no flows",
    features: [
      feat("context", "context.go", 83, 35, 23, 2, undefined, null),
      feat("binding", "binding/binding.go", 87, 31, 16, 17, undefined, null),
      feat("gin", "gin.go", 80, 38, 8, 1, undefined, null),
      feat("logger", "logger.go", 17, 75, 4, 1, undefined, null),
      feat("recovery", "recovery.go", 89, 29, 7, 1, undefined, null),
      feat("tree", "tree.go", 89, 29, 7, 1, undefined, null),
    ],
  },
];


// Auto-advance interval in ms. Paused on hover.
const SLIDE_MS = 6500;

export default function ScanCarousel() {
  const [active, setActive] = useState(0);
  const [paused, setPaused] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Auto-advance unless paused or user-interacted-recently.
  useEffect(() => {
    if (paused) return;
    timerRef.current = setInterval(() => {
      setActive((prev) => (prev + 1) % REPOS.length);
    }, SLIDE_MS);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [paused]);

  // Click a tab / dot → jump AND pause auto-advance for a while so
  // the user can actually read the slide they clicked on.
  const jumpTo = (idx: number) => {
    setActive(idx);
    setPaused(true);
    // Un-pause after a longer window (15s) so the carousel resumes
    // drifting on its own if the user walks away.
    window.setTimeout(() => setPaused(false), 15000);
  };
  const prev = () => jumpTo((active - 1 + REPOS.length) % REPOS.length);
  const next = () => jumpTo((active + 1) % REPOS.length);

  const repo = REPOS[active];

  return (
    <div
      className="lp-carousel"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
    >
      {/* ── Tab bar: one click target per repo ── */}
      <div className="lp-carousel-tabs" role="tablist" aria-label="Select benchmark repo">
        {REPOS.map((r, i) => (
          <button
            key={r.id}
            type="button"
            role="tab"
            aria-selected={i === active}
            className={`lp-carousel-tab ${i === active ? "is-active" : ""}`}
            onClick={() => jumpTo(i)}
          >
            {r.shortName}
          </button>
        ))}
      </div>

      {/* ── Terminal window ── */}
      <div className="lp-showcase">
        <div className="lp-showcase-head">
          <div className="lp-showcase-dots">
            <span className="lp-showcase-dot" style={{ background: "#ff5f57" }} />
            <span className="lp-showcase-dot" style={{ background: "#febc2e" }} />
            <span className="lp-showcase-dot" style={{ background: "#28c840" }} />
          </div>
          <div className="lp-showcase-title mono">
            {repo.title} — faultlines analyze
          </div>
          <div className="lp-showcase-badge ok">
            <span className="pulse" />
            Live
          </div>
        </div>

        <div className="lp-showcase-body mono" key={repo.id}>
          <div className="lp-sc-line">
            <span className="lp-sc-prompt">$</span>
            <span className="lp-sc-cmd">faultlines analyze . --llm --flows</span>
          </div>
          <div className="lp-sc-out">
            <span className="dim">→</span> Detected{" "}
            <span className="hi">{repo.detected.split(" ")[0]}</span>{" "}
            <span className="dim">
              ({repo.detected.split(" ").slice(1).join(" ")})
            </span>
          </div>
          <div className="lp-sc-out">
            <span className="dim">→</span> Reading git blame{" "}
            <span className="dim">
              {repo.fileCount} · {repo.commitCount}
            </span>
          </div>
          <div className="lp-sc-out">
            <span className="dim">→</span> Clustering with Claude Sonnet 4.6{" "}
            <span className="ok">✓</span>
          </div>
          <div className="lp-sc-out">
            <span className="dim">→</span> Scoring features{" "}
            <span className="ok">✓</span>
          </div>

          <div className="lp-sc-spacer" />

          <div className="lp-sc-feature-row">
            <span className="f-icon">●</span>
            <span
              className="f-name"
              style={{
                color: "var(--fg-muted)",
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: 0.6,
              }}
            >
              feature
            </span>
            <span
              className="f-health"
              style={{
                color: "var(--fg-muted)",
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: 0.6,
              }}
            >
              health
            </span>
            <span
              className="f-cov"
              style={{
                color: "var(--fg-muted)",
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: 0.6,
              }}
            >
              tests
            </span>
            <span
              className="f-ratio"
              style={{
                color: "var(--fg-muted)",
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: 0.6,
              }}
            >
              bug%
            </span>
            <span
              className="f-commits"
              style={{
                color: "var(--fg-muted)",
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: 0.6,
              }}
            >
              commits
            </span>
            <span className="f-impact" style={{ color: "var(--fg-muted)" }}>
              impact
            </span>
          </div>

          {repo.features.map((f) => {
            const color = colorFor(f.health);
            return (
              <React.Fragment key={f.name}>
                <div className="lp-sc-feature-row">
                  <span className="f-icon" style={{ color }}>
                    ●
                  </span>
                  <span className="f-name">
                    {f.name}
                    <span className="dim">{f.sub}</span>
                  </span>
                  <span className="f-health" style={{ color }}>
                    {f.health}
                  </span>
                  <span className="f-cov" style={{ color: covColorFor(f.cov) }}>
                    {covLabel(f.cov)}
                  </span>
                  <span className="f-ratio">{f.ratio}%</span>
                  <span className="f-commits">{f.commits}</span>
                  <span className="f-impact" style={{ color }}>
                    {f.impact}
                  </span>
                </div>
                {f.flows?.map((fl) => (
                  <div className="lp-sc-flow-row" key={fl.name}>
                    <span className="fl-icon" />
                    <span className="fl-name">{fl.name}</span>
                    <span className="fl-val" style={{ color }}>
                      {fl.health}
                    </span>
                    <span className="fl-val" style={{ color: covColorFor(fl.cov) }}>
                      {covLabel(fl.cov)}
                    </span>
                    <span className="fl-val">{fl.ratio}%</span>
                    <span className="fl-val">{fl.commits}</span>
                    <span />
                  </div>
                ))}
              </React.Fragment>
            );
          })}

          <div className="lp-sc-spacer" />

          <div className="lp-sc-out">
            <span className="ok">✓</span> Wrote{" "}
            <span className="hi">{repo.outputFile}</span>{" "}
            <span className="dim">
              ({repo.featureCount} features
              {repo.flowCount > 0 ? `, ${repo.flowCount} flows` : ""})
            </span>
          </div>
          <div className="lp-sc-out">
            <span className="dim">
              elapsed {repo.elapsed} · cost {repo.cost} · {repo.topNote}
            </span>
          </div>
        </div>
      </div>

      {/* ── Footer: prev, dots, next ── */}
      <div className="lp-carousel-footer">
        <button
          type="button"
          className="lp-carousel-arrow"
          onClick={prev}
          aria-label="Previous benchmark"
        >
          <ChevronLeft size={16} />
        </button>
        <div className="lp-carousel-dots" role="tablist">
          {REPOS.map((r, i) => (
            <button
              key={r.id}
              type="button"
              role="tab"
              aria-selected={i === active}
              aria-label={`Show ${r.shortName}`}
              className={`lp-carousel-dot ${i === active ? "is-active" : ""}`}
              onClick={() => jumpTo(i)}
            />
          ))}
        </div>
        <button
          type="button"
          className="lp-carousel-arrow"
          onClick={next}
          aria-label="Next benchmark"
        >
          <ChevronRight size={16} />
        </button>
      </div>

      {/* ── Caption: links to the public repo for reproducibility ── */}
      <div className="lp-showcase-caption">
        <span className="lp-showcase-caption-dot" />
        Real run on{" "}
        <a href={repo.repoUrl} target="_blank" rel="noopener noreferrer">
          {repo.shortName}
        </a>{" "}
        — {repo.langLabel}. Reproduce yourself with{" "}
        <span className="mono">pip install faultlines</span>.
      </div>
    </div>
  );
}
