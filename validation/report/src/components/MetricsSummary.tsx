import type { OverallSummary } from "../types";

interface Props {
  summary: OverallSummary;
}

function f1Color(f1: number): string {
  if (f1 >= 0.7) return "#10b981";
  if (f1 >= 0.4) return "#f59e0b";
  return "#ef4444";
}

export default function MetricsSummary({ summary: s }: Props) {
  return (
    <div style={styles.wrap}>
      <Card label="Repos" value={`${s.successful_repos}/${s.total_repos}`} />
      <Card label="Precision" value={`${(s.avg_precision * 100).toFixed(0)}%`} />
      <Card label="Recall" value={`${(s.avg_recall * 100).toFixed(0)}%`} />
      <Card
        label="F1 Score"
        value={`${(s.avg_f1 * 100).toFixed(0)}%`}
        color={f1Color(s.avg_f1)}
      />
      <Card
        label="Features"
        value={`${s.total_features_detected} / ${s.total_features_expected}`}
        sub="detected / expected"
      />
    </div>
  );
}

function Card({
  label,
  value,
  color,
  sub,
}: {
  label: string;
  value: string;
  color?: string;
  sub?: string;
}) {
  return (
    <div style={styles.card}>
      <div style={{ ...styles.value, color: color ?? "#1a1c23" }}>{value}</div>
      <div style={styles.label}>{label}</div>
      {sub && <div style={styles.sub}>{sub}</div>}
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  wrap: {
    display: "flex",
    gap: 12,
    marginBottom: 24,
    flexWrap: "wrap",
  },
  card: {
    background: "#fff",
    border: "1px solid #e8eaef",
    borderRadius: 6,
    padding: "12px 18px",
    minWidth: 100,
  },
  value: {
    fontSize: 24,
    fontWeight: 700,
    lineHeight: 1,
  },
  label: {
    fontSize: 11,
    color: "#6b7284",
    marginTop: 4,
    textTransform: "uppercase" as const,
    letterSpacing: 0.3,
  },
  sub: {
    fontSize: 10,
    color: "#9ca3af",
    marginTop: 2,
  },
};
