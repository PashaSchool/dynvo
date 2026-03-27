import { createContext, useContext, useState, useEffect, useRef } from "react";
import { useScans, useScan, groupScansByRepo } from "./useScans";
import type { ScanMeta } from "../api";
import type { FeatureMap } from "../types";

interface ScanContextValue {
  scans: ScanMeta[];
  scansByRepo: Record<string, ScanMeta[]>;
  selectedFilename: string | null;
  selectedScan: FeatureMap | null;
  isLoadingList: boolean;
  isLoadingScan: boolean;
  selectScan: (filename: string) => void;
}

const ScanContext = createContext<ScanContextValue | null>(null);

export function ScanProvider({ children }: { children: React.ReactNode }) {
  const { scans, isLoading: isLoadingList, hasNewScan } = useScans();
  const [selectedFilename, setSelectedFilename] = useState<string | null>(null);
  const { scan: selectedScan, isLoading: isLoadingScan } = useScan(selectedFilename);
  const prevLatestRef = useRef<string | null>(null);

  // Auto-select latest scan on first load
  useEffect(() => {
    if (scans.length > 0 && !selectedFilename) {
      setSelectedFilename(scans[0].filename);
      prevLatestRef.current = scans[0].filename;
    }
  }, [scans, selectedFilename]);

  // Auto-switch to new scan when one appears
  useEffect(() => {
    if (hasNewScan && scans.length > 0 && scans[0].filename !== prevLatestRef.current) {
      setSelectedFilename(scans[0].filename);
      prevLatestRef.current = scans[0].filename;
    }
  }, [hasNewScan, scans]);

  const scansByRepo = groupScansByRepo(scans);

  return (
    <ScanContext.Provider value={{
      scans,
      scansByRepo,
      selectedFilename,
      selectedScan,
      isLoadingList,
      isLoadingScan,
      selectScan: setSelectedFilename,
    }}>
      {children}
    </ScanContext.Provider>
  );
}

export function useScanContext(): ScanContextValue {
  const ctx = useContext(ScanContext);
  if (!ctx) throw new Error("useScanContext must be used within ScanProvider");
  return ctx;
}
