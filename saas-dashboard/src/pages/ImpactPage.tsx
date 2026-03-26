import HealthBar from "../components/HealthBar";
import ImpactBadge from "../components/ImpactBadge";
import ScanSelector from "../components/ScanSelector";
import { useScanContext } from "../hooks/useScanContext";
import { AlertTriangle, Plug } from "lucide-react";
import { useNavigate } from "react-router-dom";

export default function ImpactPage() {
  const { selectedScan: scan, isLoadingScan } = useScanContext();
  const navigate = useNavigate();

  if (isLoadingScan || !scan) {
    return <div className="page"><div className="empty-state">Loading...</div></div>;
  }

  // Build impact-like scores from real data (without analytics, based on health + commits)
  const allFlows = scan.features.flatMap((f) =>
    f.flows.length > 0
      ? f.flows.map((fl) => ({
          name: fl.name,
          health_score: fl.health_score,
          total_commits: fl.total_commits,
          bug_fixes: fl.bug_fixes,
          bus_factor: fl.bus_factor ?? 0,
          feature_name: f.name,
        }))
      : [{
          name: f.name,
          health_score: f.health_score,
          total_commits: f.total_commits,
          bug_fixes: f.bug_fixes,
          bus_factor: 0,
          feature_name: f.name,
        }]
  );

  // Sort by risk (low health + high commits = most urgent)
  const sorted = [...allFlows].sort((a, b) => {
    const scoreA = a.health_score - (a.total_commits / 10);
    const scoreB = b.health_score - (b.total_commits / 10);
    return scoreA - scoreB;
  });

  const getLevel = (h: number) => {
    if (h < 30) return "critical";
    if (h < 50) return "high";
    if (h < 65) return "medium";
    if (h < 80) return "low";
    return "healthy";
  };

  const atRisk = sorted.filter((s) => s.health_score < 50);

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">Impact Scores</h1>
        <p className="page-subtitle">
          Technical debt weighted by activity. Connect analytics for traffic + error data.
        </p>
        <div className="mt-3">
          <ScanSelector />
        </div>
      </div>

      {/* Connect analytics prompt */}
      <div className="alert alert-warning">
        <AlertTriangle size={18} className="alert-icon" />
        <div style={{ flex: 1 }}>
          <div className="font-semibold">Connect analytics to unlock full Impact Scores</div>
          <div className="text-sm mt-1" style={{ opacity: 0.85 }}>
            Impact scores currently use health + commit activity.
            Connect PostHog or Sentry to add real user traffic and error data.
          </div>
        </div>
        <button className="btn btn-sm btn-secondary" onClick={() => navigate("/integrations")}>
          <Plug size={14} />
          Connect
        </button>
      </div>

      {/* Stats */}
      <div className="stats-grid">
        <div className={`stat-card ${atRisk.length > 0 ? "stat-critical" : "stat-good"}`}>
          <div className="stat-card-label">At Risk</div>
          <div className="stat-card-value">{atRisk.length}</div>
          <div className="stat-card-meta">Health below 50</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-label">Total Analyzed</div>
          <div className="stat-card-value">{sorted.length}</div>
          <div className="stat-card-meta">Features & flows</div>
        </div>
        <div className="stat-card">
          <div className="stat-card-label">Avg Health</div>
          <div className="stat-card-value">
            {sorted.length > 0 ? Math.round(sorted.reduce((s, f) => s + f.health_score, 0) / sorted.length) : 0}
          </div>
          <div className="stat-card-meta">Across all features</div>
        </div>
      </div>

      {/* Table */}
      <div className="card">
        <div className="card-header">
          <div>
            <div className="card-title">Risk Ranking</div>
            <div className="card-desc">
              Sorted by health score and commit activity. Pageviews and errors will appear after connecting analytics.
            </div>
          </div>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Feature / Flow</th>
              <th>Health</th>
              <th>Commits</th>
              <th>Bug Fixes</th>
              <th>Pageviews</th>
              <th>Errors</th>
              <th>Impact</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((s) => (
              <tr key={s.name}>
                <td>
                  <div style={{ fontWeight: 500 }}>{s.name}</div>
                  {s.name !== s.feature_name && (
                    <div className="text-sm text-muted">{s.feature_name}</div>
                  )}
                </td>
                <td><HealthBar score={s.health_score} /></td>
                <td>{s.total_commits}</td>
                <td style={{ color: s.bug_fixes > 10 ? "var(--danger)" : "inherit" }}>
                  {s.bug_fixes}
                </td>
                <td className="text-muted">—</td>
                <td className="text-muted">—</td>
                <td><ImpactBadge level={getLevel(s.health_score)} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
