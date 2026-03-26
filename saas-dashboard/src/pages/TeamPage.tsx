import { useState } from "react";
import { mockTeamMembers } from "../mock/data";
import type { TeamMember } from "../types";
import { UserPlus, MoreVertical, Shield, Eye, Edit3 } from "lucide-react";

const ROLE_LABELS: Record<string, { label: string; color: string; bg: string }> = {
  owner: { label: "Owner", color: "var(--done)", bg: "var(--done-subtle)" },
  admin: { label: "Admin", color: "var(--severe)", bg: "var(--severe-subtle)" },
  member: { label: "Member", color: "var(--success)", bg: "var(--success-subtle)" },
  viewer: { label: "Viewer", color: "var(--text-secondary)", bg: "var(--bg-inset)" },
};

export default function TeamPage() {
  const [members] = useState<TeamMember[]>(mockTeamMembers);
  const [showInvite, setShowInvite] = useState(false);

  return (
    <div className="page">
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start" }}>
        <div>
          <h1 className="page-title">Team</h1>
          <p className="page-subtitle">
            {members.length} members &middot; Manage roles and permissions
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => setShowInvite(true)}>
          <UserPlus size={16} />
          Invite Member
        </button>
      </div>

      {/* Roles explanation */}
      <div className="card mb-4">
        <div className="card-header">
          <div className="card-title">Role Permissions</div>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Permission</th>
              <th>Viewer</th>
              <th>Member</th>
              <th>Admin</th>
              <th>Owner</th>
            </tr>
          </thead>
          <tbody>
            {[
              ["View feature reports", true, true, true, true],
              ["View impact scores", true, true, true, true],
              ["Run manual analysis", false, true, true, true],
              ["Configure integrations", false, false, true, true],
              ["Manage GitHub App", false, false, true, true],
              ["Change project settings", false, false, true, true],
              ["Manage team members", false, false, true, true],
              ["Manage billing", false, false, false, true],
              ["Delete project", false, false, false, true],
            ].map(([perm, ...vals]) => (
              <tr key={String(perm)}>
                <td style={{ fontWeight: 500 }}>{String(perm)}</td>
                {vals.map((v, i) => (
                  <td key={i} style={{ textAlign: "center" }}>
                    {v ? <span style={{ color: "var(--success)" }}>Y</span> : <span style={{ color: "var(--text-muted)" }}>—</span>}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Members list */}
      <div className="card">
        <div className="card-header">
          <div className="card-title">Members</div>
        </div>
        <table className="data-table">
          <thead>
            <tr>
              <th>Member</th>
              <th>Email</th>
              <th>Role</th>
              <th style={{ width: 48 }}></th>
            </tr>
          </thead>
          <tbody>
            {members.map((m) => {
              const role = ROLE_LABELS[m.role];
              return (
                <tr key={m.id}>
                  <td>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <div style={{
                        width: 32,
                        height: 32,
                        borderRadius: "50%",
                        background: "var(--accent-subtle)",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        fontSize: 13,
                        fontWeight: 600,
                        color: "var(--accent)",
                      }}>
                        {m.name.split(" ").map(n => n[0]).join("")}
                      </div>
                      <span style={{ fontWeight: 500 }}>{m.name}</span>
                    </div>
                  </td>
                  <td className="text-muted">{m.email}</td>
                  <td>
                    <span style={{
                      display: "inline-block",
                      padding: "2px 10px",
                      borderRadius: 12,
                      fontSize: 12,
                      fontWeight: 600,
                      background: role.bg,
                      color: role.color,
                    }}>
                      {role.label}
                    </span>
                  </td>
                  <td>
                    {m.role !== "owner" && (
                      <button className="btn-icon">
                        <MoreVertical size={16} />
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Invite modal */}
      {showInvite && (
        <div className="overlay">
          <div className="modal" style={{ width: 420 }}>
            <div className="card-header">
              <div className="card-title">Invite Team Member</div>
            </div>
            <div className="card-body">
              <div className="form-group">
                <label className="form-label">Email Address</label>
                <input className="form-input" type="email" placeholder="colleague@company.com" />
              </div>
              <div className="form-group">
                <label className="form-label">Role</label>
                <select className="form-select">
                  <option value="viewer">Viewer — Can view reports only</option>
                  <option value="member">Member — Can run analysis</option>
                  <option value="admin">Admin — Full access except billing</option>
                </select>
              </div>
            </div>
            <div className="card-footer">
              <button className="btn btn-secondary" onClick={() => setShowInvite(false)}>Cancel</button>
              <button className="btn btn-primary" onClick={() => setShowInvite(false)}>Send Invite</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
