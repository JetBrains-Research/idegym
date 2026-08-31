# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Sections are generated from merged pull requests by
[`scripts/generate_changelog.py`](scripts/generate_changelog.py); the `Highlights`
paragraph is drafted separately by a maintainer with
[`scripts/draft_highlights.py`](scripts/draft_highlights.py) and may be edited by hand.

## [0.12.0] - 2026-08-30

### Highlights

_TODO: summarise the headline changes of this release. Draft this paragraph with `uv run scripts/draft_highlights.py 0.12.0` (uses Claude Code), or write it by hand._

### Features

- Bound and sanitize bash executor output ([#286](https://github.com/JetBrains-Research/idegym/pull/286))
- Pin mcp-steroid by link and make the unschedulable readiness budget configurable (JBRes-10578, [#284](https://github.com/JetBrains-Research/idegym/pull/284))
- Replace Hydra with Pydantic config (JBRes-9159, [#280](https://github.com/JetBrains-Research/idegym/pull/280))
- Add safe database schema rollback support for IdeGYM deployments (JBRes-9944, [#278](https://github.com/JetBrains-Research/idegym/pull/278))
- add 0.11.1 section ([#274](https://github.com/JetBrains-Research/idegym/pull/274))

### Infrastructure

- Draft changelog Highlights with Claude Code, anchor the range on CHANGELOG.md ([#275](https://github.com/JetBrains-Research/idegym/pull/275))

### Dependencies

<details>
<summary>6 routine dependency updates</summary>

- Bump mermaid ([#283](https://github.com/JetBrains-Research/idegym/pull/283))
- Bump js-yaml ([#282](https://github.com/JetBrains-Research/idegym/pull/282))
- Bump dompurify ([#281](https://github.com/JetBrains-Research/idegym/pull/281))
- Bump fast-uri ([#279](https://github.com/JetBrains-Research/idegym/pull/279))
- Bump brace-expansion ([#277](https://github.com/JetBrains-Research/idegym/pull/277))
- Bump aiohttp ([#276](https://github.com/JetBrains-Research/idegym/pull/276))

</details>

## [0.11.1] - 2026-07-30

### Highlights

Image building gains a backend abstraction with a GKE Cloud Build implementation alongside Kaniko,
and plugin images can now be built without cloning the idegym repository. IDE images become more
configurable: external IntelliJ IDEA and PyCharm plugins can be supplied through a dedicated knob,
IDEA can run against a virtual display, and a new agentless OpenHands tools plugin exposes its
tooling over REST and MCP with a typed client. On the orchestration side, pod crashes are detected
and surfaced with a restart budget, servers can be restored from a specific pod snapshot via a
snapshot tag, and callers can specify more of the pod spec. This release also adds a Docusaurus
presentation and architecture website, curated changelog and release automation, fixes for local
builds and deployment on Linux, and a narrower PyPI publishing set with the IDE plugin packages
merged together.

### Features

- Add AGENTS.md with repository conventions and coding standards (JBRes-10127, [#272](https://github.com/JetBrains-Research/idegym/pull/272))
- merge the two duplicate 003 alembic revisions ([#271](https://github.com/JetBrains-Research/idegym/pull/271))
- Upgrade ruff to 0.16 and adopt its expanded rule set ([#267](https://github.com/JetBrains-Research/idegym/pull/267))
- Curated CHANGELOG.md + release automation (JBRes-9953, [#258](https://github.com/JetBrains-Research/idegym/pull/258))
- Agentless OpenHands tools plugin (REST + MCP + typed client) (JBRes-9808, [#230](https://github.com/JetBrains-Research/idegym/pull/230))
- Run IDEA with a virtual display (headless flag) (JBRes-10122, [#231](https://github.com/JetBrains-Research/idegym/pull/231))
- Build plugin images without cloning the idegym repo (JBRes-10055, [#204](https://github.com/JetBrains-Research/idegym/pull/204))
- External IDE plugins knob for IDEA and PyCharm images (JBRes-10058, [#206](https://github.com/JetBrains-Research/idegym/pull/206))
- Abstract the image build backend + add a GKE Cloud Build implementation (JBRes-9799, [#181](https://github.com/JetBrains-Research/idegym/pull/181))
- IdeGYM presentation & architecture website (Docusaurus + GitHub Pages) (JBRes-9938, [#202](https://github.com/JetBrains-Research/idegym/pull/202))
- restore a server from a specific pod snapshot via snapshot-tag (JBRes-9632, [#159](https://github.com/JetBrains-Research/idegym/pull/159))
- Limit PyPI publishing and merge plugin packages (JBRes-9740, [#161](https://github.com/JetBrains-Research/idegym/pull/161))
- handle pod crash events: detect crashes, surface reason, restart budget (JBRes-9066, [#162](https://github.com/JetBrains-Research/idegym/pull/162))
- Allow users to specify more specs for pods (JBRes-9717, [#160](https://github.com/JetBrains-Research/idegym/pull/160))

### Bug Fixes

- : fix package building ([#273](https://github.com/JetBrains-Research/idegym/pull/273))
- Make generate_changelog.py executable to fix EXE001 ([#270](https://github.com/JetBrains-Research/idegym/pull/270))
- mcp-steroid headless fixes + reflect recent features in the website ([#229](https://github.com/JetBrains-Research/idegym/pull/229))
- Fix local/remote deployment on Linux + local builds (JBRes-10057, [#205](https://github.com/JetBrains-Research/idegym/pull/205))
- Fix mcp-steroid version regex and IDE start-script naming ([#170](https://github.com/JetBrains-Research/idegym/pull/170))

### Documentation

- fix stale IdeGYM import paths in documentation ([#177](https://github.com/JetBrains-Research/idegym/pull/177))

### Infrastructure

- Add security-only npm Dependabot config for /website and improve auto-merge diagnostics ([#252](https://github.com/JetBrains-Research/idegym/pull/252))
- Group Dependabot examples + Docker routine updates into single PRs ([#247](https://github.com/JetBrains-Research/idegym/pull/247))
- Reduce Dependabot update noise (JBRes-9943, [#228](https://github.com/JetBrains-Research/idegym/pull/228))

### Dependencies

<details>
<summary>73 routine dependency updates</summary>

- Bump python in /orchestrator ([#259](https://github.com/JetBrains-Research/idegym/pull/259))
- Update fastapi[standard] requirement ([#266](https://github.com/JetBrains-Research/idegym/pull/266))
- Bump the actions-minor-patch group across 1 directory with 3 updates ([#261](https://github.com/JetBrains-Research/idegym/pull/261))
- Bump the python-other-minor-patch group with 2 updates ([#265](https://github.com/JetBrains-Research/idegym/pull/265))
- Bump pre-commit ([#269](https://github.com/JetBrains-Research/idegym/pull/269))
- Bump greenlet in the python-database-minor-patch group ([#263](https://github.com/JetBrains-Research/idegym/pull/263))
- Bump anyio in the python-web-stack-minor-patch group ([#262](https://github.com/JetBrains-Research/idegym/pull/262))
- Bump postcss ([#268](https://github.com/JetBrains-Research/idegym/pull/268))
- Bump the observability-minor-patch group ([#260](https://github.com/JetBrains-Research/idegym/pull/260))
- Bump pillow ([#251](https://github.com/JetBrains-Research/idegym/pull/251))
- `body-parser`: 1.20.5 → 1.20.6 ([#250](https://github.com/JetBrains-Research/idegym/pull/250))
- `webpack-dev-server`: 5.2.5 → 5.2.6 ([#249](https://github.com/JetBrains-Research/idegym/pull/249))
- `dompurify`: 3.4.11 → 3.4.12 ([#253](https://github.com/JetBrains-Research/idegym/pull/253))
- `svgo`: 3.3.3 → 3.3.4 ([#254](https://github.com/JetBrains-Research/idegym/pull/254))
- `fast-uri`: 3.1.3 → 3.1.4 ([#255](https://github.com/JetBrains-Research/idegym/pull/255))
- Bump pyasn1 ([#256](https://github.com/JetBrains-Research/idegym/pull/256))
- `fast-xml-parser`: 5.9.3 → 5.10.1 ([#257](https://github.com/JetBrains-Research/idegym/pull/257))
- Bump the python-other-minor-patch group with 2 updates ([#248](https://github.com/JetBrains-Research/idegym/pull/248))
- Update opentelemetry-sdk requirement in /examples ([#240](https://github.com/JetBrains-Research/idegym/pull/240))
- Bump ruff ([#244](https://github.com/JetBrains-Research/idegym/pull/244))
- Bump the python-observability-minor-patch group across 1 directory with 12 updates ([#243](https://github.com/JetBrains-Research/idegym/pull/243))
- Bump uvicorn ([#239](https://github.com/JetBrains-Research/idegym/pull/239))
- Bump postgresql ([#237](https://github.com/JetBrains-Research/idegym/pull/237))
- Update opentelemetry-exporter-otlp requirement in /examples ([#241](https://github.com/JetBrains-Research/idegym/pull/241))
- Bump python in /watcher ([#235](https://github.com/JetBrains-Research/idegym/pull/235))
- Bump prometheus ([#236](https://github.com/JetBrains-Research/idegym/pull/236))
- Bump the actions-minor-patch group with 2 updates ([#238](https://github.com/JetBrains-Research/idegym/pull/238))
- Update opentelemetry-instrumentation-httpx requirement ([#242](https://github.com/JetBrains-Research/idegym/pull/242))
- Bump kubernetes in the python-platform-minor-patch group ([#245](https://github.com/JetBrains-Research/idegym/pull/245))
- Bump the python-other-minor-patch group with 4 updates ([#246](https://github.com/JetBrains-Research/idegym/pull/246))
- `gradio`: 6.15.0 → 6.15.1 ([#232](https://github.com/JetBrains-Research/idegym/pull/232))
- `mcp`: 1.27.0 → 1.28.1 ([#233](https://github.com/JetBrains-Research/idegym/pull/233))
- `mcp`: 1.27.0 → 1.28.1 ([#234](https://github.com/JetBrains-Research/idegym/pull/234))
- `fastapi[standard]` → >=0.139.0 ([#226](https://github.com/JetBrains-Research/idegym/pull/226))
- Bump the observability group across 1 directory with 2 updates ([#221](https://github.com/JetBrains-Research/idegym/pull/221))
- Bump the astral group with 2 updates ([#218](https://github.com/JetBrains-Research/idegym/pull/218))
- Bump the docker group with 4 updates ([#219](https://github.com/JetBrains-Research/idegym/pull/219))
- `dorny/paths-filter`: 4.0.1 → 4.0.2 ([#220](https://github.com/JetBrains-Research/idegym/pull/220))
- `postgresql`: 18.7.11 → 18.7.13 ([#222](https://github.com/JetBrains-Research/idegym/pull/222))
- `hydra-core`: 1.3.3 → 1.3.4 ([#223](https://github.com/JetBrains-Research/idegym/pull/223))
- `uvicorn`: 0.49.0 → 0.50.1 ([#224](https://github.com/JetBrains-Research/idegym/pull/224))
- `anyio`: 4.13.0 → 4.14.1 ([#225](https://github.com/JetBrains-Research/idegym/pull/225))
- `greenlet`: 3.5.2 → 3.5.3 ([#227](https://github.com/JetBrains-Research/idegym/pull/227))
- Bump grafana ([#210](https://github.com/JetBrains-Research/idegym/pull/210))
- `gradio`: 6.12.0 → 6.15.0 ([#203](https://github.com/JetBrains-Research/idegym/pull/203))
- `astral-sh/ruff-action`: 3 → 4.0.0 ([#207](https://github.com/JetBrains-Research/idegym/pull/207))
- `docker/setup-buildx-action`: 4 → 4.1.0 ([#208](https://github.com/JetBrains-Research/idegym/pull/208))
- `dorny/paths-filter`: 4 → 4.0.1 ([#209](https://github.com/JetBrains-Research/idegym/pull/209))
- `postgresql`: 18.7.8 → 18.7.11 ([#211](https://github.com/JetBrains-Research/idegym/pull/211))
- Bump the opentelemetry group with 12 updates ([#212](https://github.com/JetBrains-Research/idegym/pull/212))
- `dependency-injector`: 4.49.0 → 4.49.1 ([#213](https://github.com/JetBrains-Research/idegym/pull/213))
- `ruff`: 0.15.18 → 0.15.20 ([#214](https://github.com/JetBrains-Research/idegym/pull/214))
- `alembic`: 1.18.4 → 1.18.5 ([#215](https://github.com/JetBrains-Research/idegym/pull/215))
- `joserfc`: 1.6.7 → 1.6.8 ([#217](https://github.com/JetBrains-Research/idegym/pull/217))
- `actions/checkout`: 6.0.3 → 7.0.0 ([#193](https://github.com/JetBrains-Research/idegym/pull/193))
- `postgresql`: 18.7.6 → 18.7.8 ([#194](https://github.com/JetBrains-Research/idegym/pull/194))
- Bump python in /orchestrator in the python group ([#195](https://github.com/JetBrains-Research/idegym/pull/195))
- `pytest`: 9.1.0 → 9.1.1 ([#196](https://github.com/JetBrains-Research/idegym/pull/196))
- `greenlet`: 3.5.1 → 3.5.2 ([#197](https://github.com/JetBrains-Research/idegym/pull/197))
- `ruff`: 0.15.17 → 0.15.18 ([#198](https://github.com/JetBrains-Research/idegym/pull/198))
- `hydra-core`: 1.3.2 → 1.3.3 ([#199](https://github.com/JetBrains-Research/idegym/pull/199))
- `sqlalchemy`: 2.0.50 → 2.0.51 ([#200](https://github.com/JetBrains-Research/idegym/pull/200))
- `joserfc`: 1.6.4 → 1.6.7 ([#201](https://github.com/JetBrains-Research/idegym/pull/201))
- `pytest`: 9.0.3 → 9.1.0 ([#186](https://github.com/JetBrains-Research/idegym/pull/186))
- Bump the observability group across 1 directory with 3 updates ([#184](https://github.com/JetBrains-Research/idegym/pull/184))
- `actions/checkout`: 6 → 6.0.3 ([#183](https://github.com/JetBrains-Research/idegym/pull/183))
- `postgresql`: 18.7.3 → 18.7.6 ([#185](https://github.com/JetBrains-Research/idegym/pull/185))
- `tqdm`: 4.67.3 → 4.68.2 ([#187](https://github.com/JetBrains-Research/idegym/pull/187))
- `junitparser`: 5.0.0 → 5.0.1 ([#188](https://github.com/JetBrains-Research/idegym/pull/188))
- `kubernetes`: 36.0.1 → 36.0.2 ([#189](https://github.com/JetBrains-Research/idegym/pull/189))
- `ruff`: 0.15.16 → 0.15.17 ([#190](https://github.com/JetBrains-Research/idegym/pull/190))
- `pydantic-settings`: 2.13.1 → 2.14.2 ([#191](https://github.com/JetBrains-Research/idegym/pull/191))
- `pydantic-settings`: 2.14.0 → 2.14.2 ([#192](https://github.com/JetBrains-Research/idegym/pull/192))

</details>

## [0.10.0] - 2026-06-18

### Highlights

This release brings MCP (Model Context Protocol) support across the stack: IdeGYM servers now
expose their MCP tools through the orchestrator's MCP server, and the `mcp-steroid` plugin can be
used from the IntelliJ IDEA and PyCharm plugins. GKE-based server checkpoint/restore matures with a
snapshot-preparation pipeline, asynchronous trigger cleanup, and a `snapshot()` method on
`IdeGYMServer`. The cleanup watcher becomes a standalone service, and the Helm chart gains UX
improvements and pod-snapshot documentation.

### Features

- extract cleanup watcher into a standalone service (JBRes-9093, [#141](https://github.com/JetBrains-Research/idegym/pull/141))
- make configure_logging idempotent to stop duplicate log lines ([#144](https://github.com/JetBrains-Research/idegym/pull/144))
- ClusterRole + ClusterBindings for orchestrator SA (JBRes-9587, [#143](https://github.com/JetBrains-Research/idegym/pull/143))
- add mcp session hash, sticky sessions and reduce amount of workers ([#129](https://github.com/JetBrains-Research/idegym/pull/129))
- add documentation for pod-snapshot, update Chart (JBRes-9477, [#130](https://github.com/JetBrains-Research/idegym/pull/130))
- add snapshot method to IdeGYMServer (JBRes-9577, [#131](https://github.com/JetBrains-Research/idegym/pull/131))
- initial implementation for checkpoint preparation pipeline (JBRes-9180, [#119](https://github.com/JetBrains-Research/idegym/pull/119))
- Updated README.md (JBRes-9505, [#120](https://github.com/JetBrains-Research/idegym/pull/120))
- Clean Trigger's CRD after snapshotting, wait for snapshot being taken as async operation (JBRes-9303, [#118](https://github.com/JetBrains-Research/idegym/pull/118))
- Snapshot/restore functionality various improvements ([#117](https://github.com/JetBrains-Research/idegym/pull/117))
- Documentation for MCP tools ([#110](https://github.com/JetBrains-Research/idegym/pull/110))
- Helm UX improvements (JBRes-9364, [#108](https://github.com/JetBrains-Research/idegym/pull/108))
- Showing MCP tools from IdeGYM servers through orchestrator's mcp server (JBRes-9332, [#102](https://github.com/JetBrains-Research/idegym/pull/102))
- Helm chart (JBRes-8916, [#97](https://github.com/JetBrains-Research/idegym/pull/97))
- allow to use mcp-steroid plugin in idea and pycharm plugins (JBRes-9207, [#99](https://github.com/JetBrains-Research/idegym/pull/99))

### Bug Fixes

- Pre-release fixes (JBRes-9587, [#132](https://github.com/JetBrains-Research/idegym/pull/132))
- Checkpoint / Restore API tweaks, snapshot-id propagation to the client fix (JBRes-9479, [#116](https://github.com/JetBrains-Research/idegym/pull/116))

### Documentation

- add some architectural diagrams ([#142](https://github.com/JetBrains-Research/idegym/pull/142))

### Dependencies

Notable upgrades:

- `cryptography`: 46.0.7 → 48.0.1 ([#180](https://github.com/JetBrains-Research/idegym/pull/180))
- `cryptography`: 47.0.0 → 48.0.1 ([#174](https://github.com/JetBrains-Research/idegym/pull/174))
- `structlog`: 25.5.0 → 26.1.0 ([#166](https://github.com/JetBrains-Research/idegym/pull/166))
- `kubernetes-asyncio`: 35.0.1 → 36.1.0 ([#165](https://github.com/JetBrains-Research/idegym/pull/165))
- `kubernetes`: 35.0.0 → 36.0.1 ([#156](https://github.com/JetBrains-Research/idegym/pull/156))

<details>
<summary>50 routine dependency updates</summary>

- `python-multipart`: 0.0.27 → 0.0.31 ([#178](https://github.com/JetBrains-Research/idegym/pull/178))
- `starlette`: 1.0.1 → 1.3.1 ([#179](https://github.com/JetBrains-Research/idegym/pull/179))
- `python-multipart`: 0.0.27 → 0.0.31 ([#173](https://github.com/JetBrains-Research/idegym/pull/173))
- `starlette`: 1.2.1 → 1.3.1 ([#175](https://github.com/JetBrains-Research/idegym/pull/175))
- `aiohttp`: 3.14.0 → 3.14.1 ([#176](https://github.com/JetBrains-Research/idegym/pull/176))
- `pyjwt`: 2.12.1 → 2.13.0 ([#172](https://github.com/JetBrains-Research/idegym/pull/172))
- `pyjwt`: 2.12.1 → 2.13.0 ([#171](https://github.com/JetBrains-Research/idegym/pull/171))
- `astral-sh/setup-uv`: 8.1.0 → 8.2.0 ([#163](https://github.com/JetBrains-Research/idegym/pull/163))
- `ruff`: 0.15.15 → 0.15.16 ([#164](https://github.com/JetBrains-Research/idegym/pull/164))
- Update fastapi[standard] requirement ([#167](https://github.com/JetBrains-Research/idegym/pull/167))
- `uvicorn`: 0.48.0 → 0.49.0 ([#168](https://github.com/JetBrains-Research/idegym/pull/168))
- `postgresql`: 18.7.2 → 18.7.3 ([#169](https://github.com/JetBrains-Research/idegym/pull/169))
- Bump python in /orchestrator in the python group ([#152](https://github.com/JetBrains-Research/idegym/pull/152))
- Bump actions/checkout in the core group across 1 directory ([#148](https://github.com/JetBrains-Research/idegym/pull/148))
- Bump the observability group across 1 directory with 3 updates ([#158](https://github.com/JetBrains-Research/idegym/pull/158))
- `starlette`: 1.0.0 → 1.0.1 ([#146](https://github.com/JetBrains-Research/idegym/pull/146))
- `postgresql`: 18.7.0 → 18.7.2 ([#150](https://github.com/JetBrains-Research/idegym/pull/150))
- Bump docker/setup-docker-action in the docker group ([#151](https://github.com/JetBrains-Research/idegym/pull/151))
- Bump pytest-asyncio in the testing group ([#153](https://github.com/JetBrains-Research/idegym/pull/153))
- `starlette`: 1.0.0 → 1.2.1 ([#154](https://github.com/JetBrains-Research/idegym/pull/154))
- Update fastapi[standard] requirement ([#155](https://github.com/JetBrains-Research/idegym/pull/155))
- `ruff`: 0.15.13 → 0.15.15 ([#157](https://github.com/JetBrains-Research/idegym/pull/157))
- `aiohttp`: 3.13.5 → 3.14.0 ([#145](https://github.com/JetBrains-Research/idegym/pull/145))
- `greenlet`: 3.5.0 → 3.5.1 ([#135](https://github.com/JetBrains-Research/idegym/pull/135))
- Bump the opentelemetry group across 1 directory with 13 updates ([#134](https://github.com/JetBrains-Research/idegym/pull/134))
- Bump the observability group across 1 directory with 2 updates ([#139](https://github.com/JetBrains-Research/idegym/pull/139))
- `sqlalchemy`: 2.0.49 → 2.0.50 ([#136](https://github.com/JetBrains-Research/idegym/pull/136))
- Bump the docker group with 3 updates ([#133](https://github.com/JetBrains-Research/idegym/pull/133))
- `uvicorn`: 0.47.0 → 0.48.0 ([#137](https://github.com/JetBrains-Research/idegym/pull/137))
- Update fastapi[standard] requirement ([#138](https://github.com/JetBrains-Research/idegym/pull/138))
- `postgresql`: 18.6.7 → 18.7.0 ([#140](https://github.com/JetBrains-Research/idegym/pull/140))
- `fastmcp`: 3.2.4 → 3.3.1 ([#123](https://github.com/JetBrains-Research/idegym/pull/123))
- `requests`: 2.33.1 → 2.34.2 ([#125](https://github.com/JetBrains-Research/idegym/pull/125))
- Bump python in /orchestrator in the python group ([#127](https://github.com/JetBrains-Research/idegym/pull/127))
- Bump the observability group in /charts/idegym with 3 updates ([#128](https://github.com/JetBrains-Research/idegym/pull/128))
- Update fastapi[standard] requirement ([#126](https://github.com/JetBrains-Research/idegym/pull/126))
- `uvicorn`: 0.46.0 → 0.47.0 ([#124](https://github.com/JetBrains-Research/idegym/pull/124))
- `ruff`: 0.15.12 → 0.15.13 ([#122](https://github.com/JetBrains-Research/idegym/pull/122))
- Bump the docker group with 2 updates ([#121](https://github.com/JetBrains-Research/idegym/pull/121))
- `idna`: 3.13 → 3.15 ([#115](https://github.com/JetBrains-Research/idegym/pull/115))
- `idna`: 3.11 → 3.15 ([#114](https://github.com/JetBrains-Research/idegym/pull/114))
- `postgresql`: 18.6.4 → 18.6.7 ([#113](https://github.com/JetBrains-Research/idegym/pull/113))
- `tomlkit`: 0.14.0 → 0.15.0 ([#112](https://github.com/JetBrains-Research/idegym/pull/112))
- `pydantic`: 2.13.3 → 2.13.4 ([#111](https://github.com/JetBrains-Research/idegym/pull/111))
- `authlib`: 1.6.11 → 1.6.12 ([#109](https://github.com/JetBrains-Research/idegym/pull/109))
- Update fastapi[standard] requirement ([#107](https://github.com/JetBrains-Research/idegym/pull/107))
- `urllib3`: 2.6.3 → 2.7.0 ([#103](https://github.com/JetBrains-Research/idegym/pull/103))
- `urllib3`: 2.6.3 → 2.7.0 ([#104](https://github.com/JetBrains-Research/idegym/pull/104))
- `python-multipart`: 0.0.26 → 0.0.27 ([#100](https://github.com/JetBrains-Research/idegym/pull/100))
- Update fastapi[standard] requirement ([#101](https://github.com/JetBrains-Research/idegym/pull/101))

</details>

## [0.9.0] - 2026-05-08

### Highlights

This release introduces the plugin system — plugins for tools and rewards, plus full PyCharm and
IntelliJ IDEA plugins — along with the initial MCP server implementation and an API for GKE pod
checkpointing. It adds support for dedicated Kubernetes node pools and an `examples/` folder with
OpenEnv and agentic RL-training (VERL) integrations.

### Features

- introduce full pycharm and idea plugins (JBRes-9072, [#93](https://github.com/JetBrains-Research/idegym/pull/93))
- Introducing plugins for tools and rewards (JBRes-9069, [#91](https://github.com/JetBrains-Research/idegym/pull/91))
- Introduce API for GKE pod checkpoint (JBRes-9089, [#92](https://github.com/JetBrains-Research/idegym/pull/92))
- Initial MCP server implementation (JBRes-9071, [#90](https://github.com/JetBrains-Research/idegym/pull/90))
- Enhance configuration model classes for resource specifications (JBRes-6033, [#87](https://github.com/JetBrains-Research/idegym/pull/87))
- Example of the agentic RL training using IDEGYM and VERL. ([#89](https://github.com/JetBrains-Research/idegym/pull/89))
- github issue templates (JBRes-9108, [#86](https://github.com/JetBrains-Research/idegym/pull/86))
- move hydra and alembic config and other files inside the module (JBRes-5360, [#82](https://github.com/JetBrains-Research/idegym/pull/82))
- Support for dedicated node pools (JBRes-8920, [#69](https://github.com/JetBrains-Research/idegym/pull/69))
- Introduce examples folder and add OpenEnv integration examples there (JBRes-9011, [#66](https://github.com/JetBrains-Research/idegym/pull/66))

### Bug Fixes

- Added bug report and feature request templates ([#85](https://github.com/JetBrains-Research/idegym/pull/85))
- Fix incomplete logging on orchestrator (JBRes-8864, [#80](https://github.com/JetBrains-Research/idegym/pull/80))

### Documentation

- fix pre-commit issue in docs ([#73](https://github.com/JetBrains-Research/idegym/pull/73))

### Infrastructure

- fix pg container clean up ([#98](https://github.com/JetBrains-Research/idegym/pull/98))
- CI changes (JBRes-9012, [#74](https://github.com/JetBrains-Research/idegym/pull/74))
- fix push pull registry urls and fix web socket connection closing ([#72](https://github.com/JetBrains-Research/idegym/pull/72))
- add database client tests ([#67](https://github.com/JetBrains-Research/idegym/pull/67))

### Dependencies

<details>
<summary>13 routine dependency updates</summary>

- `ruff`: 0.15.11 → 0.15.12 ([#96](https://github.com/JetBrains-Research/idegym/pull/96))
- Bump pytest-randomly in the testing group ([#94](https://github.com/JetBrains-Research/idegym/pull/94))
- `pre-commit`: 4.5.1 → 4.6.0 ([#95](https://github.com/JetBrains-Research/idegym/pull/95))
- `astral-sh/setup-uv`: 8.0.0 → 8.1.0 ([#88](https://github.com/JetBrains-Research/idegym/pull/88))
- `astral-sh/setup-uv`: 7 → 8.0.0 ([#81](https://github.com/JetBrains-Research/idegym/pull/81))
- Update testcontainers[postgres] requirement ([#79](https://github.com/JetBrains-Research/idegym/pull/79))
- Bump prom/prometheus ([#76](https://github.com/JetBrains-Research/idegym/pull/76))
- Bump grafana/tempo in /orchestrator/kubernetes/tempo ([#77](https://github.com/JetBrains-Research/idegym/pull/77))
- `ruff`: 0.15.10 → 0.15.11 ([#78](https://github.com/JetBrains-Research/idegym/pull/78))
- Bump grafana/grafana in /orchestrator/kubernetes/grafana ([#75](https://github.com/JetBrains-Research/idegym/pull/75))
- `authlib`: 1.6.10 → 1.6.11 ([#71](https://github.com/JetBrains-Research/idegym/pull/71))
- `mako`: 1.3.10 → 1.3.11 ([#70](https://github.com/JetBrains-Research/idegym/pull/70))
- `python-multipart`: 0.0.22 → 0.0.26 ([#68](https://github.com/JetBrains-Research/idegym/pull/68))

</details>

## [0.8.0] - 2026-04-14

### Highlights

The first tagged release of IdeGYM. Highlights include building images from arbitrary base images
via Kaniko, FIFO reuse of finished servers, owner-reference-based Kubernetes resource cleanup,
OpenEnv server compatibility, and a consistent SQL table naming scheme. It establishes the testing
foundation with full integration and end-to-end suites running in minikube.

### Features

- clean and update docstrings, descriptors and comments throughout the whole codebase (JBRes-9010, [#64](https://github.com/JetBrains-Research/idegym/pull/64))
- Support arbitrary base image building (JBRes-5153, [#39](https://github.com/JetBrains-Research/idegym/pull/39))
- introduce kaniko minikube tests ([#50](https://github.com/JetBrains-Research/idegym/pull/50))
- Add compatibility with OpenEnv servers ([#47](https://github.com/JetBrains-Research/idegym/pull/47))
- Add full integration tests in minikube (JBRes-8782, [#33](https://github.com/JetBrains-Research/idegym/pull/33))
- Use owner references for easier resource cleanup (JBRes-8762, [#35](https://github.com/JetBrains-Research/idegym/pull/35))
- return finished server in fifo order for new requests (JBRes-8318, [#32](https://github.com/JetBrains-Research/idegym/pull/32))
- Cleanup (JBRes-4758, JBRes-4975, [#22](https://github.com/JetBrains-Research/idegym/pull/22))
- Use secret reference for tracing credentials (JBRes-6154, [#14](https://github.com/JetBrains-Research/idegym/pull/14))
- add run_as_root parameter as a column in database and use it when reusing finished servers (JBRes-4758, [#13](https://github.com/JetBrains-Research/idegym/pull/13))
- update `uv` to the latest version (JBRes-4975, [#10](https://github.com/JetBrains-Research/idegym/pull/10))
- clean up orphaned kaniko jobs; better error handling during status monitoring (JBRes-6036, [#12](https://github.com/JetBrains-Research/idegym/pull/12))
- Consistent SQL table naming scheme (JBRes-5355, [#11](https://github.com/JetBrains-Research/idegym/pull/11))
- Initial commit

### Bug Fixes

- Fix handling of database migration locks (JBRes-8664, [#9](https://github.com/JetBrains-Research/idegym/pull/9))

### Documentation

- review http error codes, document them and fix some issues related to it ([#63](https://github.com/JetBrains-Research/idegym/pull/63))
- add proper documentation ([#55](https://github.com/JetBrains-Research/idegym/pull/55))

### Infrastructure

- disable e2e ci for now because runners are not available ([#65](https://github.com/JetBrains-Research/idegym/pull/65))
- E2E tests in CI (JBRes-8914, [#49](https://github.com/JetBrains-Research/idegym/pull/49))

### Dependencies

Notable upgrades:

- `kubernetes-asyncio`: 32.3.2 → 35.0.1 ([#58](https://github.com/JetBrains-Research/idegym/pull/58))
- `pytest-randomly`: 3.16.0 → 4.0.1 ([#43](https://github.com/JetBrains-Research/idegym/pull/43))
- `pytest`: 8.4.1 → 9.0.2 ([#31](https://github.com/JetBrains-Research/idegym/pull/31))

<details>
<summary>42 routine dependency updates</summary>

- Bump python in /orchestrator ([#56](https://github.com/JetBrains-Research/idegym/pull/56))
- Bump prom/prometheus ([#57](https://github.com/JetBrains-Research/idegym/pull/57))
- `ruff`: 0.15.9 → 0.15.10 ([#59](https://github.com/JetBrains-Research/idegym/pull/59))
- `pyyaml`: 6.0.2 → 6.0.3 ([#60](https://github.com/JetBrains-Research/idegym/pull/60))
- `pytest`: 9.0.2 → 9.0.3 ([#61](https://github.com/JetBrains-Research/idegym/pull/61))
- `pytest`: 9.0.2 → 9.0.3 ([#62](https://github.com/JetBrains-Research/idegym/pull/62))
- `requests`: 2.33.0 → 2.33.1 ([#53](https://github.com/JetBrains-Research/idegym/pull/53))
- `ruff`: 0.15.8 → 0.15.9 ([#52](https://github.com/JetBrains-Research/idegym/pull/52))
- Bump prom/prometheus ([#51](https://github.com/JetBrains-Research/idegym/pull/51))
- `aiohttp`: 3.13.3 → 3.13.4 ([#48](https://github.com/JetBrains-Research/idegym/pull/48))
- `pygments`: 2.19.1 → 2.20.0 ([#46](https://github.com/JetBrains-Research/idegym/pull/46))
- `pre-commit`: 4.3.0 → 4.5.1 ([#44](https://github.com/JetBrains-Research/idegym/pull/44))
- `ruff`: 0.15.7 → 0.15.8 ([#45](https://github.com/JetBrains-Research/idegym/pull/45))
- `grafana/grafana`: 12.4.1 → 12.4.2 ([#40](https://github.com/JetBrains-Research/idegym/pull/40))
- `python-on-whales`: 0.78.0 → 0.81.0 ([#41](https://github.com/JetBrains-Research/idegym/pull/41))
- `tqdm`: 4.67.1 → 4.67.3 ([#42](https://github.com/JetBrains-Research/idegym/pull/42))
- `jinja2`: 3.1.5 → 3.1.6 ([#38](https://github.com/JetBrains-Research/idegym/pull/38))
- `h11`: 0.14.0 → 0.16.0 ([#37](https://github.com/JetBrains-Research/idegym/pull/37))
- `starlette`: 0.45.3 → 0.49.1 ([#36](https://github.com/JetBrains-Research/idegym/pull/36))
- `virtualenv`: 20.29.2 → 20.36.1 ([#20](https://github.com/JetBrains-Research/idegym/pull/20))
- `aiohttp`: 3.12.15 → 3.13.3 ([#21](https://github.com/JetBrains-Research/idegym/pull/21))
- `urllib3`: 2.3.0 → 2.6.3 ([#19](https://github.com/JetBrains-Research/idegym/pull/19))
- `protobuf`: 6.31.1 → 6.33.5 ([#18](https://github.com/JetBrains-Research/idegym/pull/18))
- `python-multipart`: 0.0.20 → 0.0.22 ([#17](https://github.com/JetBrains-Research/idegym/pull/17))
- `filelock`: 3.17.0 → 3.20.3 ([#16](https://github.com/JetBrains-Research/idegym/pull/16))
- Bump grafana/tempo in /orchestrator/kubernetes/tempo ([#15](https://github.com/JetBrains-Research/idegym/pull/15))
- `docker/login-action`: 3 → 4 ([#7](https://github.com/JetBrains-Research/idegym/pull/7))
- Bump grafana/grafana in /orchestrator/kubernetes/grafana ([#6](https://github.com/JetBrains-Research/idegym/pull/6))
- `docker/build-push-action`: 6 → 7 ([#5](https://github.com/JetBrains-Research/idegym/pull/5))
- Bump python in /orchestrator ([#4](https://github.com/JetBrains-Research/idegym/pull/4))
- `docker/setup-buildx-action`: 3 → 4 ([#3](https://github.com/JetBrains-Research/idegym/pull/3))
- `docker/setup-docker-action`: 4 → 5 ([#2](https://github.com/JetBrains-Research/idegym/pull/2))
- `astral-sh/setup-uv`: 6 → 7 ([#1](https://github.com/JetBrains-Research/idegym/pull/1))
- `docker/build-push-action`: 6 → 7 ([#23](https://github.com/JetBrains-Research/idegym/pull/23))
- `docker/login-action`: 3 → 4 ([#24](https://github.com/JetBrains-Research/idegym/pull/24))
- `docker/setup-docker-action`: 4 → 5 ([#25](https://github.com/JetBrains-Research/idegym/pull/25))
- `docker/setup-buildx-action`: 3 → 4 ([#26](https://github.com/JetBrains-Research/idegym/pull/26))
- `supervisor`: 4.2.5 → 4.3.0 ([#27](https://github.com/JetBrains-Research/idegym/pull/27))
- `pytest-mock`: 3.14.1 → 3.15.1 ([#28](https://github.com/JetBrains-Research/idegym/pull/28))
- `tomlkit`: 0.13.3 → 0.14.0 ([#29](https://github.com/JetBrains-Research/idegym/pull/29))
- `pytest-asyncio`: 1.1.0 → 1.3.0 ([#30](https://github.com/JetBrains-Research/idegym/pull/30))
- `requests`: 2.32.5 → 2.33.0 ([#34](https://github.com/JetBrains-Research/idegym/pull/34))

</details>

[0.12.0]: https://github.com/JetBrains-Research/idegym/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/JetBrains-Research/idegym/compare/v0.10.0...v0.11.1
[0.10.0]: https://github.com/JetBrains-Research/idegym/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/JetBrains-Research/idegym/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/JetBrains-Research/idegym/releases/tag/v0.8.0
