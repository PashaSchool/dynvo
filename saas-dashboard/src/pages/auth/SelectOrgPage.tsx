import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../../auth/AuthContext";
import { Plus, ArrowRight, Building2, ChevronRight } from "lucide-react";

const PLAN_LABELS: Record<string, { label: string; className: string }> = {
  free: { label: "Free", className: "label-gray" },
  team: { label: "Team", className: "label-purple" },
  business: { label: "Business", className: "label-yellow" },
  enterprise: { label: "Enterprise", className: "label-green" },
};

export default function SelectOrgPage() {
  const { user, organizations, switchOrg, createOrg } = useAuth();
  const navigate = useNavigate();
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newSlug, setNewSlug] = useState("");

  const handleSelect = (orgId: string) => {
    switchOrg(orgId);
    navigate("/overview");
  };

  const handleCreate = (e: React.FormEvent) => {
    e.preventDefault();
    if (!newName.trim()) return;
    const slug = newSlug || newName.toLowerCase().replace(/[^a-z0-9]/g, "-").replace(/-+/g, "-");
    createOrg(newName, slug);
    navigate("/overview");
  };

  const handleNameChange = (value: string) => {
    setNewName(value);
    if (!newSlug || newSlug === newName.toLowerCase().replace(/[^a-z0-9]/g, "-").replace(/-+/g, "-")) {
      setNewSlug(value.toLowerCase().replace(/[^a-z0-9]/g, "-").replace(/-+/g, "-"));
    }
  };

  return (
    <div className="auth-page">
      <div className="auth-container" style={{ maxWidth: 480 }}>
        <div className="auth-logo">
          <div className="auth-logo-icon">FM</div>
          <div className="auth-logo-text">FeatureMap</div>
        </div>

        {!showCreate ? (
          <>
            <div className="auth-card">
              <h1 className="auth-title">Select an organization</h1>
              <p className="auth-subtitle">
                Welcome back, {user?.name?.split(" ")[0]}. Choose a workspace to continue.
              </p>

              <div className="org-list">
                {organizations.map((org) => {
                  const plan = PLAN_LABELS[org.metadata?.plan || "free"];
                  return (
                    <button
                      key={org.id}
                      className="org-item"
                      onClick={() => handleSelect(org.id)}
                    >
                      <div className="org-item-avatar">
                        {org.logo ? (
                          <img src={org.logo} alt="" />
                        ) : (
                          <Building2 size={18} />
                        )}
                      </div>
                      <div className="org-item-info">
                        <div className="org-item-name">{org.name}</div>
                        <div className="org-item-slug">
                          {org.slug}.featuremap.dev
                        </div>
                      </div>
                      <span className={`label ${plan.className}`}>{plan.label}</span>
                      <ChevronRight size={16} className="org-item-arrow" />
                    </button>
                  );
                })}
              </div>
            </div>

            <button
              className="auth-create-org-btn"
              onClick={() => setShowCreate(true)}
            >
              <Plus size={16} />
              Create a new organization
            </button>
          </>
        ) : (
          <div className="auth-card">
            <h1 className="auth-title">Create organization</h1>
            <p className="auth-subtitle">
              Set up a new workspace for your team.
            </p>

            <form onSubmit={handleCreate}>
              <div className="auth-field">
                <label className="auth-label">Organization name</label>
                <input
                  className="auth-input"
                  type="text"
                  value={newName}
                  onChange={(e) => handleNameChange(e.target.value)}
                  placeholder="Acme Corp"
                  autoFocus
                />
              </div>

              <div className="auth-field">
                <label className="auth-label">URL slug</label>
                <div className="auth-slug-wrap">
                  <span className="auth-slug-prefix">featuremap.dev/</span>
                  <input
                    className="auth-input auth-slug-input"
                    type="text"
                    value={newSlug}
                    onChange={(e) => setNewSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))}
                    placeholder="acme"
                  />
                </div>
                <div className="auth-hint">
                  This will be your workspace URL. Only lowercase letters, numbers, and hyphens.
                </div>
              </div>

              <button className="auth-submit" type="submit">
                Create organization
                <ArrowRight size={16} />
              </button>

              <button
                type="button"
                className="auth-back-link"
                onClick={() => setShowCreate(false)}
              >
                Back to organization list
              </button>
            </form>
          </div>
        )}
      </div>
    </div>
  );
}
