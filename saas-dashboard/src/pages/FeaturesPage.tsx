import { useState } from "react";
import HealthBar from "../components/HealthBar";
import ScanSelector from "../components/ScanSelector";
import { useScanContext } from "../hooks/useScanContext";
import { ChevronDown, ChevronRight, Search } from "lucide-react";

export default function FeaturesPage() {
  const { selectedScan: scan, isLoadingScan } = useScanContext();
  const [expandedFeature, setExpandedFeature] = useState<string | null>(null);
  const [search, setSearch] = useState("");

  if (isLoadingScan || !scan) {
    return <div className="page"><div className="empty-state">Loading...</div></div>;
  }

  const features = scan.features.filter((f) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return (
      f.name.toLowerCase().includes(q) ||
      f.paths.some((p) => p.toLowerCase().includes(q)) ||
      f.flows.some((fl) => fl.name.toLowerCase().includes(q))
    );
  });

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">Feature Analysis</h1>
        <p className="page-subtitle">
          Detailed breakdown of all detected features and their flows
        </p>
        <div className="mt-3">
          <ScanSelector />
        </div>
      </div>

      {/* Search */}
      <div style={{ position: "relative", maxWidth: 400, marginBottom: 16 }}>
        <Search size={16} style={{ position: "absolute", left: 12, top: 10, color: "var(--text-muted)" }} />
        <input
          className="form-input"
          style={{ paddingLeft: 36 }}
          placeholder="Search features, flows, or file paths..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      <div className="card">
        <table className="data-table">
          <thead>
            <tr>
              <th style={{ width: 32 }}></th>
              <th>Feature / Flow</th>
              <th>Health</th>
              <th>Commits</th>
              <th>Bug Fixes</th>
              <th>Bug %</th>
              <th>Coverage</th>
              <th>Bus Factor</th>
              <th>Authors</th>
            </tr>
          </thead>
          <tbody>
            {features.map((f) => {
              const isExpanded = expandedFeature === f.name;
              const hasFlows = f.flows.length > 0;

              return (
                <FeatureRow
                  key={f.name}
                  feature={f}
                  isExpanded={isExpanded}
                  hasFlows={hasFlows}
                  onToggle={() => hasFlows && setExpandedFeature(isExpanded ? null : f.name)}
                />
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function FeatureRow({
  feature: f,
  isExpanded,
  hasFlows,
  onToggle,
}: {
  feature: import("../types").Feature;
  isExpanded: boolean;
  hasFlows: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr
        style={{ cursor: hasFlows ? "pointer" : "default" }}
        onClick={onToggle}
      >
        <td style={{ textAlign: "center", color: "var(--text-muted)" }}>
          {hasFlows ? (isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />) : null}
        </td>
        <td>
          <div style={{ fontWeight: 600 }}>{f.name}</div>
          {f.description && <div className="text-sm text-muted">{f.description}</div>}
        </td>
        <td><HealthBar score={f.health_score} /></td>
        <td>{f.total_commits}</td>
        <td>{f.bug_fixes}</td>
        <td>{Math.round(f.bug_fix_ratio * 100)}%</td>
        <td>
          {f.coverage_pct != null ? `${Math.round(f.coverage_pct)}%` : "—"}
        </td>
        <td>—</td>
        <td className="text-sm text-muted">{f.authors.join(", ")}</td>
      </tr>

      {isExpanded && f.flows.map((fl) => (
        <tr key={fl.name} style={{ background: "var(--bg)" }}>
          <td></td>
          <td style={{ paddingLeft: 32 }}>
            <div style={{ fontWeight: 500, fontSize: 13 }}>{fl.name}</div>
            {fl.description && <div className="text-sm text-muted">{fl.description}</div>}
            <div style={{ display: "flex", gap: 4, flexWrap: "wrap", marginTop: 6 }}>
              {fl.paths.map((p) => (
                <span key={p} className="text-mono" style={{
                  fontSize: 11,
                  background: "var(--accent-subtle)",
                  color: "var(--accent)",
                  padding: "2px 8px",
                  borderRadius: 4,
                }}>
                  {p}
                </span>
              ))}
            </div>
          </td>
          <td><HealthBar score={fl.health_score} /></td>
          <td>{fl.total_commits}</td>
          <td>{fl.bug_fixes}</td>
          <td>{Math.round(fl.bug_fix_ratio * 100)}%</td>
          <td>
            {fl.coverage_pct != null ? (
              <span style={{
                color: fl.coverage_pct >= 80 ? "var(--success)" : fl.coverage_pct >= 50 ? "var(--attention)" : "var(--danger)"
              }}>
                {Math.round(fl.coverage_pct)}%
              </span>
            ) : "—"}
          </td>
          <td>
            <span style={{
              color: fl.bus_factor === 1 ? "var(--danger)" : "inherit",
              fontWeight: fl.bus_factor === 1 ? 600 : 400,
            }}>
              {fl.bus_factor ?? "—"}
              {fl.bus_factor === 1 && " (risk)"}
            </span>
          </td>
          <td className="text-sm text-muted">{fl.authors.join(", ")}</td>
        </tr>
      ))}
    </>
  );
}
