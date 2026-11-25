#!/usr/bin/env bash
# Install the software factory.
#
#   curl -fsSL https://raw.githubusercontent.com/danielsmag/modular-factory/master/install.sh | bash -s -- --global
#   curl -fsSL https://raw.githubusercontent.com/danielsmag/modular-factory/master/install.sh | bash -s -- --tool claude
#
# --global installs the mfactory command for your user. Without it, the script
# copies the factory into the git repository you run it in.
set -euo pipefail

REPO="${FACTORY_SOURCE_REPO:-danielsmag/modular-factory}"
VERSION="latest"
SOURCE=""
TOOLS=()
GLOBAL=0
PERSONAL=0

usage() {
  cat <<'EOF'
Usage: install.sh [--global] [--tool claude|cursor|codex]... [--personal] [--version vX.Y.Z] [--source DIR]

  --global    install the mfactory command for your user
  --tool      the coding tool to generate agents for; repeat it, or separate
              names with commas, for several (default: claude). Not with --global.
  --personal  keep the factory untracked in this checkout (.git/info/exclude).
              Not with --global.
  --version   a release tag to install (default: the latest release)
  --source    install from a local copy of the factory instead of downloading
EOF
}

fail() {
  echo "install.sh: $*" >&2
  exit 1
}

need_value() {
  [ $# -ge 2 ] && [ -n "$2" ] || fail "$1 needs a value"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --global) GLOBAL=1; shift ;;
    --personal) PERSONAL=1; shift ;;
    --tool) need_value "$@"; IFS=',' read -r -a more <<<"$2"; TOOLS+=("${more[@]}"); shift 2 ;;
    --tool=*) IFS=',' read -r -a more <<<"${1#*=}"; TOOLS+=("${more[@]}"); shift ;;
    --version) need_value "$@"; VERSION="$2"; shift 2 ;;
    --version=*) VERSION="${1#*=}"; shift ;;
    --source) need_value "$@"; SOURCE="$2"; shift 2 ;;
    --source=*) SOURCE="${1#*=}"; shift ;;
    -h | --help) usage; exit 0 ;;
    *) usage >&2; fail "unknown option $1" ;;
  esac
done

if [ "$GLOBAL" = 1 ] && [ ${#TOOLS[@]} -gt 0 ]; then
  fail "--global installs the CLI for your user; run mfactory install --tool inside a repository"
fi
if [ "$GLOBAL" = 1 ] && [ "$PERSONAL" = 1 ]; then
  fail "--personal installs into a repository; run \`mfactory install --personal\` there"
fi
if [ "$GLOBAL" != 1 ]; then
  git rev-parse --show-toplevel >/dev/null 2>&1 || fail "run this inside the git repository you want the factory in"
fi

PYTHON=""
for candidate in python3 python3.13 python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1 &&
    "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    PYTHON="$candidate"
    break
  fi
done
[ -n "$PYTHON" ] || fail "Python 3.11 or later is needed and was not found"
[ "$PYTHON" = python3 ] || echo "note: python3 is older than 3.11; the factory needs \`python3\` to be 3.11 or later to run"

if [ -z "$SOURCE" ]; then
  command -v curl >/dev/null 2>&1 || fail "curl is needed to download the factory (or pass --source DIR)"
  SOURCE="$(mktemp -d)"
  trap 'rm -rf "$SOURCE"' EXIT
  ref="refs/tags/$VERSION"
  if [ "$VERSION" = latest ]; then
    tag="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null |
      "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])' 2>/dev/null || true)"
    # Before the first release, the master branch is the latest version.
    ref="${tag:+refs/tags/$tag}"
    ref="${ref:-refs/heads/master}"
  fi
  echo "Downloading $REPO ($ref)"
  curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$ref" | tar -xz -C "$SOURCE" --strip-components=1 ||
    fail "could not download $REPO at $ref"
fi
[ -f "$SOURCE/core/bin/mfactory" ] || fail "$SOURCE is not a copy of the factory"

if [ "$GLOBAL" = 1 ]; then
  HOME_DIR="${FACTORY_HOME:-$HOME/.mfactory}"
  BIN_DIR="${FACTORY_BIN_DIR:-$HOME/.local/bin}"
  # The user's files in the home; keep the list in step with KEPT_GLOBAL in cli/maintain.py.
  saved="$(mktemp -d)"
  for name in workspace.toml dashboard.json dashboard.log plan-usage.json; do
    if [ -f "$HOME_DIR/$name" ]; then
      cp -p "$HOME_DIR/$name" "$saved/$name"
    fi
  done
  if [ ! -f "$saved/workspace.toml" ] && [ -f "$HOME_DIR/fleet.toml" ]; then
    cp -p "$HOME_DIR/fleet.toml" "$saved/workspace.toml"
  fi
  rm -rf "$HOME_DIR"
  mkdir -p "$HOME_DIR" "$BIN_DIR"
  cp -a "$SOURCE"/. "$HOME_DIR"/
  cp -pR "$saved"/. "$HOME_DIR"/
  rm -rf "$saved"
  printf '%s\n' "$HOME_DIR" > "$HOME_DIR/.global"
  ln -sfn "$HOME_DIR/core/bin/mfactory" "$BIN_DIR/mfactory"
  echo "ok    ~/.mfactory is $HOME_DIR"
  echo "ok    command $BIN_DIR/mfactory"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: add $BIN_DIR to PATH: export PATH=\"$BIN_DIR:\$PATH\"" ;;
  esac
  echo "Next: in a repository, run \`mfactory install --tool claude\`."
  exit 0
fi

args=()
for tool in ${TOOLS[@]+"${TOOLS[@]}"}; do
  args+=(--tool "$tool")
done
if [ "$PERSONAL" = 1 ]; then
  args+=(--personal)
fi
"$PYTHON" "$SOURCE/core/bin/mfactory" init ${args[@]+"${args[@]}"}
