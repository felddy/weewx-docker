# Branch protection rulesets #

This directory holds the repository's GitHub rulesets, managed as code so
that branch protection is reviewable, version-controlled, and reproducible.

## Files ##

- `development.json` — protections for the default branch and `develop-*`
  branches.  Among other rules, it requires a small, stable set of status
  checks: `CI Gate`, `CodeQL`, and `dependency-review`.

## Applying ##

Apply a ruleset with the Makefile target (requires the GitHub CLI, `gh`,
authenticated as a repository admin):

```console
make apply-ruleset
```

`apply-ruleset` resolves the ruleset ID from the JSON `name` field at run
time, so no ID is hardcoded.  If a ruleset with that name already exists it
is updated (`PUT`); otherwise it is created (`POST`).  The same JSON body
works for both, so this also bootstraps a repository that has no matching
ruleset yet.

## Exporting ##

If a ruleset is changed in the GitHub UI, pull those edits back into the
file so the repository stays the source of truth:

```console
make export-ruleset
```

This fetches the live ruleset (located by the `name` in the current file),
strips the read-only fields the API rejects on write (`id`, `_links`,
`created_at`, timestamps, etc.), and rewrites `development.json`.  Review
the diff and commit the result.

## Decoding the App IDs ##

The JSON references GitHub Apps by numeric ID because the API requires the
number rather than the slug.  These IDs are global and stable across all of
GitHub:

| ID      | App                      | Used for                                |
| ------- | ------------------------ | --------------------------------------- |
| `15368` | GitHub Actions           | `integration_id` of the Actions checks  |
| `57789` | GitHub Advanced Security | `integration_id` of the `CodeQL` check  |
| `29110` | Dependabot               | `bypass_actors` (Dependabot may bypass) |

Verify any App ID yourself:

```console
gh api /apps/github-actions --jq '{slug, id, name}'
```

Unlike the App IDs, the ruleset ID is specific to this repository.  List the
repo's rulesets and their IDs with:

```console
gh api repos/felddy/weewx-docker/rulesets
```

## Why the "CI Gate" check? ##

Branch protection matches required checks by exact name, and rulesets do not
support wildcards.  Pinning every matrix or job context breaks whenever a
job is renamed or the build matrix changes.  Instead, the `Build` workflow
defines a single aggregate `CI Gate` job that depends on the other jobs, and
only that stable name -- plus `CodeQL` and `dependency-review` -- is
required.  Reworking the underlying jobs no longer requires editing this
ruleset.
