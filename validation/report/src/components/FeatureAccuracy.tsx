import type { ValidationResult } from "../types";

interface Props {
  result: ValidationResult;
}

export default function FeatureAccuracy({ result: vr }: Props) {
  return (
    <div>
      {/* Matched features */}
      {vr.matched_features.length > 0 && (
        <div style={{ marginBottom: 12 }}>
          <div style={styles.sectionLabel}>Matched features</div>
          <table style={styles.table}>
            <thead>
              <tr>
                <th style={styles.th}>Expected</th>
                <th style={styles.th}>Detected</th>
                <th style={{ ...styles.th, textAlign: "right" }}>Confidence</th>
                <th style={styles.th}>Notes</th>
              </tr>
            </thead>
            <tbody>
              {vr.matched_features.map((m, i) => (
                <tr key={i}>
                  <td style={styles.td}>{m.expected}</td>
                  <td style={styles.td}>
                    <span style={styles.detected}>{m.detected}</span>
                  </td>
                  <td style={{ ...styles.td, textAlign: "right" }}>
                    <span style={{
                      color: m.confidence >= 0.7 ? "#10b981" : m.confidence >= 0.4 ? "#f59e0b" : "#ef4444",
                      fontWeight: 600,
                    }}>
                      {(m.confidence * 100).toFixed(0)}%
                    </span>
                  </td>
                  <td style={{ ...styles.td, color: "#6b7284" }}>{m.notes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Missed and spurious */}
      <div style={{ display: "flex", gap: 24 }}>
        {vr.missed_features.length > 0 && (
          <div>
            <div style={styles.sectionLabel}>Missed features</div>
            <div style={styles.chips}>
              {vr.missed_features.map((f) => (
                <span key={f} style={styles.chipRed}>{f}</span>
              ))}
            </div>
          </div>
        )}
        {vr.spurious_features.length > 0 && (
          <div>
            <div style={styles.sectionLabel}>Spurious features</div>
            <div style={styles.chips}>
              {vr.spurious_features.map((f) => (
                <span key={f} style={styles.chipYellow}>{f}</span>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  sectionLabel: {
    fontSize: 11,
    fontWeight: 600,
    textTransform: "uppercase",
    letterSpacing: 0.5,
    color: "#6b7284",
    marginBottom: 6,
  },
  table: {
    width: "100%",
    borderCollapse: "collapse",
    fontSize: 13,
  },
  th: {
    padding: "6px 10px",
    textAlign: "left",
    fontSize: 11,
    fontWeight: 600,
    color: "#6b7284",
    borderBottom: "1px solid #e8eaef",
    textTransform: "uppercase",
    letterSpacing: 0.3,
  },
  td: {
    padding: "6px 10px",
    borderBottom: "1px solid #f3f4f6",
    verticalAlign: "middle",
  },
  detected: {
    background: "#eef0ff",
    color: "#6366f1",
    padding: "2px 8px",
    borderRadius: 4,
    fontSize: 12,
    fontWeight: 500,
  },
  chips: {
    display: "flex",
    flexWrap: "wrap",
    gap: 4,
  },
  chipRed: {
    fontSize: 11,
    padding: "2px 8px",
    borderRadius: 10,
    background: "rgba(239, 68, 68, 0.1)",
    color: "#ef4444",
    fontWeight: 500,
  },
  chipYellow: {
    fontSize: 11,
    padding: "2px 8px",
    borderRadius: 10,
    background: "rgba(245, 158, 11, 0.1)",
    color: "#f59e0b",
    fontWeight: 500,
  },
};
