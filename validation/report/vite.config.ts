import fs from "node:fs";
import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const VALIDATION_DIR =
  process.env.VALIDATION_DIR ??
  path.join(process.env.HOME ?? process.env.USERPROFILE ?? ".", ".faultline", "validation");

function validationPlugin() {
  return {
    name: "validation-progress",
    configureServer(server: import("vite").ViteDevServer): void {
      server.middlewares.use("/api", (req, res) => {
        res.setHeader("Content-Type", "application/json");
        const progressPath = path.join(VALIDATION_DIR, "progress.json");
        try {
          if (!fs.existsSync(progressPath)) {
            res.end(JSON.stringify(null));
            return;
          }
          res.end(fs.readFileSync(progressPath, "utf-8"));
        } catch {
          res.statusCode = 500;
          res.end(JSON.stringify({ error: "Failed to read progress" }));
        }
      });
    },
  };
}

export default defineConfig({
  plugins: [react(), validationPlugin()],
  server: { port: 5174 },
});
