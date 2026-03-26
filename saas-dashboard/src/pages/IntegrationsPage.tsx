import { useState } from "react";
import { mockAnalyticsConnections } from "../mock/data";
import type { AnalyticsConnection, AnalyticsProviderType } from "../types";
import { CheckCircle, Circle, ExternalLink, X } from "lucide-react";

const PROVIDER_INFO: Record<AnalyticsProviderType, { label: string; icon: string; description: string }> = {
  posthog: {
    label: "PostHog",
    icon: "PH",
    description: "Product analytics, error tracking, session replays, and feature flags",
  },
  sentry: {
    label: "Sentry",
    icon: "S",
    description: "Error monitoring with stack traces, performance, and issue tracking",
  },
  ga4: {
    label: "Google Analytics 4",
    icon: "GA",
    description: "Web analytics — pageviews, sessions, user behavior",
  },
  amplitude: {
    label: "Amplitude",
    icon: "A",
    description: "Product analytics for growth — funnels, retention, user journeys",
  },
  mixpanel: {
    label: "Mixpanel",
    icon: "MP",
    description: "Event-based analytics — user engagement, A/B testing",
  },
  plausible: {
    label: "Plausible",
    icon: "PL",
    description: "Privacy-friendly web analytics — lightweight, open source, GDPR compliant",
  },
};

export default function IntegrationsPage() {
  const [connections, setConnections] = useState(mockAnalyticsConnections);
  const [editingProvider, setEditingProvider] = useState<AnalyticsProviderType | null>(null);
  const [formData, setFormData] = useState({ api_key: "", project_id: "", host: "", organization: "" });

  const handleConnect = (provider: AnalyticsProviderType) => {
    const conn = connections.find((c) => c.provider === provider);
    setFormData({
      api_key: "",
      project_id: conn?.project_id || "",
      host: conn?.host || PROVIDER_INFO[provider].label.includes("Google") ? "https://analyticsdata.googleapis.com" : "",
      organization: conn?.organization || "",
    });
    setEditingProvider(provider);
  };

  const handleSave = () => {
    if (!editingProvider) return;
    setConnections((prev) =>
      prev.map((c) =>
        c.provider === editingProvider
          ? {
              ...c,
              is_connected: true,
              api_key: formData.api_key ? `${formData.api_key.slice(0, 8)}...redacted` : c.api_key,
              project_id: formData.project_id || c.project_id,
              host: formData.host || c.host,
              organization: formData.organization || c.organization,
              last_synced: new Date().toISOString(),
            }
          : c
      )
    );
    setEditingProvider(null);
  };

  const handleDisconnect = (provider: AnalyticsProviderType) => {
    setConnections((prev) =>
      prev.map((c) =>
        c.provider === provider
          ? { ...c, is_connected: false, api_key: "", project_id: "", last_synced: undefined }
          : c
      )
    );
  };

  const connectedCount = connections.filter((c) => c.is_connected).length;

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">Integrations</h1>
        <p className="page-subtitle">
          Connect analytics providers to power Impact Scores.
          {connectedCount > 0 && ` ${connectedCount} connected.`}
        </p>
      </div>

      <div className="integrations-grid">
        {connections.map((conn) => {
          const info = PROVIDER_INFO[conn.provider];
          return (
            <div
              key={conn.provider}
              className={`integration-card ${conn.is_connected ? "connected" : ""}`}
            >
              <div className="integration-header">
                <div className={`integration-icon ${conn.provider}`}>
                  {info.icon}
                </div>
                <div style={{ flex: 1 }}>
                  <div className="integration-name">{info.label}</div>
                  <div className="integration-status">
                    <span className={`status-dot ${conn.is_connected ? "connected" : "disconnected"}`} />
                    {conn.is_connected ? "Connected" : "Not connected"}
                  </div>
                </div>
              </div>

              <div className="text-sm text-muted">{info.description}</div>

              {conn.is_connected && (
                <div className="integration-meta">
                  Project: {conn.project_id}
                  {conn.organization && ` / ${conn.organization}`}
                  <br />
                  Last synced: {conn.last_synced ? new Date(conn.last_synced).toLocaleString() : "Never"}
                </div>
              )}

              <div style={{ marginTop: 16, display: "flex", gap: 8 }}>
                {conn.is_connected ? (
                  <>
                    <button className="btn btn-secondary btn-sm" onClick={() => handleConnect(conn.provider)}>
                      Configure
                    </button>
                    <button className="btn btn-danger btn-sm" onClick={() => handleDisconnect(conn.provider)}>
                      Disconnect
                    </button>
                  </>
                ) : (
                  <button className="btn btn-primary btn-sm" onClick={() => handleConnect(conn.provider)}>
                    Connect
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* Connection Modal */}
      {editingProvider && (
        <div className="overlay">
          <div className="modal" style={{ width: 480 }}>
            <div className="card-header">
              <div className="card-title">
                Connect {PROVIDER_INFO[editingProvider].label}
              </div>
              <button className="btn-icon" onClick={() => setEditingProvider(null)}>
                <X size={18} />
              </button>
            </div>
            <div className="card-body">
              <div className="form-group">
                <label className="form-label">API Key</label>
                <input
                  className="form-input form-input-mono"
                  type="password"
                  placeholder={editingProvider === "posthog" ? "phx_..." : editingProvider === "sentry" ? "sntrys_..." : "Enter API key"}
                  value={formData.api_key}
                  onChange={(e) => setFormData({ ...formData, api_key: e.target.value })}
                />
                <div className="form-hint">
                  Your API key is encrypted and never stored in plain text.
                </div>
              </div>

              <div className="form-group">
                <label className="form-label">Project ID</label>
                <input
                  className="form-input"
                  placeholder="e.g. 12345 or my-project"
                  value={formData.project_id}
                  onChange={(e) => setFormData({ ...formData, project_id: e.target.value })}
                />
              </div>

              {(editingProvider === "sentry") && (
                <div className="form-group">
                  <label className="form-label">Organization</label>
                  <input
                    className="form-input"
                    placeholder="e.g. my-org"
                    value={formData.organization}
                    onChange={(e) => setFormData({ ...formData, organization: e.target.value })}
                  />
                </div>
              )}

              <div className="form-group">
                <label className="form-label">Host URL</label>
                <input
                  className="form-input form-input-mono"
                  placeholder="https://app.posthog.com"
                  value={formData.host}
                  onChange={(e) => setFormData({ ...formData, host: e.target.value })}
                />
                <div className="form-hint">
                  Use custom URL for self-hosted instances.
                </div>
              </div>
            </div>
            <div className="card-footer">
              <button className="btn btn-secondary" onClick={() => setEditingProvider(null)}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleSave}>
                Save & Connect
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
