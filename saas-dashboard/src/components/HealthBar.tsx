interface HealthBarProps {
  score: number;
}

function getHealthColor(score: number): string {
  if (score >= 80) return "var(--success)";
  if (score >= 60) return "var(--success-muted)";
  if (score >= 45) return "var(--attention-muted)";
  if (score >= 30) return "var(--severe)";
  return "var(--danger)";
}

export default function HealthBar({ score }: HealthBarProps) {
  const color = getHealthColor(score);

  return (
    <div className="health-bar">
      <div className="health-track">
        <div
          className="health-fill"
          style={{ width: `${score}%`, background: color }}
        />
      </div>
      <span className="health-label" style={{ color }}>
        {Math.round(score)}
      </span>
    </div>
  );
}
