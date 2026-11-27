#!/usr/bin/env bash
# Bump the version (docs/releases.md). Versions are Section.Feature.Fix, e.g. 2.6.1, the same rule as SirisOS and SirisAI:
#   scripts/bump-version.sh fix       # a fix or minor change                 2.6.1 -> 2.6.2
#   scripts/bump-version.sh feature   # a new feature or bigger upgrade       2.6.1 -> 2.7.0
#   scripts/bump-version.sh section   # a significant new section             2.6.1 -> 3.0.0
#   scripts/bump-version.sh rebase    # main moved on: renumber this branch onto the next version after main's
# (patch, minor and major also work, as fix, feature and section.) Each part counts on its own and never rolls over
# into the next, so a whole-number change always means a new section.
# Writes VERSION (and every other copy of it), the README's version line if it has one, and starts
# docs/releases/<version>.md. Merging the bump to main tags and releases it.
set -euo pipefail
cd "$(dirname "$0")/.."

kind="${1:-}"
current="$(tr -d '[:space:]' < VERSION)"
if [ "$kind" = "rebase" ]; then
  # Keep this branch's size of change (fix, feature or section), on top of main's version.
  git fetch -q origin main
  base="$(git show "$(git merge-base HEAD origin/main):VERSION" 2>/dev/null | tr -d '[:space:]' || true)"
  main="$(git show origin/main:VERSION | tr -d '[:space:]')"
  kind="$(python3 - "$base" "$current" <<'PY'
import sys
b, c = sys.argv[1], sys.argv[2]
try:
    bs, bf, bx = (int(p) for p in b.split("."))
    cs, cf, cx = (int(p) for p in c.split("."))
except ValueError:
    print("fix"); sys.exit()
print("section" if cs > bs else "feature" if cf > bf else "fix")
PY
)"
  current="$main"
fi
next="$(python3 - "$current" "$kind" <<'PY'
import sys
current, kind = sys.argv[1], {"patch": "fix", "minor": "feature", "major": "section"}.get(sys.argv[2], sys.argv[2])
parts = current.split(".")
if len(parts) != 3 or not all(p.isdigit() for p in parts):
    sys.exit(f"VERSION should be Section.Feature.Fix, like 2.6.1, not {current}")
s, f, x = (int(p) for p in parts)
if kind == "fix":
    x += 1
elif kind == "feature":
    f, x = f + 1, 0
elif kind == "section":
    s, f, x = s + 1, 0, 0
else:
    sys.exit("usage: scripts/bump-version.sh fix|feature|section|rebase")
print(f"{s}.{f}.{x}")
PY
)"

echo "$next" > VERSION
export next
# The app's other copies of the version.
perl -pi -e 's/^__version__ = ".*"/__version__ = "$ENV{next}"/' app/__init__.py
# The README's version line, if it has one. perl, not sed: macOS's sed -i -E takes -E as a backup suffix.
[ -f README.md ] && perl -pi -e "s#^\\*\\*Version [0-9.]+\\*\\* \\(\\[release notes\\]\\(docs/releases/[0-9.]+\\.md\\)#**Version $next** ([release notes](docs/releases/$next.md)#" README.md
mkdir -p docs/releases
notes="docs/releases/$next.md"
[ -f "$notes" ] || printf '# SirisDrone %s\n\n- \n' "$next" > "$notes"
echo "SirisDrone $current -> $next (fill in $notes)"
