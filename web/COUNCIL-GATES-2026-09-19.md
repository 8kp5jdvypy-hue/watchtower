# Council gates — perchmarkets.com + app.perchmarkets.com (2026-09-19)

Experience gate and release gate run on the LIVE properties after the
Watch Log redesign (PR #185) shipped. Panel per
`creative-web-council/references/council-gates.md`; every seat reported
under the specialist evidence contract. Protected elements — the session
rail with its pinned real alerts, the "P on the rail" mark, and copy
claims only from the public API — were assessed and are unchanged.

## Experience gate

| Seat | Score | Findings → remedy (all shipped in #186) |
|---|---:|---|
| motion-choreographer | 2/10 as reported, **withdrawn** | Reported the rail frozen and rows never lighting. Reproduced as an artifact: the built-in browser pane's tab was hidden (`document.hidden === true`), which suspends rAF/IntersectionObserver/scroll. In a visible browser the rail reads 08:02 → 10:17 → 16:24 → 18:19 → 19:51 across the scroll, pins light by mid-page, rows light 2/2 on entering view, tooltip opacity 1 on focus. Lifecycle cleanup verified from source. |
| accessibility-media-auditor | 7/10 | Major: rail pin 7×7 target → 25px hit area (dot unchanged). Major: sign-in email had no label → hidden `<label for>`. Minor: keyboard met the rail before content → rail after `<main>`. Contrast 4.99–17.9:1; landmarks, skip link, heading outline, mobile sheet (aria-expanded, Escape, hidden) all verified by driving them. |
| typography-critic | 8/10 | Both faces preloaded and loaded, no synthetic weights; ledes 54 cpl; h1 two lines at 640; anatomy mono headline wraps to 4 lines at 640 (acceptable, watch at 390); rail tick labels at 9.6px are the floor. |
| conversion-psychology-critic | — | Path is email → link → dashboard, no dark patterns. Hypothesis: perceived cost/commitment unstated → one truthful line under the hero CTA ("Free while Perch is in beta. An email link signs you in; no card, no password."). Existing `signup_cta_click{source}` events measure hero vs access vs nav. |
| design-system-guardian | — | Drift: dashboard login/verify/shell still rendered the old ticker-tape `AmbientField` the direction rejects → removed. Hardcoded hover cyan and mixed radii → `--cyan-hover`, `--glow-cyan`, `--radius-*`. Purposeful exception kept: the cyan glow is the "noticed" state, now one token. |
| sonic / webgl-3d | n/a | No audio, no 3D. |

## Release gate

| Seat | Result → remedy (shipped in #187, #188) |
|---|---|
| performance / Lighthouse (mobile) | Before: a11y 96, BP 100, SEO 100; two failures. **After: 100 / 100 / 100 / 100, 56 passed, 0 failed.** Main bundle 21 kB, no WebGL or animation libraries, fonts preloaded. |
| experience-red-team | **Blocker**: `/privacy` and `/terms` rendered the homepage (the iOS release config already points at them) → static pages, factual drafts marked "pending review". **Major**: hero said "connecting…" forever on a failed status fetch while the footer said unavailable → explicit "scanner status unreachable" state. Verified working: sign-in validation, anchors, mobile sheet, no overflow at 390, honest outage copy, clean console. |
| accessibility-media-auditor | See above; contrast of unlit log rows (opacity 0.32) flagged by Lighthouse → dimmed by colour, ≥4.99:1 in every state. |
| seo-discovery-editor | Meta/OG/Twitter copy still said "See What Matters" from the old site → matches the redesign; sitemap lists /privacy and /terms; `/llms.txt` was the SPA shell → real file. robots/sitemap/canonical 200. |
| privacy-legal-reviewer | Issue-spotting only: the privacy and terms pages are drafts written from actual data flows (email for sign-in, localStorage anon id, session cookie on the dashboard, processors Cloudflare/DigitalOcean/Resend/market-data vendors). **Owner review required before treating them as policy.** |
| maintenance-critic | Cloudflare Workers Builds only uploads versions and never promotes — 445 unpromoted versions, the live site was 5 days stale before this pass. Documented in DEPLOYMENT.md already; the working command is `wrangler versions deploy <id>@100% --name watchtower --yes`. Both `PerchMark.jsx` copies must stay byte-identical (noted in the file). |
| real-device-qa | **Not run** — no physical devices in this session. Verified only in Chrome (DevTools MCP) at 390/640/1440 and headless. |
| release-director | Ship state: **Ready, with two owner actions** — review the privacy/terms drafts; run a real-device pass on an iPhone (Safari) for the mobile nav sheet and the iPhone-screens section. |

## What could not be verified
Real screen readers; OS-level reduced-motion in a rendered browser (verified from source); physical touch; iOS Safari; the dashboard's post-login states beyond Today.
