import { useState } from "react";
import { mockApiKeys } from "../mock/data";
import type { ApiKeys } from "../types";
import { Eye, EyeOff, Check, AlertTriangle } from "lucide-react";

export default function ApiKeysPage() {
  const [keys, setKeys] = useState<ApiKeys>(mockApiKeys);
  const [showAnthropicKey, setShowAnthropicKey] = useState(false);
  const [saved, setSaved] = useState(false);

  const handleSave = () => {
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">API Keys</h1>
        <p className="page-subtitle">
          Configure LLM providers for feature detection and flow analysis
        </p>
      </div>

      {/* LLM Provider Selection */}
      <div className="card mb-4">
        <div className="card-header">
          <div className="card-title">LLM Provider</div>
        </div>
        <div className="card-body">
          <div className="form-group">
            <label className="form-label">Primary Provider</label>
            <select
              className="form-select"
              value={keys.provider_preference}
              onChange={(e) => setKeys({ ...keys, provider_preference: e.target.value as "anthropic" | "ollama" })}
            >
              <option value="anthropic">Anthropic (Claude API)</option>
              <option value="ollama">Ollama (Local / Self-hosted)</option>
            </select>
            <div className="form-hint">
              Anthropic is recommended for best accuracy. Ollama is free and keeps code local.
            </div>
          </div>
        </div>
      </div>

      {/* Anthropic Config */}
      <div className="card mb-4">
        <div className="card-header">
          <div>
            <div className="card-title">Anthropic (Claude)</div>
            <div className="card-desc">Used for feature detection, flow analysis, and enrichment</div>
          </div>
          {keys.anthropic_key && (
            <span className="impact-badge impact-healthy">
              <Check size={12} /> Configured
            </span>
          )}
        </div>
        <div className="card-body">
          <div className="form-group">
            <label className="form-label">API Key</label>
            <div style={{ position: "relative" }}>
              <input
                className="form-input form-input-mono"
                type={showAnthropicKey ? "text" : "password"}
                placeholder="sk-ant-api03-..."
                value={keys.anthropic_key}
                onChange={(e) => setKeys({ ...keys, anthropic_key: e.target.value })}
                style={{ paddingRight: 40 }}
              />
              <button
                className="btn-icon"
                style={{ position: "absolute", right: 4, top: 4 }}
                onClick={() => setShowAnthropicKey(!showAnthropicKey)}
              >
                {showAnthropicKey ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
            <div className="form-hint">
              Get your API key from console.anthropic.com. Your key is encrypted at rest.
            </div>
          </div>

          <div className="form-group">
            <label className="form-label">Model</label>
            <select
              className="form-select"
              value={keys.anthropic_model}
              onChange={(e) => setKeys({ ...keys, anthropic_model: e.target.value })}
            >
              <option value="claude-haiku-4-5-20251001">Claude Haiku 4.5 (Fast, cheap — recommended for most tasks)</option>
              <option value="claude-sonnet-4-6-20260320">Claude Sonnet 4.6 (Best accuracy — for complex analysis)</option>
              <option value="claude-opus-4-6-20260320">Claude Opus 4.6 (Most capable — enterprise)</option>
            </select>
            <div className="form-hint">
              Haiku handles 80% of tasks. Sonnet is used for complex feature map generation.
            </div>
          </div>

          <div style={{
            background: "var(--info-subtle)",
            border: "1px solid rgba(9,105,218,0.2)",
            borderRadius: 6,
            padding: 12,
            fontSize: 13,
            color: "var(--info)",
          }}>
            <strong>Cost estimate:</strong> ~$0.02 per analysis (Haiku) or ~$0.15 per analysis (Sonnet)
            for a repo with 500 files. FeatureMap uses Batch API for 50% discount on all requests.
          </div>
        </div>
      </div>

      {/* Ollama Config */}
      <div className="card mb-4">
        <div className="card-header">
          <div>
            <div className="card-title">Ollama (Local)</div>
            <div className="card-desc">Run analysis locally — your code never leaves your machine</div>
          </div>
        </div>
        <div className="card-body">
          <div className="form-row">
            <div className="form-group">
              <label className="form-label">Ollama URL</label>
              <input
                className="form-input form-input-mono"
                placeholder="http://localhost:11434"
                value={keys.ollama_url}
                onChange={(e) => setKeys({ ...keys, ollama_url: e.target.value })}
              />
            </div>
            <div className="form-group">
              <label className="form-label">Model</label>
              <input
                className="form-input"
                placeholder="llama3.1:8b"
                value={keys.ollama_model}
                onChange={(e) => setKeys({ ...keys, ollama_model: e.target.value })}
              />
            </div>
          </div>
          <div className="form-hint">
            Requires Ollama running locally. Recommended model: llama3.1:8b (best for semantic grouping).
          </div>

          <div style={{
            marginTop: 12,
            background: "var(--attention-subtle)",
            border: "1px solid rgba(154,103,0,0.2)",
            borderRadius: 6,
            padding: 12,
            fontSize: 13,
            display: "flex",
            gap: 8,
            alignItems: "flex-start",
            color: "var(--attention)",
          }}>
            <AlertTriangle size={16} color="var(--attention)" style={{ flexShrink: 0, marginTop: 2 }} />
            <span>
              Ollama accuracy is lower than Claude for feature detection.
              Best for private repos where sending code to cloud APIs is not an option.
            </span>
          </div>
        </div>
      </div>

      {/* BYOK info */}
      <div className="card">
        <div className="card-header">
          <div className="card-title">Enterprise: Bring Your Own Key (BYOK)</div>
        </div>
        <div className="card-body">
          <p className="text-sm text-muted">
            On Business and Enterprise plans, each team member can use their own API key.
            Keys are encrypted per-user and never shared across the team.
            Supported providers: Anthropic, OpenAI (GPT-4o), Azure OpenAI.
          </p>
        </div>
      </div>

      {/* Save button */}
      <div style={{ marginTop: 20, display: "flex", gap: 8 }}>
        <button className="btn btn-primary" onClick={handleSave}>
          {saved ? <><Check size={14} /> Saved</> : "Save Changes"}
        </button>
        <button className="btn btn-secondary">Test Connection</button>
      </div>
    </div>
  );
}
