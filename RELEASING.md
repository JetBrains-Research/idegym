# Releasing

IdeGYM releases are cut by pushing a `vX.Y.Z` tag. Two workflows react to that tag:

- **`.github/workflows/publish.yml`** builds and publishes the server/orchestrator/watcher
  images, the example image, the Helm chart, and the Python packages.
- **`.github/workflows/changelog.yml`** generates a curated [`CHANGELOG.md`](CHANGELOG.md)
  section for the version, syncs the GitHub Release notes, and opens a pull request with the
  changelog change for review.

The changelog pull request lands with a **placeholder in place of the `Highlights` paragraph** —
that one part is not automated, and a maintainer drafts it locally before merging (see
[Highlights](#highlights)).

## Changelog automation

GitHub's own "auto-generated release notes" list every merged pull request — including dozens of
dependency bumps — which is hard to read. [`scripts/generate_changelog.py`](scripts/generate_changelog.py)
turns the merged history between two tags into a readable [Keep a Changelog][keepachangelog] entry:

- pull requests are grouped into **Features / Bug Fixes / Documentation / Infrastructure /
  Dependencies** from their title prefixes (`[docs]`, `[ci]`, `[e2e]`, …) and author;
- issue tickets (`JBRes-XXXX`) are referenced as plain IDs and pull requests are linked;
- dependency bumps are collapsed into a `<details>` block, and only **significant** upgrades are
  surfaced above the fold;
- a short **Highlights** paragraph is left as a placeholder for a maintainer to draft (see below).

Categorisation is heuristic — it reads pull-request titles, so an occasional entry lands in a
neighbouring section. Fix those by editing the generated section before merging the changelog PR.

### "Significant" dependencies

A dependency upgrade is surfaced as *notable* only when a library's **major version increases**
(e.g. `kubernetes 35 → 36`). GitHub Actions and container images (slash-named entries such as
`docker/build-push-action` or `grafana/grafana`) are treated as routine infrastructure pins and
kept in the collapsed block even when their major version changes. Everything else is collapsed
too. Tweak `is_significant_dependency` in the script to change this.

### Highlights

**GitHub Models is not available for this repository**, so no LLM runs in CI and the workflow
never drafts the paragraph. Every generated section ships with a `_TODO_` placeholder naming the
command below, and the changelog pull request repeats it in its description. The rest of the
release automation — the changelog section, the pull request, the GitHub Release notes — is
unaffected and completes on its own.

A maintainer drafts the paragraph locally with
[`scripts/draft_highlights.py`](scripts/draft_highlights.py), which uses **Claude Code**:

```bash
# On the changelog branch the workflow pushed (tags too — the range comes from git log):
git fetch --tags origin changelog/v0.11.0 && git switch changelog/v0.11.0

# Draft the paragraph, rewrite the section in CHANGELOG.md, and update the release notes:
uv run scripts/draft_highlights.py 0.11.0 --sync-release

git commit -am "[changelog] draft 0.11.0 highlights" && git push
```

The script runs the generator with `--emit-highlights-prompt` to build the prompt from the
release's merged changes, pipes it through a one-shot `claude --print` (the drafting instructions
live in `SYSTEM_PROMPT` at the top of the script), then feeds the answer back with
`--highlights-file --force` to rewrite the section. `--sync-release` additionally pushes the
regenerated notes to the `vX.Y.Z` GitHub Release with `gh release edit`.

Other options:

```bash
# See the paragraph without touching CHANGELOG.md:
uv run scripts/draft_highlights.py 0.11.0 --print

# Draft with a specific model, and against an explicit previous release:
uv run scripts/draft_highlights.py 0.11.0 --model opus --previous 0.10.0
```

Requirements: the `claude` CLI installed and signed in (no API key or token is read by the
script), plus `gh` for `--sync-release`. Dependency-only releases produce no prompt — the script
says so and leaves the placeholder alone. Always read the result before committing; it is prose
about your own release and is meant to be edited by hand when it misses the point.

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

Neither script has third-party dependencies, so `uv run` needs no project sync. The `vX.Y.Z` tag
must exist locally (`git fetch --tags`) because the range is read from `git log`.

To **backfill** older releases, run the generator once per version (oldest first keeps the newest
section on top), then draft the Highlights of each:

```bash
for v in 0.8.0 0.9.0 0.10.0; do
  uv run scripts/generate_changelog.py "$v" --force
  uv run scripts/draft_highlights.py "$v"
done
```

## Manual / re-runs in CI

The changelog workflow also accepts a `workflow_dispatch` with a `version` (and optional
`previous`) input, so a release can be regenerated or backfilled from the Actions tab without
re-tagging.

[keepachangelog]: https://keepachangelog.com/en/1.1.0/
