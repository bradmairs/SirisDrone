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
- Tagging is automatic: the CI workflow (`.github/workflows/ci.yml`) tags `v<version>` on `main`
  after each merge. Sessions can't push tags themselves, so don't try.
- CI runs the tests on every pull request; wait for it to pass before merging.

## Checks before pushing

- `python -m pytest` must pass.
- `node --check app/static/app.js` for UI changes; drive the UI in headless Chromium when the
  change is visible.

## Siris: record what you build (Brad's standing instruction)

Siris is Brad's second brain, the private repo `bradmairs/Siris-Second-Brain`, and his manual for rebuilding everything. When a session in this repo builds, changes, deploys or retires a feature, record it in Siris before finishing, without being asked:

- Follow `plugins/siris-brain/skills/learn/SKILL.md` in that repo. In a cloud session, attach it first (owner `bradmairs`, repo `Siris-Second-Brain`, push access). Always write through its engine (`tools/brain.py learn`), never by editing notes directly, then commit and push.
- Add to this app's note, **"SirisDrone"**, so it gets a dated update instead of a duplicate.
- Capture what changed and why, the version, how to deploy or update it, the settings that matter (names only), and lessons learned. Never write secrets.
- End your reply with one line, e.g. `🧠 Siris learned: [[SirisDrone]] (updated)`.
