import { useState } from "react";
import Toggle from "../components/Toggle";
import { useScanContext } from "../hooks/useScanContext";
import type { GitHubCommentConfig } from "../types";
import { Eye } from "lucide-react";

const DEFAULT_CONFIG: GitHubCommentConfig = {
  is_enabled: true,
  show_health_score: true,
  show_bug_fix_ratio: true,
  show_top_risks: true,
  show_flows: true,
  show_analytics: true,
  show_impact_score: true,
  show_coverage: true,
  show_authors: false,
  max_features_in_comment: 5,
  comment_style: "detailed",
};

export default function GitHubAppPage() {
  const [config, setConfig] = useState<GitHubCommentConfig>(DEFAULT_CONFIG);
  const [activeTab, setActiveTab] = useState<"settings" | "preview">("settings");

  const update = (key: keyof GitHubCommentConfig, value: unknown) => {
    setConfig((prev) => ({ ...prev, [key]: value }));
  };

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">GitHub App</h1>
        <p className="page-subtitle">
          Configure what FeatureMap posts in PR comments
        </p>
      </div>

      <div className="tabs">
        <button className={`tab ${activeTab === "settings" ? "active" : ""}`} onClick={() => setActiveTab("settings")}>
          Settings
        </button>
        <button className={`tab ${activeTab === "preview" ? "active" : ""}`} onClick={() => setActiveTab("preview")}>
          <Eye size={14} style={{ marginRight: 4, verticalAlign: -2 }} />
          Live Preview
        </button>
      </div>

      {activeTab === "settings" && (
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 24 }}>
          {/* Left: Toggle settings */}
          <div className="card">
            <div className="card-header">
              <div className="card-title">Comment Sections</div>
            </div>
            <div className="card-body">
              <Toggle
                title="Enable PR Comments"
                description="Post FeatureMap analysis on every PR"
                checked={config.is_enabled}
                onChange={(v) => update("is_enabled", v)}
              />
              <Toggle
                title="Health Score"
                description="Show health score for affected features"
                checked={config.show_health_score}
                onChange={(v) => update("show_health_score", v)}
              />
              <Toggle
                title="Bug Fix Ratio"
                description="Show bug fix percentage"
                checked={config.show_bug_fix_ratio}
                onChange={(v) => update("show_bug_fix_ratio", v)}
              />
              <Toggle
                title="Top Risks"
                description="Highlight features at risk in the PR"
                checked={config.show_top_risks}
                onChange={(v) => update("show_top_risks", v)}
              />
              <Toggle
                title="Flows"
                description="Show affected user-facing flows"
                checked={config.show_flows}
                onChange={(v) => update("show_flows", v)}
              />
              <Toggle
                title="Analytics Data"
                description="Include traffic and error data from connected providers"
                checked={config.show_analytics}
                onChange={(v) => update("show_analytics", v)}
              />
              <Toggle
                title="Impact Score"
                description="Show combined health + traffic + errors score"
                checked={config.show_impact_score}
                onChange={(v) => update("show_impact_score", v)}
              />
              <Toggle
                title="Code Coverage"
                description="Show coverage percentage for affected code"
                checked={config.show_coverage}
                onChange={(v) => update("show_coverage", v)}
              />
              <Toggle
                title="Authors"
                description="List contributors for affected features"
                checked={config.show_authors}
                onChange={(v) => update("show_authors", v)}
              />
            </div>
          </div>

          {/* Right: Comment style */}
          <div>
            <div className="card mb-4">
              <div className="card-header">
                <div className="card-title">Comment Style</div>
              </div>
              <div className="card-body">
                <div className="form-group">
                  <label className="form-label">Style</label>
                  <select
                    className="form-select"
                    value={config.comment_style}
                    onChange={(e) => update("comment_style", e.target.value)}
                  >
                    <option value="minimal">Minimal — One-line summary</option>
                    <option value="compact">Compact — Summary table</option>
                    <option value="detailed">Detailed — Full breakdown with flows</option>
                  </select>
                </div>

                <div className="form-group">
                  <label className="form-label">Max features in comment</label>
                  <input
                    className="form-input"
                    type="number"
                    min={1}
                    max={20}
                    value={config.max_features_in_comment}
                    onChange={(e) => update("max_features_in_comment", parseInt(e.target.value) || 5)}
                  />
                  <div className="form-hint">
                    Only the top N most-affected features will be shown
                  </div>
                </div>
              </div>
            </div>

            <div className="card">
              <div className="card-header">
                <div className="card-title">Trigger Rules</div>
              </div>
              <div className="card-body">
                <Toggle
                  title="Comment on every PR"
                  description="Post analysis comment when a PR is opened or updated"
                  checked={true}
                  onChange={() => {}}
                />
                <Toggle
                  title="Only on PRs touching at-risk features"
                  description="Only comment when the PR modifies files in features with health < 50"
                  checked={false}
                  onChange={() => {}}
                />
                <Toggle
                  title="Skip draft PRs"
                  description="Don't post on draft PRs until they're ready for review"
                  checked={true}
                  onChange={() => {}}
                />
              </div>
            </div>
          </div>
        </div>
      )}

      {activeTab === "preview" && (
        <div>
          <p className="text-sm text-muted mb-4">
            This is how the FeatureMap comment will appear in your PRs based on current settings.
          </p>
          <GitHubCommentPreview config={config} />
        </div>
      )}
    </div>
  );
}

