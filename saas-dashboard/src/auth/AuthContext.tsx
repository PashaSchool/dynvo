import { createContext, useContext, useState, useCallback } from "react";
import type { Session, Organization, User } from "./client";

/* ── Mock data (replace with Better Auth client in production) ── */

const MOCK_USER: User = {
  id: "u_1",
  name: "Pasha Kuzina",
  email: "pasha@acme.com",
  image: "",
};

const MOCK_ORGS: Organization[] = [
  {
    id: "org_1",
    name: "Acme Corp",
    slug: "acme",
    metadata: { plan: "team", seats_total: 10, repo_url: "https://github.com/acme/ecommerce-platform" },
  },
  {
    id: "org_2",
    name: "Side Project",
    slug: "side-project",
    metadata: { plan: "free", seats_total: 1 },
  },
  {
    id: "org_3",
    name: "Consulting Client",
    slug: "consulting",
    metadata: { plan: "business", seats_total: 25 },
  },
];

/* ── Context ── */

interface AuthContextValue {
  isAuthenticated: boolean;
  isLoading: boolean;
  user: User | null;
  activeOrg: Organization | null;
  organizations: Organization[];
  role: "owner" | "admin" | "member" | "viewer";
  signIn: (provider: "github" | "google" | "credentials") => void;
  signOut: () => void;
  switchOrg: (orgId: string) => void;
  createOrg: (name: string, slug: string) => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  // TODO: replace with Better Auth client in production
  // Auto-login for development — skip login/org-select pages
  const [isAuthenticated, setIsAuthenticated] = useState(true);
  const [isLoading, setIsLoading] = useState(false);
  const [user, setUser] = useState<User | null>(MOCK_USER);
  const [activeOrg, setActiveOrg] = useState<Organization | null>(MOCK_ORGS[0]);
  const [organizations, setOrganizations] = useState<Organization[]>(MOCK_ORGS);

  const signIn = useCallback((provider: "github" | "google" | "credentials") => {
    setIsLoading(true);
    // Simulate auth delay
    setTimeout(() => {
      setUser(MOCK_USER);
      setOrganizations(MOCK_ORGS);
      setActiveOrg(MOCK_ORGS[0]);
      setIsAuthenticated(true);
      setIsLoading(false);
    }, 600);
  }, []);

  const signOut = useCallback(() => {
    setUser(null);
    setActiveOrg(null);
    setOrganizations([]);
    setIsAuthenticated(false);
  }, []);

  const switchOrg = useCallback((orgId: string) => {
    const org = organizations.find((o) => o.id === orgId);
    if (org) setActiveOrg(org);
  }, [organizations]);

  const createOrg = useCallback((name: string, slug: string) => {
    const newOrg: Organization = {
      id: `org_${Date.now()}`,
      name,
      slug,
      metadata: { plan: "free", seats_total: 1 },
    };
    setOrganizations((prev) => [...prev, newOrg]);
    setActiveOrg(newOrg);
  }, []);

  return (
    <AuthContext.Provider value={{
      isAuthenticated,
      isLoading,
      user,
      activeOrg,
      organizations,
      role: "owner",
      signIn,
      signOut,
      switchOrg,
      createOrg,
    }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
