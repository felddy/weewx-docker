# Repository rulesets #

This directory holds the repository's GitHub rulesets, managed as code so
that branch and tag protection is reviewable, version-controlled, and
reproducible instead of being hand-edited in the web UI.

## How it works ##

Each `*.json` file in this directory is a single, self-contained ruleset,
identified by its `name` field rather than a numeric ID so nothing is
hardcoded.  The Makefile targets operate on every `*.json` file here, so
adding or removing a ruleset is just a matter of adding or removing a file.

Managing rulesets requires the GitHub CLI (`gh`) and `jq`, with `gh`
authenticated as a repository admin.

## Applying ##

Push the local rulesets up to GitHub:

```console
make apply-ruleset
```

For each file, `apply-ruleset` resolves the ruleset by its `name`.  If a
ruleset with that name already exists it is updated (`PUT`); otherwise it is
created (`POST`).  The same body works for both, so this also bootstraps a
repository that has no matching ruleset yet.

## Exporting ##

If a ruleset is edited in the GitHub UI, pull those changes back into the
files so the repository stays the source of truth:

```console
make export-ruleset
```

For each file, `export-ruleset` fetches the live ruleset (located by its
`name`), strips the read-only fields the API rejects on write (`id`,
`_links`, `created_at`, timestamps, and so on), and rewrites the JSON.
Review the diff and commit the result.

## Decoding numeric IDs ##

Rulesets reference GitHub Apps and repository roles by numeric ID because
the API requires the number rather than a slug.  These IDs are stable; look
one up with the GitHub CLI, for example:

```console
gh api /apps/github-actions --jq '{slug, id, name}'
```
