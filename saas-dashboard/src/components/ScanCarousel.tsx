"use client";

import React, { useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";

// ── Feature row type ─────────────────────────────────────────────
type Feature = {
  name: string;
  sub: string;
  health: number;
  ratio: number;
  commits: number;
  files: number;
  impact: "critical" | "high" | "medium" | "low" | "healthy";
  flows?: { name: string; health: number; ratio: number; commits: number }[];
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
const feat = (
  name: string,
  sub: string,
  health: number,
  ratio: number,
  commits: number,
  files: number,
  flows?: Feature["flows"],
): Feature => ({
  name,
  sub,
  health: Math.round(health),
  ratio: Math.round(ratio),
  commits,
  files,
  impact: impactFor(health),
  flows,
});

// ── Showcase data — real numbers from the Day 11–14 LLM regression
//    runs (tests/baseline/accuracy-7repos) plus fresh Day 15 scans on
//    5 additional popular OSS applications. ──
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
      feat("trpc/viewer", "packages/trpc/server/routers/viewer", 26, 68, 539, 728, [
        { name: "list-event-types", health: 22, ratio: 71, commits: 187 },
        { name: "update-availability", health: 31, ratio: 65, commits: 142 },
        { name: "manage-team-members", health: 25, ratio: 69, commits: 98 },
      ]),
      feat("web/bookings", "apps/web/pages/bookings", 16, 76, 172, 109),
      feat("web/settings", "apps/web/pages/settings", 23, 70, 206, 178),
      feat("ee/billing", "packages/features/ee/billing", 35, 63, 142, 171),
      feat("lib/server", "packages/lib/server", 35, 63, 177, 48),
      feat("web/dashboard", "apps/web/pages/dashboard", 28, 67, 175, 128),
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
      feat("web/issues", "apps/web/ce/components/issues", 36, 62, 172, 447, [
        { name: "view-issues-in-layout", health: 33, ratio: 64, commits: 78 },
        { name: "filter-and-sort-issues", health: 42, ratio: 59, commits: 52 },
        { name: "delete-issue", health: 26, ratio: 68, commits: 34 },
      ]),
      feat("editor/editor-extensions", "packages/editor/src/extensions", 20, 72, 104, 111),
      feat("web/workspace", "apps/web/ce/components/workspace", 52, 54, 106, 100),
      feat("web/pages", "apps/web/ce/components/pages", 36, 62, 76, 99),
      feat("web/project", "apps/web/core/components/project", 46, 57, 87, 88),
      feat("web/inbox", "apps/web/ce/components/inbox", 38, 61, 67, 48),
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
      feat("trpc/envelope", "packages/trpc/server/envelope-router", 81, 37, 57, 132, [
        { name: "create-envelope", health: 77, ratio: 40, commits: 24 },
        { name: "send-for-signing", health: 82, ratio: 36, commits: 19 },
        { name: "track-status", health: 85, ratio: 33, commits: 14 },
      ]),
      feat("remix/document-signing", "apps/remix/app/routes/sign", 67, 46, 28, 25),
      feat("ee/billing-management", "packages/ee/billing", 46, 57, 44, 49),
      feat("trpc/organisation", "packages/trpc/server/organisation", 64, 48, 41, 28),
      feat("ui/document", "packages/ui/primitives/document", 56, 52, 67, 51),
      feat("auth/server", "packages/auth/server", 87, 31, 18, 14),
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
      feat("rich-text-editor", "shared/editor", 29, 66, 438, 249, [
        { name: "insert-table", health: 22, ratio: 71, commits: 62 },
        { name: "render-editor", health: 33, ratio: 64, commits: 87 },
        { name: "structure-content", health: 26, ratio: 68, commits: 54 },
      ]),
      feat("api-backend", "server/routes + server/collaboration", 44, 58, 375, 275),
      feat("dashboard", "app/scenes + server/routes/app", 33, 64, 318, 256),
      feat("document-management", "app/components/DocumentExplorer", 38, 61, 218, 74),
      feat("document-editor", "app/components/DocumentBreadcrumb", 23, 70, 212, 64),
      feat("plugins", "plugins/* (azure, slack, …)", 50, 55, 101, 95),
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
      feat("web/survey", "apps/web/modules/survey", 54, 53, 289, 249, [
        { name: "create-survey-from-template", health: 44, ratio: 58, commits: 62 },
        { name: "take-survey-via-link", health: 62, ratio: 49, commits: 41 },
        { name: "take-contact-survey", health: 56, ratio: 52, commits: 28 },
      ]),
      feat("web/shared-ui", "apps/web/modules/ui", 71, 44, 331, 428),
      feat("web/ee", "apps/web/modules/ee", 72, 43, 166, 181),
      feat("web/organization", "apps/web/modules/organization", 65, 47, 114, 94),
      feat("surveys/general", "packages/surveys/src", 36, 62, 98, 45),
      feat("web/auth", "apps/web/modules/auth", 84, 34, 64, 67),
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
    flowCount: 62,
    elapsed: "3m 51s",
    cost: "$0.14",
    outputFile: "~/.faultlines/feature-map-excalidraw.json",
    topNote: "top 6 of 15 shown",
    features: [
      feat("excalidraw/shared-ui", "packages/excalidraw/components", 54, 53, 64, 132, [
        { name: "render-toolbar", health: 50, ratio: 55, commits: 24 },
        { name: "show-context-menu", health: 60, ratio: 50, commits: 18 },
        { name: "toggle-panels", health: 56, ratio: 52, commits: 14 },
      ]),
      feat("excalidraw/data", "packages/excalidraw/data", 36, 62, 40, 22),
      feat("excalidraw/renderer", "packages/excalidraw/renderer", 36, 62, 34, 16),
      feat("excalidraw-app/data", "packages/excalidraw-app/data", 44, 58, 12, 17),
      feat("math", "packages/math", 64, 48, 21, 26),
      feat("excalidraw/selection-tools", "packages/excalidraw/components/LassoTrail", 48, 56, 9, 2),
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
      feat("admin-x-framework", "apps/admin-x-framework/src", 14, 78, 203, 77, [
        { name: "manage-site-settings", health: 12, ratio: 80, commits: 62 },
        { name: "manage-staff-users", health: 16, ratio: 76, commits: 48 },
        { name: "manage-content", health: 13, ratio: 79, commits: 54 },
      ]),
      feat("ghost/members", "ghost/core/core/server/services/members", 6, 90, 173, 112),
      feat("stats/stats", "apps/stats/src", 10, 82, 174, 44),
      feat("ghost/email", "ghost/core/core/server/services/email", 10, 83, 144, 62),
      feat("shade/ui", "apps/shade/src", 12, 80, 143, 124),
      feat("posts/post-analytics", "apps/posts/src/views", 9, 84, 139, 25),
    ],
  },
  {
    id: "trpc",
    title: "~/trpc",
    repoUrl: "https://github.com/trpc/trpc",
    shortName: "trpc",
    langLabel: "TS monorepo · library · 9 packages",
    detected: "monorepo library (pnpm workspace, 9 packages)",
    fileCount: "1,573 files",
    commitCount: "3,421 commits",
    featureCount: 16,
    flowCount: 0,
    elapsed: "1m 17s",
    cost: "$0.11",
    outputFile: "~/.faultlines/feature-map-trpc.json",
    topNote: "top 6 of 16 modules · library mode · no flows",
    features: [
      feat("server/trpc-core", "packages/server/src", 15, 77, 35, 39),
      feat("client/links", "packages/client/src/links", 14, 78, 18, 24),
      feat("server/adapters", "packages/server/src/adapters", 22, 71, 17, 26),
      feat("tanstack-react-query", "packages/tanstack-react-query", 5, 91, 11, 8),
      feat("openapi", "packages/openapi/src", 56, 52, 25, 7),
      feat("next-adapter", "packages/next/src", 88, 30, 63, 23),
    ],
  },
  {
    id: "chi",
    title: "~/chi",
    repoUrl: "https://github.com/go-chi/chi",
    shortName: "chi",
    langLabel: "Go · HTTP router library · flat layout",
    detected: "library (go.mod, no main.go) — flows suppressed",
    fileCount: "95 files",
    commitCount: "1,842 commits",
    featureCount: 38,
    flowCount: 0,
    elapsed: "18s",
    cost: "$0.02",
    outputFile: "~/.faultlines/feature-map-chi.json",
    topNote: "top 6 of 38 modules · library mode",
    features: [
      feat("chi", "chi.go", 99, 0, 2, 1),
      feat("mux", "mux.go", 94, 20, 5, 1),
      feat("tree", "tree.go", 95, 17, 6, 1),
      feat("basic_auth", "middleware/basic_auth.go", 89, 29, 7, 1),
      feat("compress", "middleware/compress.go", 99, 0, 1, 1),
      feat("recoverer", "middleware/recoverer.go", 96, 14, 4, 1),
    ],
  },
  {
    id: "gin",
    title: "~/gin",
    repoUrl: "https://github.com/gin-gonic/gin",
    shortName: "gin",
    langLabel: "Go · web framework · flat layout",
    detected: "library (go.mod, no cmd/ dir) — flows suppressed",
    fileCount: "130 files",
    commitCount: "3,987 commits",
    featureCount: 22,
    flowCount: 0,
    elapsed: "15s",
    cost: "$0.01",
    outputFile: "~/.faultlines/feature-map-gin.json",
    topNote: "top 6 of 22 modules · library mode",
    features: [
      feat("binding", "binding/", 87, 31, 16, 17),
      feat("render", "render/", 96, 15, 10, 14),
      feat("context", "context.go + context_*.go", 83, 35, 23, 2),
      feat("recovery", "recovery.go", 89, 29, 7, 1),
      feat("routergroup", "routergroup.go", 90, 28, 7, 1),
      feat("logger", "logger.go", 17, 75, 4, 1),
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
