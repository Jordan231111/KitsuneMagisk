#!/usr/bin/env bash

set -euo pipefail

# $1=variant
# $2=install_dir
# $3=exe
install_from_gh() {
  local ver
  ver=$(curl -fsSL --retry 3 --retry-all-errors 'https://api.github.com/repos/mozilla/sccache/releases/latest' | jq -er .tag_name)
  local url="https://github.com/mozilla/sccache/releases/download/${ver}/sccache-${ver}-$1.tar.gz"
  local dest="$2/$3"
  curl -fL --retry 3 --retry-all-errors "$url" | tar xz -O --wildcards "*/$3" > "$dest.tmp"
  chmod +x "$dest.tmp"
  mv "$dest.tmp" "$dest"
}

if [ "$RUNNER_OS" = "macOS" ]; then
  brew install sccache
elif [ "$RUNNER_OS" = "Linux" ]; then
  install_from_gh x86_64-unknown-linux-musl /usr/local/bin sccache
elif [ "$RUNNER_OS" = "Windows" ]; then
  install_from_gh x86_64-pc-windows-msvc "$USERPROFILE/.cargo/bin" sccache.exe
fi
