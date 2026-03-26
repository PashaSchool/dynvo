import { useState, useRef, useEffect } from "react";
import { useAuth } from "../auth/AuthContext";
import { useNavigate } from "react-router-dom";
import { ChevronDown, Check, Plus, Settings, LogOut } from "lucide-react";

export default function OrgSwitcher() {
  const { activeOrg, organizations, switchOrg, signOut } = useAuth();
  const navigate = useNavigate();
  const [isOpen, setIsOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  if (!activeOrg) return null;

  const initials = activeOrg.name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <div className="org-switcher" ref={ref}>
      <button
        className="org-switcher-btn"
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
      >
        <div className="org-switcher-avatar">{initials}</div>
        <span className="org-switcher-name">{activeOrg.name}</span>
        <ChevronDown size={14} className="org-switcher-chevron" />
      </button>

      {isOpen && (
        <div className="org-switcher-dropdown">
          {organizations.map((org) => (
            <button
              key={org.id}
              className={`org-switcher-option ${org.id === activeOrg.id ? "active" : ""}`}
              onClick={() => {
                switchOrg(org.id);
                setIsOpen(false);
              }}
            >
              <span className="org-switcher-option-check">
                {org.id === activeOrg.id && <Check size={14} />}
              </span>
              {org.name}
            </button>
          ))}

          <div className="org-switcher-divider" />

          <button
            className="org-switcher-option"
            onClick={() => {
              setIsOpen(false);
              navigate("/select-org");
            }}
          >
            <span className="org-switcher-option-check">
              <Plus size={14} />
            </span>
            Create organization
          </button>

          <button
            className="org-switcher-option"
            onClick={() => {
              setIsOpen(false);
              navigate("/settings");
            }}
          >
            <span className="org-switcher-option-check">
              <Settings size={14} />
            </span>
            Organization settings
          </button>

          <div className="org-switcher-divider" />

          <button
            className="org-switcher-option"
            onClick={() => {
              signOut();
              navigate("/login");
              setIsOpen(false);
            }}
          >
            <span className="org-switcher-option-check">
              <LogOut size={14} />
            </span>
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
