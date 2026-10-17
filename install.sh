#!/bin/sh
# Install kennet, the Kennet CLI, for this user:
#
#   curl -fsSL https://github.com/metaphi-labs/metaphi-ai-cli/releases/latest/download/install.sh | sh
#
# macOS and Linux, x86_64 and arm64; no root. The release's checksums.txt must carry Metaphi AI's release signature
# (checked with ssh-keygen), the archive must match it, and the binary must answer --version before anything moves.
# Each version goes in ~/.local/share/kennet/versions/<version>/, and ~/.local/bin/kennet (or
# $KENNET_INSTALL_DIR/kennet) is a symlink to it, flipped atomically: a kennet already running finishes on its own
# version. Run it again to upgrade; the binary also keeps itself current (KENNET_DISABLE_AUTOUPDATE=1 stops that).
#
#   KENNET_INSTALL_DIR   where the kennet command goes (default ~/.local/bin)
#   KENNET_RELEASE_URL   where the release's files are (default: the latest GitHub release)
#   KENNET_SETUP_AGENTS  1: set Kennet up in the coding agents here without asking; 0: do not ask

# The release key (src/metaphi/signature.py carries the same line). Empty: nothing verifies, nothing is installed.
PUBLIC_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGM4JSNzis6zZVswYoxPTh6MnN4TfZnwY8PWFIbaj+JO kennet-release"

main() {
  set -eu
  REPO="metaphi-labs/metaphi-ai-cli"
  BASE="${KENNET_RELEASE_URL:-https://github.com/$REPO/releases/latest/download}"
  DIR="${KENNET_INSTALL_DIR:-$HOME/.local/bin}"
  DATA="${XDG_DATA_HOME:-$HOME/.local/share}/kennet"

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
  # a shell under Rosetta says x86_64 on Apple silicon: take the native build
  if [ "$os" = darwin ] && [ "$arch" = x86_64 ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
    arch=arm64
  fi
  target="$os-$arch"

  if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL --proto '=https,file' --retry 2 -o "$2" "$1"; }
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
  command -v ssh-keygen >/dev/null 2>&1 || fail "needs ssh-keygen (OpenSSH) to check the release signature"
  [ -n "$PUBLIC_KEY" ] || fail "this install.sh carries no release key; nothing was installed"

  tmp="$(mktemp -d 2>/dev/null || mktemp -d -t kennet)"
  trap 'rm -rf "$tmp"' EXIT
  trap 'rm -rf "$tmp"; exit 130' INT TERM

  fetch "$BASE/checksums.txt" "$tmp/checksums.txt" || fail "could not download checksums.txt from $BASE"
  fetch "$BASE/checksums.txt.sig" "$tmp/checksums.txt.sig" || fail "the release has no signature; nothing was installed"
  printf 'kennet-release %s\n' "$(printf '%s' "$PUBLIC_KEY" | cut -d' ' -f1-2)" > "$tmp/allowed_signers"
  ssh-keygen -Y verify -f "$tmp/allowed_signers" -I kennet-release -n kennet-release \
    -s "$tmp/checksums.txt.sig" < "$tmp/checksums.txt" >/dev/null 2>&1 ||
    fail "checksums.txt is not signed by Metaphi AI's release key; nothing was installed"
  say "Signature verified (Metaphi AI release key)"

  line="$(grep "  kennet_[0-9][0-9.]*_$target\.tar\.gz\$" "$tmp/checksums.txt" | head -n 1 || true)"
  [ -n "$line" ] || fail "the release has no build for $target"
  want="${line%%  *}"
  asset="${line#*  }"
  version="${asset#kennet_}"
  version="${version%%_*}"

  say "Downloading kennet $version for $target"
  fetch "$BASE/$asset" "$tmp/$asset" || fail "could not download $asset"
  got="$(sha256 "$tmp/$asset")"
  [ "$got" = "$want" ] || fail "$asset does not match checksums.txt; nothing was installed"
  tar -xzf "$tmp/$asset" -C "$tmp" kennet || fail "$asset holds no kennet binary"
  chmod 755 "$tmp/kennet"
  [ "$("$tmp/kennet" --version 2>/dev/null || true)" = "kennet $version" ] ||
    fail "the kennet $version binary did not start here; nothing was installed"

  # the version, in a directory of its own; one already there is left as it is (a process may be running it)
  mkdir -p "$DATA/versions" "$DIR" || fail "cannot write to $DATA or $DIR"
  if [ ! -x "$DATA/versions/$version/kennet" ]; then
    stage="$DATA/versions/.tmp-$$"
    rm -rf "$stage" "$DATA/versions/$version"
    mkdir "$stage" || fail "cannot write to $DATA"
    cp "$tmp/kennet" "$stage/kennet" || fail "cannot write to $DATA"
    mv "$stage" "$DATA/versions/$version"
  fi

  # the command: a symlink to that version, flipped by a rename (a plain file from an older install is replaced)
  ln -s "$DATA/versions/$version/kennet" "$DIR/.kennet.link.$$" || fail "cannot write to $DIR"
  mv -f "$DIR/.kennet.link.$$" "$DIR/kennet" || fail "cannot write to $DIR"
  printf '%s\n' "$DIR/kennet" > "$DATA/link"
  "$DIR/kennet" __prune >/dev/null 2>&1 || true
  say "Installed kennet $version to $DIR/kennet"

  case ":$PATH:" in
    *":$DIR:"*) ;;
    *)
      case "${SHELL:-}" in
        */zsh) rc="$HOME/.zshrc" ;;
        */bash) if [ "$os" = darwin ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi ;;
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

  # Kennet in the coding agents here (the skill, and Kennet's MCP server in each one's config) changes their
  # configuration, so it is asked for, never done on the side: not in CI, not where there is no terminal to ask at
  # (a container or a sandbox image that installs kennet), and only on a yes. KENNET_SETUP_AGENTS=1 answers yes
  # ahead of time, 0 no.
  say ""
  setup="${KENNET_SETUP_AGENTS:-}"
  if [ -z "$setup" ] && [ -z "${CI:-}" ] && (: < /dev/tty) 2>/dev/null; then
    found="$("$DIR/kennet" agents list 2>/dev/null | awk '$2 == "here" { printf "%s%s", sep, $1; sep = ", " }')"
    if [ -n "$found" ]; then
      say "Set Kennet up in your coding agents ($found)?"
      printf "It adds the kennet skill, and Kennet's MCP server to each one's config. [y/N] "
      answer=""
      read -r answer < /dev/tty || true
      case "$answer" in [yY]*) setup=1 ;; esac
    fi
  fi
  if [ "$setup" = 1 ] && "$DIR/kennet" agents install; then
    say "Ask your coding agent: log me into Kennet"
  else
    say "Next: run \`kennet login\`"
    [ "$setup" = 1 ] || say "To set Kennet up in your coding agents: kennet agents install"
  fi
}

say() { printf '%s\n' "$*"; }
fail() { printf 'kennet install: %s\n' "$*" >&2; exit 1; }

main "$@"
