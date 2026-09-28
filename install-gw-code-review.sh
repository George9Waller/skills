#!/usr/bin/env bash
# Install only the gw-code-review skill without replacing an existing skills folder.

set -euo pipefail

REPO_URL="${GW_CODE_REVIEW_REPO_URL:-https://github.com/George9Waller/skills.git}"
BRANCH="${GW_CODE_REVIEW_BRANCH:-main}"
SKILLS_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills"
DESTINATION="$SKILLS_DIR/gw-code-review"
TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gw-code-review.XXXXXX")"

cleanup() {
  rm -rf "$TEMP_DIR"
}
trap cleanup EXIT

if ! command -v git >/dev/null 2>&1; then
  echo "git is required to install gw-code-review." >&2
  exit 1
fi

if [ -e "$DESTINATION" ]; then
  read -r -p "Replace existing installation at $DESTINATION? [y/N] " RESPONSE
  case "$RESPONSE" in
    y|Y|yes|YES|Yes)
      rm -rf "$DESTINATION"
      ;;
    *)
      echo "Installation cancelled; existing skill was left unchanged."
      exit 0
      ;;
  esac
fi

git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TEMP_DIR/repository"

SOURCE="$TEMP_DIR/repository/gw-code-review"
if [ ! -f "$SOURCE/SKILL.md" ]; then
  echo "The repository does not contain gw-code-review/SKILL.md." >&2
  exit 1
fi

mkdir -p "$SKILLS_DIR"
cp -R "$SOURCE" "$DESTINATION"

echo "Installed gw-code-review at: $DESTINATION"
echo "Next, run: cd \"$DESTINATION\" && uv sync"
