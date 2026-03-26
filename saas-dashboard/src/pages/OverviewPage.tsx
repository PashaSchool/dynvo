import HealthBar from "../components/HealthBar";
import ScanSelector from "../components/ScanSelector";
import { useScanContext } from "../hooks/useScanContext";

export default function OverviewPage() {
  const { selectedScan: scan, isLoadingScan } = useScanContext();

  if (isLoadingScan) {
    return <div className="page"><div className="empty-state">Loading scan data...</div></div>;
  }

  if (!scan) {
    return (
      <div className="page">
        <div className="empty-state">
          <h3>No scans yet</h3>
          <p>Run <code>faultline analyze /path/to/repo --llm</code> to generate your first scan.</p>
        </div>
      </div>
    );
  }

  const features = scan.features;
  const avgHealth = features.length > 0
    ? Math.round(features.reduce((sum, f) => sum + f.health_score, 0) / features.length)
    : 0;
  const totalBugFixes = features.reduce((sum, f) => sum + f.bug_fixes, 0);
  const atRisk = features.filter((f) => f.health_score < 50).length;
  const totalFlows = features.reduce((sum, f) => sum + f.flows.length, 0);
  const repoName = scan.repo_path.split("/").pop() || scan.repo_path;

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">{repoName}</h1>
        <p className="page-subtitle">
          Analyzed {new Date(scan.analyzed_at).toLocaleDateString()} &middot; {scan.date_range_days} days window
        </p>
        <div className="mt-3">
          <ScanSelector />
        </div>
      </div>

      <div className="stats-grid">
        <div className="stat-card">
          <div className="stat-card-label">Features</div>
          <div className="stat-card-value">{features.length}</div>
          <div className="stat-card-meta">{totalFlows > 0 ? `${totalFlows} flows detected` : "No flows"}</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-label">Avg Health</div>
          <div className="stat-card-value" style={{ color: avgHealth >= 60 ? "var(--success)" : avgHealth >= 40 ? "var(--attention)" : "var(--danger)" }}>
            {avgHealth}
          </div>
          <div className="stat-card-meta">Score 0-100</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-label">Total Commits</div>
          <div className="stat-card-value">{scan.total_commits.toLocaleString()}</div>
          <div className="stat-card-meta">Last {scan.date_range_days} days</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-label">Bug Fixes</div>
          <div className="stat-card-value">{totalBugFixes}</div>
          <div className="stat-card-meta">
            {scan.total_commits > 0 ? `${Math.round(totalBugFixes / scan.total_commits * 100)}% of all commits` : ""}
          </div>
        </div>
        <div className={`stat-card ${atRisk > 0 ? "stat-critical" : "stat-good"}`}>
          <div className="stat-card-label">At Risk</div>
          <div className="stat-card-value">{atRisk}</div>
          <div className="stat-card-meta">Features with health &lt; 50</div>
        </div>
      </div>

      {/* Features Table */}
      <div className="card">
        <div className="card-header">
          <div>
            <div className="card-title">All Features</div>
            <div className="card-desc">{features.length} features detected from git history</div>
          </div>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Feature</th>
              <th>Health</th>
              <th>Commits</th>
              <th>Bug Fixes</th>
              <th>Bug %</th>
              <th>Flows</th>
              <th>Coverage</th>
              <th>Authors</th>
            </tr>
          </thead>
          <tbody>
            {[...features].sort((a, b) => a.health_score - b.health_score).map((f) => (
              <tr key={f.name}>
                <td>
                  <div style={{ fontWeight: 500 }}>{f.name}</div>
                  {f.description && (
                    <div className="text-sm text-muted" style={{ marginTop: 2 }}>{f.description}</div>
                  )}
                </td>
                <td><HealthBar score={f.health_score} /></td>
                <td>{f.total_commits}</td>
                <td>{f.bug_fixes}</td>
                <td>{Math.round(f.bug_fix_ratio * 100)}%</td>
                <td>{f.flows.length || "—"}</td>
                <td>
                  {f.coverage_pct != null ? (
                    <span style={{ color: f.coverage_pct >= 80 ? "var(--success)" : f.coverage_pct >= 50 ? "var(--attention)" : "var(--danger)" }}>
                      {Math.round(f.coverage_pct)}%
                    </span>
                  ) : "—"}
                </td>
                <td className="text-muted text-sm">{f.authors.slice(0, 3).join(", ")}{f.authors.length > 3 ? ` +${f.authors.length - 3}` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
