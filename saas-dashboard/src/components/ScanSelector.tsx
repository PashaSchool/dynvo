import { useScanContext } from "../hooks/useScanContext";
import { ChevronDown } from "lucide-react";

export default function ScanSelector() {
  const { scans, selectedFilename, selectScan, isLoadingList } = useScanContext();

  if (isLoadingList) return <span className="text-muted text-sm">Loading scans...</span>;
  if (scans.length === 0) return null;

  return (
    <select
      className="form-select"
      style={{ maxWidth: 400, fontSize: 13 }}
      value={selectedFilename || ""}
      onChange={(e) => selectScan(e.target.value)}
    >
      {scans.map((s) => {
        const repoName = s.repo_path.split("/").pop() || s.repo_path;
        const date = new Date(s.analyzed_at).toLocaleDateString("en-US", {
          month: "short", day: "numeric", year: "numeric", hour: "2-digit", minute: "2-digit",
        });
        return (
          <option key={s.filename} value={s.filename}>
            {repoName} — {date} ({s.feature_count} features, {s.total_commits} commits)
          </option>
        );
      })}
    </select>
  );
}
