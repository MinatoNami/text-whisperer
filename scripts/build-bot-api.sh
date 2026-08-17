#!/usr/bin/env bash
# Builds tdlib/telegram-bot-api from source into vendor/telegram-bot-api/bin.
#
# There is no Homebrew formula for this, so source is the only option. Expect
# 15-30 minutes on an M4 Pro for a cold build; it is skipped entirely if the
# binary already exists (pass --force to rebuild).
set -euo pipefail

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$PATH"

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="$APP_DIR/vendor/telegram-bot-api"
SRC="$APP_DIR/vendor/src/telegram-bot-api"
BIN="$PREFIX/bin/telegram-bot-api"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

if [[ -x "$BIN" && $FORCE -eq 0 ]]; then
  echo "==> telegram-bot-api already built: $("$BIN" --version 2>&1 | head -1)"
  exit 0
fi

command -v brew >/dev/null 2>&1 || {
  echo "Homebrew is required. See https://brew.sh" >&2
  exit 1
}

echo "==> Installing build dependencies"
brew install cmake gperf openssl@3 zlib

echo "==> Fetching sources"
mkdir -p "$(dirname "$SRC")"
if [[ -d "$SRC/.git" ]]; then
  git -C "$SRC" fetch --depth 1 origin master
  git -C "$SRC" reset --hard origin/master
  git -C "$SRC" submodule update --init --recursive --depth 1
else
  rm -rf "$SRC"
  git clone --recursive --depth 1 https://github.com/tdlib/telegram-bot-api.git "$SRC"
fi

# tdlib's C++ is memory-hungry per translation unit; leave headroom on 24 GB.
JOBS="${BUILD_JOBS:-$(( $(sysctl -n hw.ncpu) < 6 ? $(sysctl -n hw.ncpu) : 6 ))}"

echo "==> Building with $JOBS jobs (this takes a while)"
rm -rf "$SRC/build"
mkdir -p "$SRC/build"
cd "$SRC/build"
cmake \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX:PATH="$PREFIX" \
  -DOPENSSL_ROOT_DIR="$(brew --prefix openssl@3)" \
  -DZLIB_ROOT="$(brew --prefix zlib)" \
  ..
cmake --build . --target install -j "$JOBS"

echo "==> Built $("$BIN" --version 2>&1 | head -1)"
echo "    $BIN"
