import React from "react";
import LandingFx from "@/components/LandingFx";
import ScanCarousel from "@/components/ScanCarousel";
import {
  GitBranch,
  Activity,
  Workflow,
  Users,
  Cpu,
  MessageSquare,
  CalendarDays,
  ArrowRight,
  Github,
  Terminal,
  Shield,
  BarChart3,
  Search,
  Check,
  MessageCircle,
  Copy,
  Zap,
} from "lucide-react";

const GITHUB_URL = "https://github.com/PashaSchool/featuremap";

export default async function LandingPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string | undefined>>;
}) {
  const params = await searchParams;
  const joined = params.joined === "1";

  return (
    <>
      <LandingFx />
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

        :root {
          --bg: #07070b;
          --bg-1: #0b0b12;
          --bg-2: #101019;
          --surface: #12121c;
          --surface-hi: #171724;
          --border: #1f1f2e;
          --border-hi: #2a2a3d;
          --fg: #ececf1;
          --fg-dim: #a1a1b5;
          --fg-muted: #60606f;
          --accent: #5b8cff;
          --accent-hi: #7da2ff;
          --accent-soft: rgba(91,140,255,0.12);
          --success: #34d399;
          --warning: #fbbf24;
          --danger: #f87171;
        }

        html, body { background: var(--bg); color: var(--fg); }

        /* ── Mouse follow light (flashlight in the dark) ── */
        .mouse-glow {
          position: fixed;
          top: 0; left: 0;
          width: 760px; height: 760px;
          margin: -380px 0 0 -380px;
          border-radius: 50%;
          background: radial-gradient(
            circle,
            rgba(170,190,255,0.07) 0%,
            rgba(140,160,255,0.04) 22%,
            rgba(120,140,230,0.018) 42%,
            transparent 62%
          );
          pointer-events: none;
          z-index: 9999;
          mix-blend-mode: plus-lighter;
          will-change: transform, opacity;
          opacity: 0;
          transition: opacity .8s ease-out;
        }
        .mouse-glow.visible { opacity: 1; }
        @media (pointer: coarse), (prefers-reduced-motion: reduce) {
          .mouse-glow { display: none; }
        }

        body::before {
          content: '';
          position: fixed; inset: 0;
          background-image:
            radial-gradient(circle at 20% 0%, rgba(91,140,255,0.08), transparent 40%),
            radial-gradient(circle at 80% 10%, rgba(167,91,255,0.05), transparent 45%);
          pointer-events: none;
          z-index: 0;
        }
        body::after {
          content: '';
          position: fixed; inset: 0;
          background-image:
            linear-gradient(rgba(255,255,255,0.018) 1px, transparent 1px),
            linear-gradient(90deg, rgba(255,255,255,0.018) 1px, transparent 1px);
          background-size: 48px 48px;
          mask-image: radial-gradient(ellipse 80% 60% at 50% 0%, black 40%, transparent 80%);
          pointer-events: none;
          z-index: 0;
        }

        .lp *, .lp {
          font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
          -webkit-font-smoothing: antialiased;
        }
        .mono { font-family: 'JetBrains Mono', SFMono-Regular, ui-monospace, monospace; font-feature-settings: 'ss01', 'cv11'; }

        .lp { max-width: 1200px; margin: 0 auto; padding: 0 32px; position: relative; z-index: 1; }

        /* ── Nav ── */
        .lp-nav { display: flex; align-items: center; justify-content: space-between; padding: 22px 0; border-bottom: 1px solid transparent; }
        .lp-logo { display: flex; align-items: center; gap: 10px; text-decoration: none; color: var(--fg); }
        .lp-logo-mark {
          width: 32px; height: 32px; border-radius: 8px;
          background: linear-gradient(135deg, #5b8cff, #a75bff);
          display: flex; align-items: center; justify-content: center;
          font-size: 12px; font-weight: 700; color: #fff;
          box-shadow: 0 0 0 1px rgba(255,255,255,0.08), 0 6px 20px rgba(91,140,255,0.25);
        }
        .lp-logo-text { font-size: 17px; font-weight: 700; letter-spacing: -0.3px; }
        .lp-nav-links { display: flex; align-items: center; gap: 28px; }
        .lp-nav-link { text-decoration: none; font-size: 14px; color: var(--fg-dim); font-weight: 500; transition: color .15s; }
        .lp-nav-link:hover { color: var(--fg); }
        .lp-nav-ghbtn {
          display: inline-flex; align-items: center; gap: 8px;
          padding: 7px 12px 7px 11px; border-radius: 8px;
          background: var(--surface); border: 1px solid var(--border);
          color: var(--fg); font-size: 13px; font-weight: 500;
          text-decoration: none; transition: all .15s;
        }
        .lp-nav-ghbtn:hover { background: var(--surface-hi); border-color: var(--border-hi); }

        /* ── Hero ── */
        .lp-hero { padding: 88px 0 56px; text-align: center; position: relative; }
        .lp-hero-eyebrow {
          display: inline-flex; align-items: center; gap: 8px;
          padding: 6px 14px 6px 10px; border-radius: 100px;
          background: var(--surface); border: 1px solid var(--border);
          font-size: 12px; color: var(--fg-dim); font-weight: 500;
          margin-bottom: 28px;
        }
        .lp-hero-eyebrow-tag {
          font-size: 10px; font-weight: 700; letter-spacing: 0.4px;
          padding: 2px 7px; border-radius: 100px;
          background: var(--accent-soft); color: var(--accent-hi);
          text-transform: uppercase;
        }
        .lp-h1 {
          font-size: 68px; font-weight: 700; line-height: 1.02;
          letter-spacing: -2.5px; color: var(--fg);
          max-width: 860px; margin: 0 auto 24px;
        }
        .lp-h1 .accent {
          background: linear-gradient(135deg, #5b8cff 0%, #a75bff 100%);
          -webkit-background-clip: text; background-clip: text;
          -webkit-text-fill-color: transparent;
        }
        .lp-hero-sub {
          font-size: 19px; color: var(--fg-dim);
          max-width: 620px; margin: 0 auto 40px; line-height: 1.55;
          font-weight: 400;
        }
        .lp-hero-sub strong { color: var(--fg); font-weight: 500; }

        /* terminal install */
        .lp-install {
          display: inline-flex; align-items: stretch;
          background: var(--bg-1); border: 1px solid var(--border);
          border-radius: 12px; overflow: hidden;
          margin: 0 auto 20px;
          box-shadow: 0 0 0 1px rgba(255,255,255,0.02), 0 24px 60px rgba(0,0,0,0.5);
        }
        .lp-install-prompt {
          padding: 14px 6px 14px 18px; color: var(--fg-muted);
          font-size: 14px; display: flex; align-items: center;
        }
        .lp-install-cmd {
          padding: 14px 18px 14px 6px; color: var(--fg);
          font-size: 14px; display: flex; align-items: center; gap: 6px;
        }
        .lp-install-cmd .cmd-fn { color: var(--accent-hi); }
        .lp-install-copy {
          width: 48px; border-left: 1px solid var(--border);
          background: var(--surface); color: var(--fg-dim);
          display: flex; align-items: center; justify-content: center;
          cursor: pointer; transition: all .15s; border: none;
          border-left: 1px solid var(--border);
        }
        .lp-install-copy:hover { background: var(--surface-hi); color: var(--fg); }

        .lp-privacy {
          display: flex; align-items: center; justify-content: center; gap: 7px;
          width: fit-content;
          font-size: 12px; color: var(--fg-muted); font-weight: 500;
          margin: 14px auto 28px;
          padding: 6px 12px; border-radius: 100px;
          background: rgba(52,211,153,0.06);
          border: 1px solid rgba(52,211,153,0.15);
        }
        .lp-privacy svg { color: var(--success); }

        .lp-hero-form {
          display: flex; gap: 8px;
          max-width: 460px; margin: 0 auto;
          padding: 6px;
          background: var(--surface);
          border: 1px solid var(--border-hi);
          border-radius: 14px;
          box-shadow: 0 0 0 1px rgba(255,255,255,0.02), 0 24px 60px rgba(0,0,0,0.4);
        }
        .lp-hero-form-input {
          flex: 1; padding: 12px 14px; border-radius: 9px;
          border: none; background: transparent;
          color: var(--fg); font-size: 14px; font-family: inherit; outline: none;
        }
        .lp-hero-form-input::placeholder { color: var(--fg-muted); }
        .lp-hero-form-btn {
          display: inline-flex; align-items: center; gap: 7px;
          padding: 12px 18px; border-radius: 9px;
          background: var(--fg); color: #07070b;
          font-size: 14px; font-weight: 600; border: none;
          cursor: pointer; font-family: inherit; white-space: nowrap;
          transition: all .15s;
        }
        .lp-hero-form-btn:hover { background: #fff; }
        .lp-hero-form-hint {
          font-size: 12px; color: var(--fg-muted);
          margin-top: 14px; line-height: 1.6;
        }
        .lp-hero-form-hint a {
          color: var(--fg-dim); text-decoration: none;
          border-bottom: 1px dashed var(--border-hi);
          padding-bottom: 1px;
        }
        .lp-hero-form-hint a:hover { color: var(--fg); }

        /* proof strip */
        .lp-proof {
          display: flex; justify-content: center; align-items: center;
          gap: 40px; margin-top: 56px; flex-wrap: wrap;
          padding: 20px 0; border-top: 1px solid var(--border);
          border-bottom: 1px solid var(--border);
          max-width: 960px; margin-left: auto; margin-right: auto;
        }
        .lp-proof-item { display: flex; flex-direction: column; align-items: center; gap: 4px; }
        .lp-proof-num { font-size: 22px; font-weight: 700; color: var(--fg); letter-spacing: -0.5px; }
        .lp-proof-label { font-size: 11px; color: var(--fg-muted); text-transform: uppercase; letter-spacing: 0.6px; font-weight: 500; }
        .lp-proof-divider { width: 1px; height: 32px; background: var(--border); }

        /* ── Scan showcase section ── */
        .lp-section { padding: 112px 0; position: relative; }
        .lp-section-label {
          display: inline-flex; align-items: center; gap: 8px;
          font-size: 11px; font-weight: 600; color: var(--accent-hi);
          text-transform: uppercase; letter-spacing: 0.8px;
          padding: 4px 10px; border-radius: 100px;
          background: var(--accent-soft); border: 1px solid rgba(91,140,255,0.2);
          margin-bottom: 20px;
        }
        .lp-section-header { text-align: center; max-width: 640px; margin: 0 auto 64px; }
        .lp-h2 {
          font-size: 46px; font-weight: 700; letter-spacing: -1.6px;
          color: var(--fg); margin-bottom: 16px; line-height: 1.1;
        }
        .lp-h2 .accent {
          background: linear-gradient(135deg, #5b8cff 0%, #a75bff 100%);
          -webkit-background-clip: text; background-clip: text;
          -webkit-text-fill-color: transparent;
        }
        .lp-h2-sub { font-size: 17px; color: var(--fg-dim); line-height: 1.6; }

        /* showcase terminal */
        .lp-showcase {
          max-width: 920px; margin: 0 auto;
          background: var(--bg-1); border: 1px solid var(--border);
          border-radius: 16px; overflow: hidden;
          box-shadow: 0 0 0 1px rgba(255,255,255,0.02), 0 40px 80px rgba(0,0,0,0.55);
          position: relative;
        }
        .lp-showcase-head {
          display: flex; align-items: center; gap: 14px;
          padding: 14px 18px; border-bottom: 1px solid var(--border);
          background: var(--bg-2);
        }
        .lp-showcase-dots { display: flex; gap: 6px; }
        .lp-showcase-dot { width: 11px; height: 11px; border-radius: 50%; }
        .lp-showcase-title {
          font-size: 12px; color: var(--fg-dim); font-weight: 500;
          flex: 1; text-align: center;
        }
        .lp-showcase-badge {
          display: inline-flex; align-items: center; gap: 6px;
          font-size: 10px; font-weight: 600;
          padding: 3px 8px; border-radius: 4px;
          text-transform: uppercase; letter-spacing: 0.4px;
        }
        .lp-showcase-badge.ok {
          background: rgba(52,211,153,0.1); color: var(--success);
          border: 1px solid rgba(52,211,153,0.2);
        }
        .lp-showcase-badge .pulse {
          width: 6px; height: 6px; border-radius: 50%;
          background: var(--success);
          animation: pulse 2s ease-in-out infinite;
        }
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.3; }
        }
        .lp-showcase-body {
          padding: 28px 32px;
          font-size: 13px; line-height: 1.7;
          color: var(--fg-dim);
        }
        .lp-sc-line { display: flex; gap: 10px; align-items: flex-start; }
        .lp-sc-prompt { color: var(--fg-muted); flex-shrink: 0; user-select: none; }
        .lp-sc-cmd { color: var(--fg); }
        .lp-sc-out { color: var(--fg-dim); padding-left: 22px; }
        .lp-sc-out .ok { color: var(--success); }
        .lp-sc-out .warn { color: var(--warning); }
        .lp-sc-out .bad { color: var(--danger); }
        .lp-sc-out .dim { color: var(--fg-muted); }
        .lp-sc-out .hi { color: var(--fg); font-weight: 500; }
        .lp-sc-spacer { height: 10px; }

        .lp-sc-feature-row {
          display: grid;
          grid-template-columns: 20px 1.5fr 55px 55px 55px 60px 80px;
          gap: 10px; padding: 4px 0;
          align-items: center;
        }
        .lp-sc-feature-row .f-icon { color: var(--fg-muted); }
        .lp-sc-feature-row .f-name { color: var(--fg); }
        .lp-sc-feature-row .f-name .dim { color: var(--fg-muted); font-weight: 400; margin-left: 6px; }
        .lp-sc-feature-row .f-health { text-align: right; font-variant-numeric: tabular-nums; }
        .lp-sc-feature-row .f-cov { text-align: right; font-variant-numeric: tabular-nums; }
        .lp-sc-feature-row .f-ratio { text-align: right; font-variant-numeric: tabular-nums; color: var(--fg-dim); }
        .lp-sc-feature-row .f-commits { text-align: right; font-variant-numeric: tabular-nums; color: var(--fg-dim); }
        .lp-sc-feature-row .f-impact { text-align: right; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.4px; }
        .lp-sc-flow-row {
          display: grid;
          grid-template-columns: 20px 1.5fr 55px 55px 55px 60px 80px;
          gap: 10px; padding: 3px 0;
          align-items: center;
          font-size: 12px;
        }
        .lp-sc-flow-row .fl-icon { color: var(--fg-muted); padding-left: 14px; }
        .lp-sc-flow-row .fl-name { color: var(--fg-dim); }
        .lp-sc-flow-row .fl-name::before { content: '↳ '; color: var(--fg-muted); }
        .lp-sc-flow-row .fl-val { text-align: right; font-variant-numeric: tabular-nums; color: var(--fg-muted); }

        .lp-showcase-caption {
          max-width: 920px; margin: 18px auto 0;
          text-align: center;
          font-size: 13px; color: var(--fg-muted);
          display: flex; align-items: center; justify-content: center; gap: 8px;
          flex-wrap: wrap;
        }
        .lp-showcase-caption a {
          color: var(--fg-dim); text-decoration: none;
          border-bottom: 1px dashed var(--border-hi);
        }
        .lp-showcase-caption a:hover { color: var(--fg); }
        .lp-showcase-caption .mono { color: var(--fg-dim); }
        .lp-showcase-caption-dot {
          width: 6px; height: 6px; border-radius: 50%;
          background: var(--success); flex-shrink: 0;
          box-shadow: 0 0 8px var(--success);
        }

        /* ── How it works (dark retheme) ── */
        .lp-steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 24px; max-width: 960px; margin: 0 auto; }
        .lp-step {
          padding: 28px; border-radius: 14px;
          background: var(--surface); border: 1px solid var(--border);
          position: relative;
        }
        .lp-step-num {
          width: 34px; height: 34px; border-radius: 8px;
          background: var(--accent-soft); color: var(--accent-hi);
          display: flex; align-items: center; justify-content: center;
          font-size: 14px; font-weight: 700;
          margin-bottom: 18px; border: 1px solid rgba(91,140,255,0.2);
        }
        .lp-step-title { font-size: 17px; font-weight: 600; color: var(--fg); margin-bottom: 8px; }
        .lp-step-desc { font-size: 14px; color: var(--fg-dim); line-height: 1.6; margin-bottom: 14px; }
        .lp-step-code {
          display: inline-block; font-size: 12px;
          background: var(--bg-1); border: 1px solid var(--border);
          padding: 6px 12px; border-radius: 6px; color: var(--fg-dim);
        }

        /* ── Roadmap ── */
        .lp-roadmap { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; max-width: 1080px; margin: 0 auto; }
        .lp-rm-col {
          background: var(--surface); border: 1px solid var(--border);
          border-radius: 16px; padding: 28px 26px 24px;
          position: relative; overflow: hidden;
        }
        .lp-rm-col::before {
          content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
        }
        .lp-rm-col.shipping::before { background: linear-gradient(90deg, transparent, var(--success), transparent); }
        .lp-rm-col.beta::before { background: linear-gradient(90deg, transparent, var(--accent), transparent); }
        .lp-rm-col.next::before { background: linear-gradient(90deg, transparent, var(--fg-muted), transparent); }

        .lp-rm-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
        .lp-rm-pill {
          display: inline-flex; align-items: center; gap: 6px;
          font-size: 10px; font-weight: 700; letter-spacing: 0.5px;
          text-transform: uppercase;
          padding: 4px 9px; border-radius: 100px;
        }
        .lp-rm-pill.shipping { background: rgba(52,211,153,0.12); color: var(--success); border: 1px solid rgba(52,211,153,0.22); }
        .lp-rm-pill.beta { background: rgba(91,140,255,0.12); color: var(--accent-hi); border: 1px solid rgba(91,140,255,0.22); }
        .lp-rm-pill.next { background: var(--bg-1); color: var(--fg-muted); border: 1px solid var(--border); }
        .lp-rm-pill .dot { width: 6px; height: 6px; border-radius: 50%; }
        .lp-rm-pill.shipping .dot { background: var(--success); box-shadow: 0 0 8px var(--success); }
        .lp-rm-pill.beta .dot { background: var(--accent); animation: pulse 2s ease-in-out infinite; }
        .lp-rm-pill.next .dot { background: var(--fg-muted); }

        .lp-rm-title { font-size: 18px; font-weight: 600; color: var(--fg); margin-bottom: 8px; letter-spacing: -0.3px; }
        .lp-rm-sub { font-size: 13px; color: var(--fg-dim); line-height: 1.55; margin-bottom: 20px; }
        .lp-rm-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 10px; }
        .lp-rm-item { display: flex; align-items: flex-start; gap: 10px; font-size: 13px; color: var(--fg-dim); line-height: 1.5; }
        .lp-rm-item-icon { flex-shrink: 0; margin-top: 2px; }
        .lp-rm-item strong { color: var(--fg); font-weight: 500; }
        .lp-rm-item .hint { color: var(--fg-muted); font-size: 12px; display: block; margin-top: 1px; }

        /* ── CTA ── */
        .lp-cta-wrap {
          background: linear-gradient(135deg, var(--surface) 0%, var(--bg-2) 100%);
          border: 1px solid var(--border-hi);
          border-radius: 20px; padding: 64px 48px; text-align: center;
          position: relative; overflow: hidden;
          transition: border-color 1.6s ease-out, box-shadow 1.6s ease-out;
        }
        .lp-cta-wrap.awake {
          border-color: rgba(91,140,255,0.32);
          box-shadow:
            0 0 0 1px rgba(91,140,255,0.08),
            0 30px 80px -20px rgba(91,140,255,0.18);
        }
        .lp-cta-wrap::before {
          content: ''; position: absolute;
          top: 50%; left: 50%;
          width: 760px; height: 760px;
          transform: translate(-50%, -50%) scale(0.6);
          background: radial-gradient(
            circle,
            rgba(91,140,255,0.22) 0%,
            rgba(167,91,255,0.10) 28%,
            transparent 60%
          );
          filter: blur(40px);
          opacity: 0;
          pointer-events: none;
          transition: opacity 1.8s ease-out, transform 2.2s cubic-bezier(.2,.7,.2,1);
        }
        .lp-cta-wrap.awake::before {
          opacity: 1;
          transform: translate(-50%, -50%) scale(1);
          animation: cta-breath 6s ease-in-out 2s infinite;
        }
        .lp-cta-wrap::after {
          content: ''; position: absolute;
          inset: -1px; border-radius: 20px; pointer-events: none;
          background: linear-gradient(120deg, transparent 30%, rgba(91,140,255,0.22) 50%, transparent 70%);
          opacity: 0;
          transition: opacity 1.4s ease-out;
          mix-blend-mode: plus-lighter;
        }
        .lp-cta-wrap.awake::after {
          opacity: 0.6;
        }
        @keyframes cta-breath {
          0%, 100% { opacity: 0.78; transform: translate(-50%, -50%) scale(1); }
          50% { opacity: 1; transform: translate(-50%, -50%) scale(1.06); }
        }
        @media (prefers-reduced-motion: reduce) {
          .lp-cta-wrap.awake::before { animation: none; }
        }
        .lp-cta-wrap > * { position: relative; z-index: 1; }
        .lp-cta-h2 { font-size: 38px; font-weight: 700; letter-spacing: -1.2px; color: var(--fg); margin-bottom: 14px; }
        .lp-cta-sub { font-size: 16px; color: var(--fg-dim); margin: 0 auto 32px; max-width: 440px; line-height: 1.6; }
        .lp-cta-form { display: flex; gap: 8px; max-width: 440px; margin: 0 auto 24px; }
        .lp-cta-input {
          flex: 1; padding: 13px 16px; border-radius: 10px;
          border: 1px solid var(--border-hi); background: var(--bg);
          color: var(--fg); font-size: 14px; font-family: inherit; outline: none;
          transition: all .15s;
        }
        .lp-cta-input::placeholder { color: var(--fg-muted); }
        .lp-cta-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(91,140,255,0.15); }
        .lp-cta-btn {
          padding: 13px 24px; border-radius: 10px;
          background: var(--fg); color: #07070b; border: none;
          font-size: 14px; font-weight: 600; cursor: pointer;
          font-family: inherit; white-space: nowrap; transition: all .15s;
        }
        .lp-cta-btn:hover { background: #fff; }
        .lp-cta-pricing { font-size: 13px; color: var(--fg-muted); }

        /* ── Footer ── */
        .lp-footer {
          padding: 40px 0 32px; margin-top: 80px;
          border-top: 1px solid var(--border);
          display: flex; justify-content: space-between; align-items: center;
          font-size: 13px; color: var(--fg-muted);
        }
        .lp-footer a { color: var(--fg-dim); text-decoration: none; margin-left: 20px; transition: color .15s; }
        .lp-footer a:hover { color: var(--fg); }

        /* ── Scan carousel (interactive live showcase) ── */
        .lp-carousel {
          max-width: 900px; margin: 0 auto;
        }
        .lp-carousel-tabs {
          display: flex; justify-content: center; gap: 6px;
          margin-bottom: 18px; flex-wrap: wrap;
        }
        .lp-carousel-tab {
          font-family: var(--mono); font-size: 12px;
          padding: 7px 14px; border-radius: 999px;
          background: transparent; border: 1px solid var(--border);
          color: var(--fg-dim); cursor: pointer;
          transition: all 0.18s ease;
          letter-spacing: 0.2px;
        }
        .lp-carousel-tab:hover {
          border-color: rgba(91,140,255,0.35);
          color: var(--fg);
        }
        .lp-carousel-tab.is-active {
          background: var(--accent-soft);
          border-color: rgba(91,140,255,0.45);
          color: var(--accent-hi);
          box-shadow: 0 0 0 1px rgba(91,140,255,0.15);
        }
        .lp-carousel-footer {
          display: flex; align-items: center; justify-content: center;
          gap: 18px; margin-top: 18px;
        }
        .lp-carousel-arrow {
          width: 32px; height: 32px; border-radius: 50%;
          background: var(--surface); border: 1px solid var(--border);
          color: var(--fg-dim); cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          transition: all 0.18s ease;
        }
        .lp-carousel-arrow:hover {
          border-color: rgba(91,140,255,0.4);
          color: var(--fg);
          transform: translateY(-1px);
        }
        .lp-carousel-dots {
          display: flex; gap: 8px;
        }
        .lp-carousel-dot {
          width: 8px; height: 8px; border-radius: 50%;
          background: var(--fg-muted); opacity: 0.35;
          border: none; padding: 0; cursor: pointer;
          transition: all 0.2s ease;
        }
        .lp-carousel-dot:hover { opacity: 0.7; }
        .lp-carousel-dot.is-active {
          background: var(--accent-hi); opacity: 1;
          width: 22px; border-radius: 4px;
        }
        /* Fade-in when the terminal body re-renders */
        .lp-showcase-body {
          animation: scFadeIn 0.35s ease;
        }
        @keyframes scFadeIn {
          from { opacity: 0.35; transform: translateY(4px); }
          to   { opacity: 1;    transform: translateY(0);   }
        }

        /* ── OSS benchmark gallery ── */
        .lp-bench { max-width: 960px; margin: 0 auto; }
        .lp-bench-intro {
          font-size: 14px; color: var(--fg-dim); line-height: 1.65;
          max-width: 640px; margin: 0 auto 36px; text-align: center;
        }
        .lp-bench-intro strong { color: var(--fg); font-weight: 600; }
        .lp-bench-grid {
          display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px;
        }
        .lp-bench-card {
          background: var(--surface); border: 1px solid var(--border);
          border-radius: 12px; padding: 18px 20px;
          transition: border-color 0.2s, transform 0.2s;
        }
        .lp-bench-card:hover {
          border-color: rgba(91,140,255,0.3); transform: translateY(-1px);
        }
        .lp-bench-head {
          display: flex; align-items: baseline; justify-content: space-between;
          gap: 10px; margin-bottom: 6px;
        }
        .lp-bench-name {
          font-family: var(--mono); font-size: 14px; font-weight: 600;
          color: var(--fg);
        }
        .lp-bench-name a { color: inherit; text-decoration: none; }
        .lp-bench-name a:hover { color: var(--accent-hi); }
        .lp-bench-lang {
          font-size: 10px; text-transform: uppercase; letter-spacing: 0.6px;
          color: var(--fg-muted); font-weight: 500;
        }
        .lp-bench-desc {
          font-size: 12px; color: var(--fg-dim); line-height: 1.5;
          margin-bottom: 12px;
        }
        .lp-bench-stats {
          display: flex; gap: 14px; font-size: 11px;
          color: var(--fg-muted); font-family: var(--mono);
        }
        .lp-bench-stats strong { color: var(--fg); font-weight: 600; }
        .lp-bench-footer {
          font-size: 12px; color: var(--fg-dim); text-align: center;
          margin-top: 28px; line-height: 1.65;
        }
        .lp-bench-footer code {
          font-family: var(--mono); font-size: 11px;
          background: var(--bg-1); border: 1px solid var(--border);
          padding: 2px 8px; border-radius: 4px; color: var(--fg-dim);
        }

        /* ── Responsive ── */
        @media (max-width: 860px) {
          .lp-h1 { font-size: 42px; letter-spacing: -1.4px; }
          .lp-hero-sub { font-size: 16px; }
          .lp-h2 { font-size: 32px; }
          .lp-hero { padding: 56px 0 40px; }
          .lp-section { padding: 72px 0; }
          .lp-steps { grid-template-columns: 1fr; }
          .lp-roadmap { grid-template-columns: 1fr; }
          .lp-bench-grid { grid-template-columns: 1fr; }
          .lp-install { width: 100%; }
          .lp-proof { gap: 20px; }
          .lp-proof-divider { display: none; }
          .lp-showcase-body { padding: 18px 16px; font-size: 12px; }
          .lp-sc-feature-row { grid-template-columns: 16px 1.4fr 45px 45px; gap: 8px; }
          .lp-sc-feature-row .f-cov, .lp-sc-feature-row .f-commits, .lp-sc-feature-row .f-impact { display: none; }
          .lp-cta-wrap { padding: 48px 24px; }
          .lp-cta-h2 { font-size: 28px; }
          .lp-cta-form { flex-direction: column; }
          .lp-footer { flex-direction: column; gap: 14px; text-align: center; }
          .lp-footer a { margin: 0 10px; }
          .lp-nav-links { gap: 14px; }
          .lp-nav-link { display: none; }
        }
      `}</style>

      <div className="lp">
        {/* ── Nav ── */}
        <nav className="lp-nav">
          <a href="/" className="lp-logo">
            <div className="lp-logo-mark">FL</div>
            <div className="lp-logo-text">Faultlines</div>
          </a>
          <div className="lp-nav-links">
            <a href="#how" className="lp-nav-link">How it works</a>
            <a href="#roadmap" className="lp-nav-link">Roadmap</a>
            <a href="/login" className="lp-nav-link">Sign in</a>
            <a
              href={GITHUB_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="lp-nav-ghbtn"
            >
              <Github size={14} />
              <span>Star on GitHub</span>
            </a>
          </div>
        </nav>

        {/* ── Hero ── */}
        <section className="lp-hero">
          <div className="lp-hero-eyebrow">
            <span className="lp-hero-eyebrow-tag">v0.1</span>
            Open source · MIT · Works on any language
          </div>

          <h1 className="lp-h1">
            Ship refactors with<br />
            <span className="accent">proof, not vibes.</span>
          </h1>

          <p className="lp-hero-sub">
            For engineering managers tired of arguing where to refactor.
            Faultlines drafts a <strong>feature map</strong> of your codebase from git
            history — then scores each feature by bug density, churn, and bus factor.
            You edit, merge, and rename in the dashboard. Bug-fix heatmap and author
            distribution come for free.
          </p>

          <div className="lp-install mono">
            <span className="lp-install-prompt">$</span>
            <span className="lp-install-cmd">
              <span className="cmd-fn">pip install</span> faultlines
            </span>
            <button
              className="lp-install-copy"
              aria-label="Copy install command"
              type="button"
            >
              <Copy size={14} />
            </button>
          </div>

          <div className="lp-privacy">
            <Shield size={12} />
            Runs locally with Ollama. Your code never leaves your laptop.
          </div>

          <form className="lp-hero-form" action="/api/waitlist" method="POST">
            <input
              className="lp-hero-form-input"
              type="email"
              name="email"
              placeholder="you@company.com"
              required
              aria-label="Email for hosted dashboard early access"
            />
            <button className="lp-hero-form-btn" type="submit">
              Get early access <ArrowRight size={14} />
            </button>
          </form>

          <div className="lp-hero-form-hint">
            Hosted dashboard private beta. Or{" "}
            <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer">
              ★ star on GitHub
            </a>
            {" "}— it&apos;s free forever for any repo.
          </div>

          {/* proof strip */}
          <div className="lp-proof">
            <div className="lp-proof-item">
              <div className="lp-proof-num mono">7</div>
              <div className="lp-proof-label">OSS repos benchmarked</div>
            </div>
            <div className="lp-proof-divider" />
            <div className="lp-proof-item">
              <div className="lp-proof-num mono">Draft</div>
              <div className="lp-proof-label">map · you refine</div>
            </div>
            <div className="lp-proof-divider" />
            <div className="lp-proof-item">
              <div className="lp-proof-num mono">0</div>
              <div className="lp-proof-label">Jira tags required</div>
            </div>
            <div className="lp-proof-divider" />
            <div className="lp-proof-item">
              <div className="lp-proof-num mono">Any</div>
              <div className="lp-proof-label">Language</div>
            </div>
          </div>
        </section>

        {/* ── Scan showcase (placeholder) ── */}
        <section className="lp-section" id="how">
          <div className="lp-section-header">
            <span className="lp-section-label">
              <Terminal size={11} /> Live scan preview
            </span>
            <h2 className="lp-h2">
              Point at a repo.<br />
              <span className="accent">Get receipts.</span>
            </h2>
            <p className="lp-h2-sub">
              One command reads your git history, clusters files into a draft
              feature map, and produces a health report. No Jira, no tagging,
              no setup — the rough cut is ready in minutes, and you refine the
              names and groupings from there.
            </p>
          </div>

          <ScanCarousel />
        </section>

        {/* ── Benchmark gallery — reproducible proof on 7 public OSS repos ── */}
        <section className="lp-section" style={{ paddingTop: 40 }}>
          <div className="lp-section-header">
            <span className="lp-section-label">
              <Check size={11} /> Benchmarked
            </span>
            <h2 className="lp-h2">
              Run it yourself on{" "}
              <span className="accent">real code.</span>
            </h2>
            <p className="lp-h2-sub">
              Don&apos;t trust a marketing number. These are public OSS repos
              you can clone and scan today — we did the run, the output is
              byte-for-byte reproducible with the CLI.
            </p>
          </div>

          <div className="lp-bench">
            <p className="lp-bench-intro">
              We draft a feature map from git history alone. On the 7 repos
              below, we found <strong>70–100% of the modules</strong> a
              maintainer would list in their README — with real business names,
              not <span className="mono">src/</span> or{" "}
              <span className="mono">utils/</span>. You edit, merge, and rename
              in the dashboard. Bug-fix heatmap and author distribution come
              for free.
            </p>

            <div className="lp-bench-grid">
              {[
                {
                  name: "gin-gonic/gin",
                  url: "https://github.com/gin-gonic/gin",
                  lang: "Go",
                  desc: "HTTP web framework — router, middleware, binding, render, context.",
                  files: "130",
                  features: "22",
                  flows: "36",
                },
                {
                  name: "axios/axios",
                  url: "https://github.com/axios/axios",
                  lang: "JS",
                  desc: "HTTP client — adapters, interceptors, cancel tokens, transforms, defaults.",
                  files: "329",
                  features: "57",
                  flows: "0",
                },
                {
                  name: "tiangolo/fastapi",
                  url: "https://github.com/fastapi/fastapi",
                  lang: "Python",
                  desc: "Web framework — routing, dependencies, security, openapi, websockets, exceptions.",
                  files: "2,981",
                  features: "14",
                  flows: "0",
                },
                {
                  name: "pallets/flask",
                  url: "https://github.com/pallets/flask",
                  lang: "Python",
                  desc: "Micro web framework — app, blueprints, sessions, templating, json, cli.",
                  files: "236",
                  features: "8",
                  flows: "0",
                },
                {
                  name: "trpc/trpc",
                  url: "https://github.com/trpc/trpc",
                  lang: "TS",
                  desc: "End-to-end typesafe APIs — server, client, react-query, openapi, adapters, links.",
                  files: "1,573",
                  features: "16",
                  flows: "48",
                },
                {
                  name: "excalidraw/excalidraw",
                  url: "https://github.com/excalidraw/excalidraw",
                  lang: "TS app",
                  desc: "Virtual whiteboard — canvas renderer, drawing tools, data export, collaboration.",
                  files: "1,225",
                  features: "15",
                  flows: "62",
                },
                {
                  name: "outline/outline",
                  url: "https://github.com/outline/outline",
                  lang: "TS app",
                  desc: "Team wiki — rich-text editor, document management, plugins, dashboard.",
                  files: "2,390",
                  features: "22",
                  flows: "188",
                },
                {
                  name: "formbricks/formbricks",
                  url: "https://github.com/formbricks/formbricks",
                  lang: "TS app",
                  desc: "Open-source form builder — surveys, responses, organization, auth.",
                  files: "3,316",
                  features: "33",
                  flows: "136",
                },
                {
                  name: "makeplane/plane",
                  url: "https://github.com/makeplane/plane",
                  lang: "TS app",
                  desc: "Jira alternative — issues, projects, pages, workspaces, editor, inbox.",
                  files: "4,932",
                  features: "134",
                  flows: "408",
                },
                {
                  name: "documenso/documenso",
                  url: "https://github.com/documenso/documenso",
                  lang: "TS app",
                  desc: "DocuSign alternative — document-signing, editor, templates, auth, billing, webhooks.",
                  files: "2,530",
                  features: "49",
                  flows: "191",
                },
                {
                  name: "TryGhost/Ghost",
                  url: "https://github.com/TryGhost/Ghost",
                  lang: "TS/JS app",
                  desc: "Blogging & newsletter platform — members, email, admin, posts, stats.",
                  files: "6,898",
                  features: "101",
                  flows: "281",
                },
                {
                  name: "calcom/cal.com",
                  url: "https://github.com/calcom/cal.com",
                  lang: "TS app",
                  desc: "Scheduling platform — bookings, events, availability, teams, app-store, insights.",
                  files: "10,463",
                  features: "282",
                  flows: "725",
                },
              ].map((r) => (
                <div className="lp-bench-card" key={r.name}>
                  <div className="lp-bench-head">
                    <span className="lp-bench-name">
                      <a href={r.url} target="_blank" rel="noopener noreferrer">
                        {r.name}
                      </a>
                    </span>
                    <span className="lp-bench-lang">{r.lang}</span>
                  </div>
                  <div className="lp-bench-desc">{r.desc}</div>
                  <div className="lp-bench-stats">
                    <span>
                      files <strong>{r.files}</strong>
                    </span>
                    <span>
                      features <strong>{r.features}</strong>
                    </span>
                    <span>
                      flows <strong>{r.flows}</strong>
                    </span>
                  </div>
                </div>
              ))}
            </div>

            <div className="lp-bench-footer">
              Reproduce any of these with{" "}
              <code>faultlines analyze /path/to/repo --llm --flows</code>.
              Snapshots live in{" "}
              <a
                href="https://github.com/pkuzina/faultlines/tree/main/tests/baseline/accuracy-7repos"
                target="_blank"
                rel="noopener noreferrer"
              >
                tests/baseline/accuracy-7repos
              </a>
              .
            </div>
          </div>
        </section>

        {/* ── How it works (short, 3 steps) ── */}
        <section className="lp-section" style={{ paddingTop: 40 }}>
          <div className="lp-section-header">
            <h2 className="lp-h2">
              <span className="accent">Three commands.</span> No setup.
            </h2>
            <p className="lp-h2-sub">
              No tagging, no ticket hygiene, no cross-team alignment meeting. Just a CLI.
            </p>
          </div>

          <div className="lp-steps">
            <div className="lp-step">
              <div className="lp-step-num">1</div>
              <div className="lp-step-title">Install</div>
              <div className="lp-step-desc">
                Python package. Works locally, in CI, or via GitHub App.
              </div>
              <div className="lp-step-code mono">pip install faultlines</div>
            </div>
            <div className="lp-step">
              <div className="lp-step-num">2</div>
              <div className="lp-step-title">Analyze</div>
              <div className="lp-step-desc">
                Claude (or local Ollama) reads git blame and commit history, clusters files into features.
              </div>
              <div className="lp-step-code mono">faultlines analyze .</div>
            </div>
            <div className="lp-step">
              <div className="lp-step-num">3</div>
              <div className="lp-step-title">Refactor with receipts</div>
              <div className="lp-step-desc">
                Open the report. Show leadership exactly where the bugs live and why it&apos;s worth fixing.
              </div>
              <div className="lp-step-code mono">~/.faultlines/*.json</div>
            </div>
          </div>
        </section>

        {/* ── Roadmap ── */}
        <section className="lp-section" id="roadmap" style={{ paddingTop: 40 }}>
          <div className="lp-section-header">
            <span className="lp-section-label">
              <Zap size={11} /> Roadmap
            </span>
            <h2 className="lp-h2">
              Built. Building. <span className="accent">Next.</span>
            </h2>
            <p className="lp-h2-sub">
              Where Faultlines is today, and where it&apos;s heading.
            </p>
          </div>

          <div className="lp-roadmap">
            {/* Shipping now */}
            <div className="lp-rm-col shipping">
              <div className="lp-rm-head">
                <span className="lp-rm-pill shipping">
                  <span className="dot" /> Live
                </span>
              </div>
              <div className="lp-rm-title">Shipping now</div>
              <div className="lp-rm-sub">Open-source CLI. Install it and scan any repo today.</div>
              <ul className="lp-rm-list">
                <li className="lp-rm-item">
                  <Check size={14} className="lp-rm-item-icon" color="var(--success)" />
                  <div>
                    <strong>Feature & flow detection</strong>
                    <span className="hint">LLM clusters files into features and user-facing flows from git history</span>
                  </div>
                </li>
                <li className="lp-rm-item">
                  <Check size={14} className="lp-rm-item-icon" color="var(--success)" />
                  <div>
                    <strong>Health scoring</strong>
                    <span className="hint">0&ndash;100 score combining bug ratio, churn, and bus factor</span>
                  </div>
                </li>
                <li className="lp-rm-item">
                  <Check size={14} className="lp-rm-item-icon" color="var(--success)" />
                  <div>
                    <strong>Monorepo support</strong>
                    <span className="hint">pnpm, Turbo, Nx, Lerna, Cargo, Go workspaces</span>
                  </div>
                </li>
                <li className="lp-rm-item">
                  <Check size={14} className="lp-rm-item-icon" color="var(--success)" />
                  <div>
                    <strong>Local-first LLM</strong>
                    <span className="hint">Claude or Ollama — code never leaves your laptop</span>
                  </div>
                </li>
              </ul>
            </div>

            {/* Private beta */}
            <div className="lp-rm-col beta">
              <div className="lp-rm-head">
                <span className="lp-rm-pill beta">
                  <span className="dot" /> Private beta
                </span>
              </div>
              <div className="lp-rm-title">Hosted dashboard</div>
              <div className="lp-rm-sub">SaaS on top of the CLI. Team view, trends, alerts.</div>
              <ul className="lp-rm-list">
                <li className="lp-rm-item">
                  <MessageSquare size={14} className="lp-rm-item-icon" color="var(--accent-hi)" />
                  <div>
                    <strong>PR risk comments</strong>
                    <span className="hint">Bot annotates every PR with affected features and their health</span>
                  </div>
                </li>
                <li className="lp-rm-item">
                  <Activity size={14} className="lp-rm-item-icon" color="var(--accent-hi)" />
                  <div>
                    <strong>Health trends over time</strong>
                    <span className="hint">Watch features rot or recover sprint by sprint</span>
                  </div>
                </li>
                <li className="lp-rm-item">
                  <Users size={14} className="lp-rm-item-icon" color="var(--accent-hi)" />
                  <div>
                    <strong>Bus-factor alerts</strong>
                    <span className="hint">Get pinged when a critical feature has only one owner left</span>
                  </div>
                </li>
                <li className="lp-rm-item">
                  <GitBranch size={14} className="lp-rm-item-icon" color="var(--accent-hi)" />
                  <div>
                    <strong>GitHub App + SSO</strong>
                    <span className="hint">One-click install, auto-scan on push, GitHub/Google OAuth</span>
                  </div>
                </li>
              </ul>
            </div>

            {/* Next up */}
            <div className="lp-rm-col next">
              <div className="lp-rm-head">
                <span className="lp-rm-pill next">
                  <span className="dot" /> Next
                </span>
              </div>
              <div className="lp-rm-title">On the build queue</div>
              <div className="lp-rm-sub">What we&apos;re building next, in priority order.</div>
              <ul className="lp-rm-list">
                <li className="lp-rm-item">
                  <BarChart3 size={14} className="lp-rm-item-icon" color="var(--fg-muted)" />
                  <div>
                    <strong>PostHog + Sentry integration</strong>
                    <span className="hint">Impact score = health × pageviews × error rate per flow</span>
                  </div>
                </li>
                <li className="lp-rm-item">
                  <Workflow size={14} className="lp-rm-item-icon" color="var(--fg-muted)" />
                  <div>
                    <strong>Cross-repo flow tracking</strong>
                    <span className="hint">Trace one user flow across frontend, backend, and worker repos</span>
                  </div>
                </li>
                <li className="lp-rm-item">
                  <MessageCircle size={14} className="lp-rm-item-icon" color="var(--fg-muted)" />
                  <div>
                    <strong>AI chat over your repo</strong>
                    <span className="hint">&ldquo;What did the team ship this week?&rdquo; — grounded in commits</span>
                  </div>
                </li>
                <li className="lp-rm-item">
                  <CalendarDays size={14} className="lp-rm-item-icon" color="var(--fg-muted)" />
                  <div>
                    <strong>Daily digest to Slack</strong>
                    <span className="hint">What changed today, what got riskier, who shipped what</span>
                  </div>
                </li>
              </ul>
            </div>
          </div>

          <div style={{ textAlign: "center", marginTop: 36, fontSize: 13, color: "var(--fg-muted)" }}>
            Want early access to the hosted dashboard?{" "}
            <a href="#waitlist" style={{ color: "var(--accent-hi)", textDecoration: "none", fontWeight: 500 }}>
              Join the waitlist ↓
            </a>
          </div>
        </section>

        {/* ── CTA ── */}
        <section className="lp-section" id="waitlist" style={{ paddingTop: 40 }}>
          <div className="lp-cta-wrap" data-awaken>
            <h2 className="lp-cta-h2">Stop arguing about<br />where to refactor.</h2>
            <p className="lp-cta-sub">
              Your next planning meeting deserves real numbers. Install the CLI in 30 seconds,
              or join the private beta for the hosted dashboard.
            </p>
            {joined ? (
              <div
                style={{
                  padding: "14px 22px",
                  borderRadius: 10,
                  background: "rgba(52,211,153,0.1)",
                  border: "1px solid rgba(52,211,153,0.25)",
                  color: "var(--success)",
                  fontSize: 14,
                  fontWeight: 600,
                  maxWidth: 440,
                  margin: "0 auto 20px",
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  justifyContent: "center",
                }}
              >
                <Check size={16} /> You&apos;re on the list. We&apos;ll be in touch.
              </div>
            ) : (
              <form className="lp-cta-form" action="/api/waitlist" method="POST">
                <input
                  className="lp-cta-input"
                  type="email"
                  name="email"
                  placeholder="you@company.com"
                  required
                />
                <button className="lp-cta-btn" type="submit">
                  Join waitlist
                </button>
              </form>
            )}
            <div className="lp-cta-pricing">
              Free during private beta. Early users shape what ships next.
            </div>
          </div>
        </section>

        {/* ── Footer ── */}
        <footer className="lp-footer">
          <span>© {new Date().getFullYear()} Faultlines</span>
          <span>
            <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer">GitHub</a>
            <a href="#how">Docs</a>
            <a href="/login">Sign in</a>
          </span>
        </footer>
      </div>
    </>
  );
}
