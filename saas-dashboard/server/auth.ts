/**
 * Better Auth — server configuration.
 *
 * This file runs on the backend (Next.js API route, Express, Hono, etc.).
 * It defines auth strategies, the organization plugin, and DB adapter.
 *
 * Usage with Next.js:
 *   // app/api/auth/[...all]/route.ts
 *   import { auth } from "@/server/auth";
 *   export const { GET, POST } = auth.handler;
 *
 * Usage with Express/Hono:
 *   import { auth } from "./server/auth";
 *   app.all("/api/auth/*", auth.handler);
 */

import { betterAuth } from "better-auth";
import { organization } from "better-auth/plugins";
import { Pool } from "pg";

export const auth = betterAuth({
  /* ── Database ─────────────────────────────── */
  database: new Pool({
    connectionString: process.env.DATABASE_URL,
    // e.g. "postgresql://user:pass@localhost:5432/featuremap"
  }),

  /* ── Email + Password ─────────────────────── */
  emailAndPassword: {
    enabled: true,
    minPasswordLength: 8,
  },

  /* ── Social Providers ─────────────────────── */
  socialProviders: {
    github: {
      clientId: process.env.GITHUB_CLIENT_ID!,
      clientSecret: process.env.GITHUB_CLIENT_SECRET!,
      // Scopes: read:user, user:email — enough for auth
      // repo scope is NOT needed for auth, only for GitHub App
    },
    google: {
      clientId: process.env.GOOGLE_CLIENT_ID!,
      clientSecret: process.env.GOOGLE_CLIENT_SECRET!,
    },
  },

  /* ── Session ──────────────────────────────── */
  session: {
    expiresIn: 60 * 60 * 24 * 7, // 7 days
    updateAge: 60 * 60 * 24,      // refresh every 24h
  },

  /* ── Plugins ──────────────────────────────── */
  plugins: [
    organization({
      /* Role-based access within each org */
      roles: {
        owner: {
          // Full access — billing, delete, manage members
        },
        admin: {
          // Manage settings, integrations, run analysis
        },
        member: {
          // Run analysis, view reports
        },
        viewer: {
          // View reports only
        },
      },

      /* Org creation settings */
      allowUserToCreateOrganization: true,

      /* Limit members per org (based on billing plan) */
      // memberLimit is handled at the application level, not here

      /* Organization metadata schema */
      organizationMetadata: {
        // stored as JSON in the organization row
        // e.g. { plan: "team", seats_total: 10, repo_url: "..." }
      },
    }),
  ],

  /* ── Advanced ─────────────────────────────── */
  trustedOrigins: [
    "http://localhost:5174",
    "http://localhost:3000",
    process.env.APP_URL || "https://app.featuremap.dev",
  ],
});

/**
 * Export the auth type for client-side type safety.
 * The client imports this type, not the implementation.
 */
export type Auth = typeof auth;
