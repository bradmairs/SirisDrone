# SirisDrone Studio – working rules

## Versions and shipping (Brad's standing instructions)

- **Bump the version in every pull request.** Versions are **Section.Feature.Fix** (e.g. 2.6.1), the same rule as SirisOS and SirisAI: `scripts/bump-version.sh fix` (+0.0.1) for a fix or minor change; `scripts/bump-version.sh feature` (+0.1.0) for a new feature or a bigger upgrade; `scripts/bump-version.sh section` (+1.0.0) only for a significant new section. Fill in the `docs/releases/<version>.md` it starts. If CI's version check says main has moved on, run `scripts/bump-version.sh rebase`. See [docs/releases.md](docs/releases.md).
- Merging a `VERSION` change tags `v<version>` and publishes the GitHub release by itself.
- **Merge and deploy automatically**: open the pull request, wait for CI to go green, merge it (squash) and deploy, without being asked. Never merge red.

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
