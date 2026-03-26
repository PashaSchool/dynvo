import { useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import OrgSwitcher from "./OrgSwitcher";
import {
  LayoutDashboard,
  Layers,
  Zap,
  Plug,
  Github,
  Settings,
  Key,
  Users,
  CreditCard,
} from "lucide-react";

interface NavItem {
  label: string;
  path: string;
  icon: React.ReactNode;
  badge?: string;
}

const mainNav: NavItem[] = [
  { label: "Overview", path: "/overview", icon: <LayoutDashboard size={16} /> },
  { label: "Features", path: "/features", icon: <Layers size={16} /> },
  { label: "Impact Scores", path: "/impact", icon: <Zap size={16} />, badge: "New" },
];

const configNav: NavItem[] = [
  { label: "Integrations", path: "/integrations", icon: <Plug size={16} /> },
  { label: "GitHub App", path: "/github-app", icon: <Github size={16} /> },
];

const settingsNav: NavItem[] = [
  { label: "Project", path: "/settings", icon: <Settings size={16} /> },
  { label: "API Keys", path: "/settings/api-keys", icon: <Key size={16} /> },
  { label: "Team", path: "/settings/team", icon: <Users size={16} /> },
  { label: "Billing", path: "/settings/billing", icon: <CreditCard size={16} /> },
];

export default function Sidebar() {
  const location = useLocation();
  const navigate = useNavigate();
  const { user } = useAuth();

  const renderItems = (items: NavItem[]) =>
    items.map((item) => (
      <button
        key={item.path}
        className={`sb-item ${location.pathname === item.path ? "active" : ""}`}
        onClick={() => navigate(item.path)}
      >
        <span className="sb-item-icon">{item.icon}</span>
        {item.label}
        {item.badge && <span className="sb-badge">{item.badge}</span>}
      </button>
    ));

  const initials = user?.name
    ? user.name.split(" ").map((w) => w[0]).join("").slice(0, 2)
    : "?";

  return (
    <aside className="app-sidebar">
      <div className="sb-logo">
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
          <div className="auth-logo-icon" style={{ width: 28, height: 28, fontSize: 11, borderRadius: 6 }}>FM</div>
          <span className="sb-logo-text">FeatureMap</span>
        </div>
        <OrgSwitcher />
      </div>

      <nav className="sb-nav">
        <div className="sb-section">
          <div className="sb-section-label">Analysis</div>
          {renderItems(mainNav)}
        </div>

        <div className="sb-section">
          <div className="sb-section-label">Configure</div>
          {renderItems(configNav)}
        </div>

        <div className="sb-section">
          <div className="sb-section-label">Settings</div>
          {renderItems(settingsNav)}
        </div>
      </nav>

      <div className="sb-footer">
        <div className="sb-user">
          <div className="sb-avatar">{initials}</div>
          <div>
            <div className="sb-user-name">{user?.name || "User"}</div>
            <div className="sb-user-plan">{user?.email || ""}</div>
          </div>
        </div>
      </div>
    </aside>
  );
}
