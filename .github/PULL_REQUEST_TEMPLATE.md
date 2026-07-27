<!--
    Add your PR description here.
    Describe how and what was changed, as well as why the change was made.
    Although this section is optional, please consider adding a summary for the reviewers.
-->

Resolves: JBRes-<!-- Add the ticket number here. -->

<!--
    Remember to include the full YouTrack ticket ID in the PR title,
    surrounded with square brackets and separated from the title with a single space.
-->

## Checklist

<!-- See AGENTS.md for the conventions behind these. Strike out what does not apply. -->

- [ ] `uv run ruff check` and `uv run ruff format` are clean (CI does not check formatting).
- [ ] Tests cover the change at the lowest suite that can express it (`unit` > `integration` > `e2e`).
- [ ] [`AGENTS.md`](../AGENTS.md) is still accurate — updated here if this PR changes a
      convention, the layout, the lint/test setup, or adds an invariant worth knowing.
