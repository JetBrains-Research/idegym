# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Sections are generated from merged pull requests by
[`scripts/generate_changelog.py`](scripts/generate_changelog.py); the `Highlights`
paragraph is drafted separately by a maintainer with
[`scripts/draft_highlights.py`](scripts/draft_highlights.py) and may be edited by hand.

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

[0.10.0]: https://github.com/JetBrains-Research/idegym/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/JetBrains-Research/idegym/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/JetBrains-Research/idegym/releases/tag/v0.8.0
