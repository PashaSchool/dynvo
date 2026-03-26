import type { FeatureMap } from "./types";

export interface ScanMeta {
  filename: string;
  repo_path: string;
  remote_url: string;
  analyzed_at: string;
  total_commits: number;
  date_range_days: number;
  feature_count: number;
}

export async function fetchScans(): Promise<ScanMeta[]> {
  const res = await fetch("/api/scans");
  if (!res.ok) throw new Error(`Failed to fetch scans: ${res.status}`);
  return res.json();
}

export async function fetchScan(filename: string): Promise<FeatureMap> {
  const res = await fetch(`/api/scans/${filename}`);
  if (!res.ok) throw new Error(`Failed to fetch scan: ${res.status}`);
  return res.json();
}
