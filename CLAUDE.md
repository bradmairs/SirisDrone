# SirisDrone Studio – working rules

## Releases and versioning (owner's standing instructions)

- The app is versioned from **1.0** (`__version__` in `app/__init__.py`, shown in the UI top bar
  and `/api/config`).
- Every change that is pushed gets a version bump in the same branch:
  - **+0.01** (`python tools/bump_version.py small "<summary>"`) for small fixes, tweaks,
    wording, small additions to an existing feature.
  - **+0.1** (`python tools/bump_version.py big "<summary>"`) for significant new features or
    behaviour changes.
  The tool writes the new version and adds a `CHANGELOG.md` entry; fill in the bullet points.
  Versions are plain decimals: 1.0 → 1.01 → 1.02, and a big step from 1.02 gives 1.12.
- **Merge to `main` automatically**: open the pull request ready for review (not a draft), and
  once the tests pass and the PR is mergeable, merge it yourself without waiting to be asked.
- After merging, tag the merge commit on `main` as `v<version>` and push the tag.

## Checks before pushing

- `python -m pytest` must pass.
- `node --check app/static/app.js` for UI changes; drive the UI in headless Chromium when the
  change is visible.
