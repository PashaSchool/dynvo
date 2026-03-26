import { useState } from "react";
import Toggle from "../components/Toggle";
import { mockProjectSettings } from "../mock/data";
import type { ProjectSettings } from "../types";
import { Check } from "lucide-react";

export default function ProjectSettingsPage() {
  const [settings, setSettings] = useState<ProjectSettings>(mockProjectSettings);
  const [saved, setSaved] = useState(false);

  const update = <K extends keyof ProjectSettings>(key: K, value: ProjectSettings[K]) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
  };

  const handleSave = () => {
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">Project Settings</h1>
        <p className="page-subtitle">
          Configure analysis behavior for {settings.repo_name}
        </p>
      </div>

      {/* Repository */}
      <div className="card mb-4">
        <div className="card-header">
          <div className="card-title">Repository</div>
        </div>
        <div className="card-body">
          <div className="form-row">
            <div className="form-group">
              <label className="form-label">Repository</label>
              <input
                className="form-input"
                value={settings.repo_name}
                onChange={(e) => update("repo_name", e.target.value)}
              />
            </div>
            <div className="form-group">
              <label className="form-label">Default Branch</label>
              <input
                className="form-input"
                value={settings.default_branch}
                onChange={(e) => update("default_branch", e.target.value)}
              />
            </div>
          </div>

          <div className="form-group">
            <label className="form-label">Source Path</label>
            <input
              className="form-input form-input-mono"
              placeholder="src/"
              value={settings.src_path}
              onChange={(e) => update("src_path", e.target.value)}
            />
            <div className="form-hint">
              Subdirectory to focus analysis on. Leave empty to analyze entire repo. E.g. "src/" for frontend projects.
            </div>
          </div>
        </div>
      </div>

      {/* Analysis Config */}
      <div className="card mb-4">
        <div className="card-header">
          <div className="card-title">Analysis Configuration</div>
        </div>
        <div className="card-body">
          <div className="form-row">
            <div className="form-group">
              <label className="form-label">Analysis Window (days)</label>
              <select
                className="form-select"
                value={settings.analysis_days}
                onChange={(e) => update("analysis_days", parseInt(e.target.value))}
              >
                <option value={90}>90 days (3 months)</option>
                <option value={180}>180 days (6 months)</option>
                <option value={365}>365 days (1 year)</option>
                <option value={730}>730 days (2 years)</option>
              </select>
            </div>
            <div className="form-group">
              <label className="form-label">Max Commits</label>
              <input
                className="form-input"
                type="number"
                min={100}
                max={50000}
                value={settings.max_commits}
                onChange={(e) => update("max_commits", parseInt(e.target.value) || 5000)}
              />
              <div className="form-hint">Cap for very large repos</div>
            </div>
          </div>

          <Toggle
            title="LLM Feature Detection"
            description="Use Claude/Ollama to intelligently name and group features instead of directory-based heuristics"
            checked={settings.llm_enabled}
            onChange={(v) => update("llm_enabled", v)}
          />

          <Toggle
            title="Flow Detection"
            description="Detect user-facing flows within each feature (requires LLM). Provides deeper analysis but takes longer."
            checked={settings.flows_enabled}
            onChange={(v) => update("flows_enabled", v)}
          />
        </div>
      </div>

      {/* Automation */}
      <div className="card mb-4">
        <div className="card-header">
          <div className="card-title">Automation</div>
        </div>
        <div className="card-body">
          <Toggle
            title="Auto-analyze on push to main"
            description="Run analysis automatically when commits are pushed to the default branch"
            checked={settings.auto_analyze_on_push}
            onChange={(v) => update("auto_analyze_on_push", v)}
          />

          <Toggle
            title="Auto-analyze on PR"
            description="Run analysis and post comment when a PR is opened or updated"
            checked={settings.auto_analyze_on_pr}
            onChange={(v) => update("auto_analyze_on_pr", v)}
          />

          <div className="form-group mt-4">
            <label className="form-label">Scheduled Analysis (Cron)</label>
            <input
              className="form-input form-input-mono"
              placeholder="0 2 * * 1"
              value={settings.schedule_cron}
              onChange={(e) => update("schedule_cron", e.target.value)}
            />
            <div className="form-hint">
              Current: Every Monday at 2:00 AM UTC.
              Uses standard cron syntax: minute hour day-of-month month day-of-week.
            </div>
          </div>
        </div>
      </div>

      {/* Danger Zone */}
      <div className="card" style={{ borderColor: "rgba(255,129,130,0.5)" }}>
        <div className="card-header">
          <div className="card-title" style={{ color: "var(--danger)" }}>Danger Zone</div>
        </div>
        <div className="card-body">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <div className="font-semibold">Delete this project</div>
              <div className="text-sm text-muted">Remove all analysis data and disconnect integrations</div>
            </div>
            <button className="btn btn-danger">Delete Project</button>
          </div>
        </div>
      </div>

      {/* Save */}
      <div style={{ marginTop: 20 }}>
        <button className="btn btn-primary" onClick={handleSave}>
          {saved ? <><Check size={14} /> Saved</> : "Save Changes"}
        </button>
      </div>
    </div>
  );
}
