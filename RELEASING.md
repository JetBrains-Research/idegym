# Releasing

IdeGYM releases are cut by pushing a `vX.Y.Z` tag. Two workflows react to that tag:

- **`.github/workflows/publish.yml`** builds and publishes the server/orchestrator/watcher
  images, the example image, the Helm chart, and the Python packages.
- **`.github/workflows/changelog.yml`** generates a curated [`CHANGELOG.md`](CHANGELOG.md)
  section for the version, syncs the GitHub Release notes, and opens a pull request with the
  changelog change for review.

## Changelog automation

GitHub's own "auto-generated release notes" list every merged pull request — including dozens of
dependency bumps — which is hard to read. [`scripts/generate_changelog.py`](scripts/generate_changelog.py)
turns the merged history between two tags into a readable [Keep a Changelog][keepachangelog] entry:

- pull requests are grouped into **Features / Bug Fixes / Documentation / Infrastructure /
  Dependencies** from their title prefixes (`[docs]`, `[ci]`, `[e2e]`, …) and author;
- issue tickets (`JBRes-XXXX`) are referenced as plain IDs and pull requests are linked;
- dependency bumps are collapsed into a `<details>` block, and only **significant** upgrades are
  surfaced above the fold;
- a short **Highlights** paragraph is drafted by an LLM (see below).

Categorisation is heuristic — it reads pull-request titles, so an occasional entry lands in a
neighbouring section. Fix those by editing the generated section before merging the changelog PR.

### "Significant" dependencies

A dependency upgrade is surfaced as *notable* only when a library's **major version increases**
(e.g. `kubernetes 35 → 36`). GitHub Actions and container images (slash-named entries such as
`docker/build-push-action` or `grafana/grafana`) are treated as routine infrastructure pins and
kept in the collapsed block even when their major version changes. Everything else is collapsed
too. Tweak `is_significant_dependency` in the script to change this.

### Highlights (GitHub Models)

The `Highlights` paragraph is drafted in the workflow, **not** by the script. `changelog.yml`
runs the generator with `--emit-highlights-prompt` to produce the prompt, hands it to
[`actions/ai-inference`](https://github.com/marketplace/actions/ai-inference) which calls
[GitHub Models](https://docs.github.com/en/github-models) using the built-in `GITHUB_TOKEN`
(`permissions: models: read`, no secret required), then feeds the draft back with
`--highlights-file`. The model is `openai/gpt-4o` by default — change the `model:` input (or set
`provider: copilot` with e.g. `claude-sonnet-4.5` to route through the org's Copilot subscription).
If there are no substantive changes (dependency-only release) or the draft step is skipped, the
script writes a `_TODO_` placeholder and generation still succeeds. Always review the paragraph; it
is meant to be edited by hand when needed.

## Running the generator locally

```bash
# Auto-detect the previous tag from the range vPREV..vX.Y.Z (writes a Highlights placeholder):
uv run scripts/generate_changelog.py 0.11.0

# Preview to stdout without touching the file:
uv run scripts/generate_changelog.py 0.11.0 --print

# Explicit range / regenerate an existing section:
uv run scripts/generate_changelog.py 0.11.0 --previous 0.10.0 --force

# Supply your own Highlights text instead of the placeholder:
uv run scripts/generate_changelog.py 0.11.0 --highlights-file my-highlights.txt
```

The script has no third-party dependencies, so `uv run` needs no project sync. The `vX.Y.Z` tag
must exist locally (`git fetch --tags`) because the range is read from `git log`.

To **backfill** older releases, run the generator once per version (oldest first keeps the newest
section on top); edit the `_TODO_` Highlights afterwards:

```bash
for v in 0.8.0 0.9.0 0.10.0; do
  uv run scripts/generate_changelog.py "$v" --force
done
```

## Manual / re-runs in CI

The changelog workflow also accepts a `workflow_dispatch` with a `version` (and optional
`previous`) input, so a release can be regenerated or backfilled from the Actions tab without
re-tagging.

[keepachangelog]: https://keepachangelog.com/en/1.1.0/
