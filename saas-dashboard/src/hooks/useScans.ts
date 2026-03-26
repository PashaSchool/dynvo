import { useEffect, useState } from "react";
import { fetchScans, fetchScan } from "../api";
import type { ScanMeta } from "../api";
import type { FeatureMap } from "../types";

export function useScans() {
  const [scans, setScans] = useState<ScanMeta[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchScans()
      .then(setScans)
      .catch((e) => setError(String(e)))
      .finally(() => setIsLoading(false));
  }, []);

  return { scans, isLoading, error };
}

export function useScan(filename: string | null) {
  const [scan, setScan] = useState<FeatureMap | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!filename) {
      setScan(null);
      return;
    }
    setIsLoading(true);
    fetchScan(filename)
      .then(setScan)
      .catch((e) => setError(String(e)))
      .finally(() => setIsLoading(false));
  }, [filename]);

  return { scan, isLoading, error };
}

/**
 * Group scans by repository name (extracted from repo_path).
 */
export function groupScansByRepo(scans: ScanMeta[]): Record<string, ScanMeta[]> {
  const groups: Record<string, ScanMeta[]> = {};
  for (const scan of scans) {
    const parts = scan.repo_path.split("/");
    const repoName = parts[parts.length - 1] || scan.repo_path;
    if (!groups[repoName]) groups[repoName] = [];
    groups[repoName].push(scan);
  }
  return groups;
}
