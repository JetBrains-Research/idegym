# IdeGYM website

The IdeGYM presentation & architecture site — a [Docusaurus](https://docusaurus.io/)
(TypeScript, classic preset) project. It doubles as a **live presentation surface**: the
hero is an interactive, clickable architecture diagram that drills down from a system
overview to per-component pages to the source on GitHub.

Everything here is **self-contained** — the entire Node toolchain lives in `website/`;
nothing is added to the repository root.

Published at **https://jetbrains-research.github.io/idegym/**.

## Prerequisites

- Node.js >= 20 and npm (the only tools needed).

## Run locally

```bash
cd website
npm install      # first time only
npm start        # dev server with hot reload at http://localhost:3000/idegym/
```

> The site is served under the `/idegym/` base path in dev and in production, so local
> URLs look like `http://localhost:3000/idegym/architecture`.

## Build & preview the production site

```bash
npm run build    # outputs static HTML to website/build/ — FAILS on any broken link
npm run serve    # serve the built site locally to verify
```

`npm run build` is the gate: it throws on broken internal links and bad Markdown links,
so a green build means the navigation is sound.

## Project layout

```
website/
├── docs/
│   ├── overview/        # concepts, data-flow (the plain-language + lifecycle layers)
│   ├── architecture/    # index.md = ★ interactive diagram, + one page per component
│   ├── deployment.md
│   ├── api.mdx          # hub linking the embedded Redoc API specs
│   └── reference/       # clean copy of documentation/*.md (secondary nav)
├── src/
│   ├── pages/index.tsx  # custom landing page
│   └── css/custom.css   # JetBrains-orange, dark-mode-first theme
├── static/openapi/      # committed orchestrator.json + server.json (rendered by Redoc)
├── scripts/gen_openapi.py  # regenerates the OpenAPI schemas from the FastAPI apps
├── docusaurus.config.ts
└── sidebars.ts
```

## The interactive architecture diagram

The centerpiece is `docs/architecture/index.md`. It uses
[`@docusaurus/theme-mermaid`](https://docusaurus.io/docs/markdown-features/diagrams) with
`click NodeId "url"` directives so nodes navigate (drill-down). Two conventions matter:

- **Mermaid click URLs include the base path** — e.g. `/idegym/architecture/orchestrator`.
  Mermaid emits a raw `<a href>` that the browser resolves against the domain root, so the
  `/idegym/` prefix is required (and works the same in dev and prod).
- **Markdown / JSX links do _not_** — use `/architecture/orchestrator` (or a Docusaurus
  `<Link>`); Docusaurus adds the base path and validates the link at build time.
- Clickable nodes require `securityLevel: 'loose'` (set in `docusaurus.config.ts`).

## Regenerating the API schemas

`docs/api.mdx` embeds two OpenAPI specs via [redocusaurus](https://redocusaurus.vercel.app/).
The committed schemas in `static/openapi/` are generated from the live FastAPI routers:

```bash
# from the repo root, using the project's Python venv (uv sync first)
python website/scripts/gen_openapi.py website/static/openapi
```

This rebuilds `orchestrator.json` and `server.json`. Commit them when the APIs change.

## Deployment

Pushes to `main` that touch `website/**` (or `documentation/**`) trigger
[`.github/workflows/docs.yml`](../.github/workflows/docs.yml), which builds the site and
publishes it via the official `actions/deploy-pages` flow. Enable it once in the repo
settings: **Settings → Pages → Build and deployment → Source = "GitHub Actions"**.

This workflow is independent of `ci.yml` / `lint.yml` / `publish.yml` and never runs the
Python test matrix.

## Presenting live (demo script)

Built to project well: dark mode by default, large headings, diagrams legible on a beamer.

1. **Full-screen the browser** and confirm dark mode (toggle is top-right).
2. **Landing (`/`)** — the one-liner: *"GitHub Codespaces for RL training and agent eval,
   at scale."* Click a node in the headline diagram to jump in.
3. **`/overview/concepts`** — name the parts in plain language (client, orchestrator,
   server pod, image, plugin, reward, watcher).
4. **`/overview/data-flow`** — walk the lifecycle: define → build → provision → use →
   evaluate → cleanup. The sequence diagram shows the RL inner loop.
5. **`/architecture`** — the money slide. Click **Orchestrator** → show its sub-diagram →
   click a node to open the **source on GitHub**. Back, then click **Server Pod**, then
   **Plugins**.
6. **`/api`** — open the **orchestrator** Redoc to show the real, generated API surface.
7. **`/deployment`** — close on how it runs (Kubernetes, Helm, gVisor, snapshots).

> Tip: use the browser back button between architecture pages — every diagram node and
> "view source" link is a real navigation, so the talk is fully click-driven.
