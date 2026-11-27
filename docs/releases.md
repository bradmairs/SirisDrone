# Releases and versions

SirisDrone has one version, in [`/VERSION`](../VERSION): **Section.Feature.Fix**, e.g. `2.6.1`, the same rule as SirisOS
and SirisAI (Brad's, since 2026-11-22; this repo since 2026-11-28).

## Every change bumps it

Each pull request moves the version up by one of:

- **Fix** (`scripts/bump-version.sh fix`): a fix or a minor change, e.g. 2.6.1 → 2.6.2.
- **Feature** (`scripts/bump-version.sh feature`): a new feature or a bigger upgrade, e.g. 2.6.1 → 2.7.0.
- **Section** (`scripts/bump-version.sh section`): a significant new section of the app, e.g. 2.6.1 → 3.0.0.

Each part counts on its own and never rolls over into the next (2.9.0 plus a feature is 2.10.0), so a change in the
whole number always means a new section. The script writes every copy of the version and starts
`docs/releases/<version>.md`: fill in what changed, why, and how to update.

If main moved on while your branch waited, CI's version check fails: `scripts/bump-version.sh rebase` renumbers the
branch onto the next version after main's, keeping its size of change.

## Releases

Merging a `VERSION` change to main tags `v<version>` and publishes the GitHub release from its notes
(`.github/workflows/release.yml`).
