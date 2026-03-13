import { useEffect, useState } from "react";
import type { OverallProgress } from "./types";
import MetricsSummary from "./components/MetricsSummary";
import RepoCard from "./components/RepoCard";

export default function App() {
  const [data, setData] = useState<OverallProgress | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval>;

    const fetchProgress = async () => {
      try {
        const res = await fetch("/api/progress");
        const json = await res.json();
        if (json) setData(json);
      } catch (e) {
        setError(String(e));
      }
    };

    fetchProgress();
    timer = setInterval(fetchProgress, 2000);

    return () => clearInterval(timer);
  }, []);

  if (error) {
    return (
      <div style={styles.container}>
        <div style={styles.error}>Error: {error}</div>
      </div>
    );
  }

  if (!data) {
    return (
      <div style={styles.container}>
        <div style={styles.loading}>
          Loading validation results...
          <div style={styles.hint}>
            Run <code>python -m validation.run</code> to generate data
          </div>
        </div>
      </div>
    );
  }

  const isRunning = data.phase !== "done";

  return (
    <div style={styles.container}>
      <header style={styles.header}>
        <h1 style={styles.title}>faultline validation report</h1>
        <span style={{
          ...styles.badge,
          background: isRunning ? "#fef3c7" : "#d1fae5",
          color: isRunning ? "#92400e" : "#065f46",
        }}>
          {isRunning ? `Running: ${data.phase}` : "Complete"}
        </span>
      </header>

      {data.summary && <MetricsSummary summary={data.summary} />}

      <section>
        <h2 style={styles.sectionTitle}>Repositories</h2>
        <div style={styles.grid}>
          {data.repos.map((rp) => (
            <RepoCard key={rp.repo.name} repo={rp} />
          ))}
        </div>
      </section>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  container: {
    maxWidth: 1000,
    margin: "0 auto",
    padding: "32px 24px",
    fontFamily: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    color: "#1a1c23",
    fontSize: 14,
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    marginBottom: 24,
  },
  title: {
    fontSize: 22,
    fontWeight: 700,
    margin: 0,
  },
  badge: {
    fontSize: 12,
    fontWeight: 600,
    padding: "3px 10px",
    borderRadius: 12,
  },
  sectionTitle: {
    fontSize: 15,
    fontWeight: 600,
    marginBottom: 12,
  },
  grid: {
    display: "flex",
    flexDirection: "column",
    gap: 12,
  },
  loading: {
    textAlign: "center",
    padding: 60,
    color: "#6b7284",
  },
  hint: {
    marginTop: 8,
    fontSize: 12,
  },
  error: {
    textAlign: "center",
    padding: 60,
    color: "#ef4444",
  },
};
