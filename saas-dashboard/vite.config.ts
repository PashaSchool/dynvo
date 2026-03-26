import fs from "node:fs";
import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * Directory containing .faultline/*.json scan files.
 * Default: ~/.faultline
 */
const DATA_DIR =
  process.env.FAULTLINE_DIR ??
  path.join(process.env.HOME ?? process.env.USERPROFILE ?? ".", ".faultline");

function featureMapsPlugin() {
  return {
    name: "feature-maps",
    configureServer(server: import("vite").ViteDevServer): void {
      server.middlewares.use("/api", (req, res, next) => {
        const url = req.url ?? "/";
        res.setHeader("Content-Type", "application/json");

        // GET /api/scans — list all scan metadata
        if (url === "/scans" || url === "/scans/") {
          try {
            if (!fs.existsSync(DATA_DIR)) {
              res.end(JSON.stringify([]));
              return;
            }
            const scans = fs
              .readdirSync(DATA_DIR)
              .filter((f) => f.endsWith(".json"))
              .flatMap((filename) => {
                try {
                  const raw = fs.readFileSync(path.join(DATA_DIR, filename), "utf-8");
                  const data = JSON.parse(raw) as Record<string, unknown>;
                  const features = (data.features as unknown[]) ?? [];
                  return [{
                    filename,
                    repo_path: data.repo_path ?? "",
                    remote_url: data.remote_url ?? "",
                    analyzed_at: data.analyzed_at ?? "",
                    total_commits: data.total_commits ?? 0,
                    date_range_days: data.date_range_days ?? 0,
                    feature_count: features.length,
                  }];
                } catch {
                  return [];
                }
              })
              .sort(
                (a, b) =>
                  new Date(b.analyzed_at as string).getTime() -
                  new Date(a.analyzed_at as string).getTime()
              );
            res.end(JSON.stringify(scans));
          } catch (e) {
            res.statusCode = 500;
            res.end(JSON.stringify({ error: String(e) }));
          }
          return;
        }

        // GET /api/scans/:filename — full scan data
        if (url.startsWith("/scans/")) {
          const filename = url.slice("/scans/".length);
          const filePath = path.join(DATA_DIR, filename);
          try {
            res.end(fs.readFileSync(filePath, "utf-8"));
          } catch {
            res.statusCode = 404;
            res.end(JSON.stringify({ error: "Not found" }));
          }
          return;
        }

        next();
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), featureMapsPlugin()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
});
