#!/bin/sh
# Install kennet, the Kennet CLI, for this user:
#
#   curl -fsSL https://github.com/metaphi-labs/metaphi-ai-cli/releases/latest/download/install.sh | sh
#
# macOS and Linux, x86_64 and arm64; no root. The latest release's binary for this platform is checked against the
# release's checksums.txt (a mismatch is refused) and put in ~/.local/bin, or $KENNET_INSTALL_DIR. Run it again to
# upgrade. The binary then keeps itself current (KENNET_DISABLE_AUTOUPDATE=1 stops that).
#
#   KENNET_INSTALL_DIR   where kennet goes (default ~/.local/bin)
#   KENNET_RELEASE_URL   where the release's files are (default: the latest GitHub release); a test points it at a
#                        directory of its own
set -eu

REPO="metaphi-labs/metaphi-ai-cli"
BASE="${KENNET_RELEASE_URL:-https://github.com/$REPO/releases/latest/download}"
DIR="${KENNET_INSTALL_DIR:-$HOME/.local/bin}"

say() { printf '%s\n' "$*"; }
fail() { printf 'kennet install: %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) os=darwin ;;
  Linux) os=linux ;;
  *) fail "$(uname -s) is not supported; use pip install metaphi-ai" ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=x86_64 ;;
  arm64 | aarch64) arch=arm64 ;;
  *) fail "$(uname -m) is not supported; use pip install metaphi-ai" ;;
esac
target="$os-$arch"

if command -v curl >/dev/null 2>&1; then
  fetch() { curl -fsSL --retry 2 -o "$2" "$1"; }
elif command -v wget >/dev/null 2>&1; then
  fetch() { wget -q -O "$2" "$1"; }
else
  fail "needs curl or wget"
fi
if command -v sha256sum >/dev/null 2>&1; then
  sha256() { sha256sum "$1" | cut -d' ' -f1; }
elif command -v shasum >/dev/null 2>&1; then
  sha256() { shasum -a 256 "$1" | cut -d' ' -f1; }
else
  fail "needs sha256sum or shasum"
fi

tmp="$(mktemp -d 2>/dev/null || mktemp -d -t kennet)"
trap 'rm -rf "$tmp"' EXIT INT TERM

fetch "$BASE/checksums.txt" "$tmp/checksums.txt" || fail "could not download checksums.txt from $BASE"
line="$(grep "  kennet_[0-9][0-9.]*_$target\.tar\.gz\$" "$tmp/checksums.txt" | head -n 1 || true)"
[ -n "$line" ] || fail "the release has no build for $target"
want="${line%%  *}"
asset="${line#*  }"
version="${asset#kennet_}"
version="${version%%_*}"

say "Downloading kennet $version for $target"
fetch "$BASE/$asset" "$tmp/$asset" || fail "could not download $asset"
got="$(sha256 "$tmp/$asset")"
[ "$got" = "$want" ] || fail "$asset does not match checksums.txt (expected $want, got $got); nothing was installed"

tar -xzf "$tmp/$asset" -C "$tmp" kennet || fail "$asset holds no kennet binary"
mkdir -p "$DIR" || fail "cannot make $DIR"
cp "$tmp/kennet" "$DIR/.kennet.new.$$" || fail "cannot write to $DIR"
chmod 755 "$DIR/.kennet.new.$$"
mv -f "$DIR/.kennet.new.$$" "$DIR/kennet"   # a rename: a kennet already running carries on
say "Installed kennet $version to $DIR/kennet"

case ":$PATH:" in
  *":$DIR:"*) ;;
  *)
    case "${SHELL:-}" in
      */zsh) rc="$HOME/.zshrc" ;;
      */bash) rc="$HOME/.bashrc" ;;
      */fish) rc="$HOME/.config/fish/config.fish" ;;
      *) rc="your shell's profile" ;;
    esac
    say ""
    say "$DIR is not on your PATH. Add this line to $rc, then open a new terminal:"
    case "${SHELL:-}" in
      */fish) say "  fish_add_path \"$DIR\"" ;;
      *) say "  export PATH=\"$DIR:\$PATH\"" ;;
    esac
    ;;
esac

say ""
if { command -v claude >/dev/null 2>&1 || [ -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" ]; } &&
  "$DIR/kennet" skill install >/dev/null 2>&1; then
  say "Ask Claude: log me into Kennet"
else
  say "Next: run \`kennet login\`"
fi
