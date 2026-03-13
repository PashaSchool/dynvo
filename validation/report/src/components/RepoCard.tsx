import { useState } from "react";
import type { RepoProgress, PhaseStatus } from "../types";
import FeatureAccuracy from "./FeatureAccuracy";

interface Props {
  repo: RepoProgress;
}

const STATUS_ICONS: Record<PhaseStatus, string> = {
  pending: "\u25CB",
  running: "\u25C9",
  completed: "\u2713",
  failed: "\u2717",
  skipped: "\u2013",
};

const STATUS_COLORS: Record<PhaseStatus, string> = {
  pending: "#9ca3af",
  running: "#f59e0b",
  completed: "#10b981",
  failed: "#ef4444",
  skipped: "#9ca3af",
};

function StatusBadge({ status, label }: { status: PhaseStatus; label: string }) {
  return (
    <span style={{ ...styles.statusBadge, color: STATUS_COLORS[status] }}>
      {STATUS_ICONS[status]} {label}
    </span>
  );
}

export default function RepoCard({ repo: rp }: Props) {
  const [expanded, setExpanded] = useState(false);
  const vr = rp.validation_result;

  return (
    <div style={styles.card}>
      <div
        style={styles.header}
        onClick={() => vr && setExpanded(!expanded)}
      >
        <div style={styles.left}>
          <span style={styles.name}>{rp.repo.name}</span>
          <span style={styles.reason}>{rp.repo.reason}</span>
        </div>
        <div style={styles.statuses}>
          <StatusBadge status={rp.clone_status} label="Clone" />
          <StatusBadge status={rp.analyze_status} label="Analyze" />
          <StatusBadge status={rp.validate_status} label="Validate" />
        </div>
        <div style={styles.scores}>
          {vr ? (
            <>
              <Score label="P" value={vr.precision} />
              <Score label="R" value={vr.recall} />
              <Score label="F1" value={vr.f1_score} />
            </>
          ) : (
            <span style={{ color: "#9ca3af", fontSize: 12 }}>
              {rp.error ? "Error" : "Pending"}
            </span>
          )}
        </div>
      </div>

      {rp.error && (
        <div style={styles.error}>{rp.error}</div>
      )}

      {expanded && vr && (
        <div style={styles.details}>
          <FeatureAccuracy result={vr} />
          {vr.metric_issues.length > 0 && (
            <div style={styles.issues}>
              <strong>Metric issues:</strong>
              <ul style={{ margin: "4px 0", paddingLeft: 18 }}>
                {vr.metric_issues.map((issue, i) => (
                  <li key={i} style={{ fontSize: 12, color: "#f59e0b" }}>{issue}</li>
                ))}
              </ul>
            </div>
          )}
          {vr.agent_reasoning && (
            <div style={styles.reasoning}>
              <strong>Agent reasoning:</strong>
              <p style={{ margin: "4px 0", whiteSpace: "pre-wrap" }}>
                {vr.agent_reasoning}
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Score({ label, value }: { label: string; value: number }) {
  const color = value >= 0.7 ? "#10b981" : value >= 0.4 ? "#f59e0b" : "#ef4444";
  return (
    <span style={{ fontSize: 13, fontWeight: 600, color }}>
      {label}: {(value * 100).toFixed(0)}%
    </span>
  );
}

const styles: Record<string, React.CSSProperties> = {
  card: {
    background: "#fff",
    border: "1px solid #e8eaef",
    borderRadius: 6,
    overflow: "hidden",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "12px 16px",
    cursor: "pointer",
    gap: 16,
  },
  left: {
    display: "flex",
    flexDirection: "column",
    gap: 2,
    flex: 1,
    minWidth: 0,
  },
  name: {
    fontWeight: 600,
    fontSize: 14,
  },
  reason: {
    fontSize: 11,
    color: "#6b7284",
    whiteSpace: "nowrap",
    overflow: "hidden",
    textOverflow: "ellipsis",
  },
  statuses: {
    display: "flex",
    gap: 10,
    flexShrink: 0,
  },
  statusBadge: {
    fontSize: 11,
    fontWeight: 600,
    whiteSpace: "nowrap",
  },
  scores: {
    display: "flex",
    gap: 12,
    flexShrink: 0,
  },
  error: {
    padding: "6px 16px 10px",
    fontSize: 12,
    color: "#ef4444",
    background: "#fef2f2",
  },
  details: {
    borderTop: "1px solid #e8eaef",
    padding: 16,
  },
  issues: {
    marginTop: 12,
    fontSize: 12,
  },
  reasoning: {
    marginTop: 12,
    fontSize: 12,
    color: "#6b7284",
  },
};