function GitHubCommentPreview({ config }: { config: GitHubCommentConfig }) {
  const { selectedScan } = useScanContext();

  if (!config.is_enabled) {
    return (
      <div className="card">
        <div className="card-body empty-state">
          <h3>Comments disabled</h3>
          <p className="text-muted">Enable PR comments to see a preview</p>
        </div>
      </div>
    );
  }

  if (!selectedScan) {
    return (
      <div className="card">
        <div className="card-body empty-state">
          <h3>No scan data</h3>
          <p className="text-muted">Run a scan to see the comment preview</p>
        </div>
      </div>
    );
  }

  const features = selectedScan.features.slice(0, config.max_features_in_comment);

  return (
    <div className="gh-preview">
      <h3>FeatureMap Analysis</h3>

      {config.comment_style !== "minimal" && (
        <table>
          <thead>
            <tr>
              <th>Feature</th>
              {config.show_health_score && <th>Health</th>}
              {config.show_bug_fix_ratio && <th>Bug %</th>}
              {config.show_coverage && <th>Coverage</th>}
              {config.show_analytics && <th>Traffic</th>}
              {config.show_analytics && <th>Errors</th>}
              {config.show_impact_score && <th>Impact</th>}
            </tr>
          </thead>
          <tbody>
            {features.map((f) => {
              const healthClass = f.health_score >= 70 ? "gh-health-good" : f.health_score >= 45 ? "gh-health-warn" : "gh-health-bad";
              const level = f.health_score < 30 ? "critical" : f.health_score < 50 ? "high" : f.health_score < 70 ? "medium" : "healthy";

              return (
                <tr key={f.name}>
                  <td>{f.name}</td>
                  {config.show_health_score && <td className={healthClass}>{Math.round(f.health_score)}</td>}
                  {config.show_bug_fix_ratio && <td>{Math.round(f.bug_fix_ratio * 100)}%</td>}
                  {config.show_coverage && <td>{f.coverage_pct != null ? `${Math.round(f.coverage_pct)}%` : "—"}</td>}
                  {config.show_analytics && <td>—</td>}
                  {config.show_analytics && <td>—</td>}
                  {config.show_impact_score && (
                    <td>
                      <span className="gh-impact-badge" style={{
                        background: level === "critical" ? "rgba(248,81,73,0.2)" :
                                    level === "high" ? "rgba(210,153,34,0.2)" :
                                    "rgba(63,185,80,0.2)",
                        color: level === "critical" ? "#f85149" :
                               level === "high" ? "#d29922" : "#3fb950",
                      }}>
                        {level}
                      </span>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {config.comment_style === "minimal" && (
        <div>
          This PR affects <strong>{features.length} features</strong>.
          {features.filter(f => f.health_score < 50).length > 0 && (
            <span className="gh-health-bad">
              {" "}{features.filter(f => f.health_score < 50).length} at risk.
            </span>
          )}
        </div>
      )}

      {config.show_top_risks && config.comment_style === "detailed" && (
        <div style={{ marginTop: 12 }}>
          <strong>Top Risks:</strong>
          <ul style={{ margin: "4px 0 0 16px", listStyle: "disc" }}>
            {features.filter(f => f.health_score < 50).map(f => (
              <li key={f.name}>
                <span className="gh-health-bad">{f.name}</span> — health {Math.round(f.health_score)}, {f.bug_fixes} bug fixes
              </li>
            ))}
          </ul>
        </div>
      )}

      {config.show_flows && config.comment_style === "detailed" && (
        <div style={{ marginTop: 12 }}>
          <strong>Affected Flows:</strong>
          {features.filter(f => f.flows.length > 0).map(f => (
            <div key={f.name} style={{ marginLeft: 8, marginTop: 4 }}>
              {f.flows.map(fl => (
                <div key={fl.name} style={{ fontSize: 12 }}>
                  &rarr; {fl.name} (health: <span className={fl.health_score < 50 ? "gh-health-bad" : "gh-health-good"}>{Math.round(fl.health_score)}</span>)
                </div>
              ))}
            </div>
          ))}
        </div>
      )}

      {config.show_authors && config.comment_style === "detailed" && (
        <div style={{ marginTop: 12 }}>
          <strong>Contributors:</strong>{" "}
          {[...new Set(features.flatMap(f => f.authors))].join(", ")}
        </div>
      )}

      <div className="gh-footer">
        Generated by FeatureMap &middot; <a href="#" style={{ color: "#58a6ff" }}>View full report</a>
      </div>
    </div>
  );
}
