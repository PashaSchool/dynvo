import { mockBilling } from "../mock/data";
import { Check, ExternalLink } from "lucide-react";

const PLANS = [
  {
    name: "free",
    label: "Free",
    price: 0,
    description: "For open source projects",
    features: [
      "Public repos only",
      "Basic feature detection",
      "CLI access",
      "Community support",
    ],
  },
  {
    name: "team",
    label: "Team",
    price: 22,
    description: "For growing engineering teams",
    features: [
      "Private repos",
      "LLM feature detection",
      "Flow analysis",
      "GitHub App PR comments",
      "Analytics integrations",
      "Impact scores",
      "Up to 10 seats",
      "Email support",
    ],
  },
  {
    name: "business",
    label: "Business",
    price: 38,
    description: "For organizations that need more control",
    features: [
      "Everything in Team",
      "BYOK (Bring Your Own Key)",
      "SSO / SAML",
      "Audit logs",
      "Custom analysis schedules",
      "Priority support",
      "Up to 50 seats",
    ],
  },
  {
    name: "enterprise",
    label: "Enterprise",
    price: -1,
    description: "For large organizations with custom needs",
    features: [
      "Everything in Business",
      "Self-hosted option",
      "Custom integrations",
      "Dedicated account manager",
      "SLA guarantees",
      "Unlimited seats",
      "On-premise Ollama support",
    ],
  },
];

export default function BillingPage() {
  const billing = mockBilling;
  const daysLeft = Math.ceil(
    (new Date(billing.current_period_end).getTime() - Date.now()) / (1000 * 60 * 60 * 24)
  );

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">Billing</h1>
        <p className="page-subtitle">Manage your subscription and usage</p>
      </div>

      {/* Current plan */}
      <div className="card mb-4">
        <div className="card-header">
          <div>
            <div className="card-title">Current Plan</div>
            <div className="card-desc">
              {billing.seats_used} of {billing.seats_total} seats used
            </div>
          </div>
          <span className="impact-badge impact-healthy" style={{ textTransform: "capitalize" }}>
            {billing.name} Plan
          </span>
        </div>
        <div className="card-body">
          <div className="stats-grid" style={{ marginBottom: 0 }}>
            <div className="stat-card">
              <div className="stat-card-label">Monthly Cost</div>
              <div className="stat-card-value">
                ${billing.price_per_seat * billing.seats_used}
              </div>
              <div className="stat-card-meta">
                ${billing.price_per_seat}/seat x {billing.seats_used} seats
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-card-label">Next Billing</div>
              <div className="stat-card-value">{daysLeft}d</div>
              <div className="stat-card-meta">
                {new Date(billing.current_period_end).toLocaleDateString()}
              </div>
            </div>
            <div className="stat-card">
              <div className="stat-card-label">Seats Available</div>
              <div className="stat-card-value">
                {billing.seats_total - billing.seats_used}
              </div>
              <div className="stat-card-meta">
                of {billing.seats_total} total
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Plans comparison */}
      <h2 style={{ fontSize: 16, fontWeight: 600, marginBottom: 16 }}>Plans</h2>
      <div style={{
        display: "grid",
        gridTemplateColumns: "repeat(4, 1fr)",
        gap: 16,
        marginBottom: 24,
      }}>
        {PLANS.map((plan) => {
          const isCurrent = plan.name === billing.name;
          return (
            <div
              key={plan.name}
              className="card"
              style={{
                borderColor: isCurrent ? "var(--primary)" : undefined,
                borderWidth: isCurrent ? 2 : 1,
              }}
            >
              <div className="card-body" style={{ display: "flex", flexDirection: "column", height: "100%" }}>
                <div>
                  <div style={{ fontSize: 15, fontWeight: 700 }}>{plan.label}</div>
                  <div className="text-sm text-muted" style={{ marginTop: 2 }}>{plan.description}</div>

                  <div style={{ margin: "16px 0" }}>
                    {plan.price === -1 ? (
                      <div style={{ fontSize: 22, fontWeight: 700 }}>Custom</div>
                    ) : plan.price === 0 ? (
                      <div style={{ fontSize: 22, fontWeight: 700 }}>Free</div>
                    ) : (
                      <div>
                        <span style={{ fontSize: 28, fontWeight: 700 }}>${plan.price}</span>
                        <span className="text-muted text-sm"> /dev/mo</span>
                      </div>
                    )}
                  </div>

                  <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                    {plan.features.map((f) => (
                      <li key={f} style={{
                        display: "flex",
                        alignItems: "flex-start",
                        gap: 6,
                        padding: "4px 0",
                        fontSize: 12,
                        color: "var(--text-secondary)",
                      }}>
                        <Check size={14} color="var(--green)" style={{ flexShrink: 0, marginTop: 1 }} />
                        {f}
                      </li>
                    ))}
                  </ul>
                </div>

                <div style={{ marginTop: "auto", paddingTop: 16 }}>
                  {isCurrent ? (
                    <button className="btn btn-secondary" style={{ width: "100%" }} disabled>
                      Current Plan
                    </button>
                  ) : plan.price === -1 ? (
                    <button className="btn btn-secondary" style={{ width: "100%" }}>
                      Contact Sales
                    </button>
                  ) : (
                    <button className="btn btn-primary" style={{ width: "100%" }}>
                      {plan.price > (billing.price_per_seat || 0) ? "Upgrade" : "Switch"}
                    </button>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Invoice history placeholder */}
      <div className="card">
        <div className="card-header">
          <div className="card-title">Invoice History</div>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Date</th>
              <th>Description</th>
              <th>Amount</th>
              <th>Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {[
              { date: "Mar 25, 2026", desc: "Team Plan — 4 seats", amount: "$88.00", status: "Paid" },
              { date: "Feb 25, 2026", desc: "Team Plan — 3 seats", amount: "$66.00", status: "Paid" },
              { date: "Jan 25, 2026", desc: "Team Plan — 3 seats", amount: "$66.00", status: "Paid" },
            ].map((inv, i) => (
              <tr key={i}>
                <td>{inv.date}</td>
                <td>{inv.desc}</td>
                <td className="font-semibold">{inv.amount}</td>
                <td>
                  <span className="impact-badge impact-healthy">{inv.status}</span>
                </td>
                <td>
                  <button className="btn-icon"><ExternalLink size={14} /></button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
