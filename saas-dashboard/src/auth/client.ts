/**
 * Better Auth — client configuration.
 *
 * In production, this connects to the Better Auth server endpoints.
 * For the prototype, we use a mock auth context instead.
 *
 * Production usage:
 *   import { createAuthClient } from "better-auth/react";
 *   import { organizationClient } from "better-auth/client/plugins";
 *
 *   export const authClient = createAuthClient({
 *     baseURL: import.meta.env.VITE_AUTH_URL || "http://localhost:3000",
 *     plugins: [organizationClient()],
 *   });
 *
 *   // Then in components:
 *   const { data: session } = authClient.useSession();
 *   const { data: org } = authClient.useActiveOrganization();
 */

export interface User {
  id: string;
  name: string;
  email: string;
  image?: string;
}

export interface Organization {
  id: string;
  name: string;
  slug: string;
  logo?: string;
  metadata?: {
    plan: "free" | "team" | "business" | "enterprise";
    seats_total: number;
    repo_url?: string;
  };
}

export interface Member {
  id: string;
  userId: string;
  organizationId: string;
  role: "owner" | "admin" | "member" | "viewer";
  user: User;
}

export interface Session {
  user: User;
  activeOrganization: Organization | null;
  organizations: Organization[];
  role: "owner" | "admin" | "member" | "viewer";
}
