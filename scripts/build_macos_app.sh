#!/bin/zsh
set -euo pipefail

ROOT_DIR="${0:A:h:h}"
OUTPUT_DIR="$ROOT_DIR/dist"
APP_PATH="$OUTPUT_DIR/StockWatch.app"
INSTALL_APP=0
MIGRATE_DATA=0

for argument in "$@"; do
  case "$argument" in
    --install) INSTALL_APP=1 ;;
    --migrate-local-data) MIGRATE_DATA=1 ;;
    *) print -u2 "Unknown option: $argument"; exit 2 ;;
  esac
done

if [[ ! -x "$ROOT_DIR/.venv/bin/python3.13" ]]; then
  print -u2 "Missing .venv Python runtime. Create the project virtual environment first."
  exit 2
fi

mkdir -p "$OUTPUT_DIR"
STAGING_DIR="$(mktemp -d "${TMPDIR:-/tmp}/stockwatch-app.XXXXXX")"
STAGING_APP="$STAGING_DIR/StockWatch.app"
CONTENTS="$STAGING_APP/Contents"
RESOURCES="$CONTENTS/Resources"
RUNTIME="$RESOURCES/runtime"
mkdir -p "$CONTENTS/MacOS" "$RUNTIME"

cleanup() {
  if [[ -d "$STAGING_DIR" ]]; then
    /bin/rm -rf "$STAGING_DIR"
  fi
}
trap cleanup EXIT

export DEVELOPER_DIR="${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer}"
export CLANG_MODULE_CACHE_PATH="$STAGING_DIR/clang-module-cache"
export SWIFT_MODULECACHE_PATH="$STAGING_DIR/swift-module-cache"

/usr/bin/xcrun swiftc \
  -O \
  -target arm64-apple-macos13.0 \
  -framework AppKit \
  -framework Foundation \
  -framework ServiceManagement \
  "$ROOT_DIR/macos/StockWatchMenu.swift" \
  -o "$CONTENTS/MacOS/StockWatch"

/usr/bin/ditto "$ROOT_DIR/macos/Info.plist" "$CONTENTS/Info.plist"
for file in build_pool.py read_pool.py prefetch_reddit.py run_lean.py stockwatch_service.py; do
  /usr/bin/ditto "$ROOT_DIR/$file" "$RUNTIME/$file"
done
for directory in stockwatch_app lean config; do
  /usr/bin/ditto "$ROOT_DIR/$directory" "$RUNTIME/$directory"
done
/usr/bin/ditto "$ROOT_DIR/vendor/TradingAgents/tradingagents" "$RUNTIME/tradingagents"
/usr/bin/ditto "$ROOT_DIR/.venv" "$RESOURCES/venv"

# venv launchers are absolute Homebrew symlinks in the checkout.  App bundles
# may not contain symlinks that escape the bundle, so embed the launcher itself
# and keep only relative aliases inside Resources/venv/bin.
PYTHON_TARGET="$(/usr/bin/readlink "$ROOT_DIR/.venv/bin/python3.13")"
/bin/rm "$RESOURCES/venv/bin/python" "$RESOURCES/venv/bin/python3" "$RESOURCES/venv/bin/python3.13"
/usr/bin/ditto "$PYTHON_TARGET" "$RESOURCES/venv/bin/python3.13"
/bin/ln -s python3.13 "$RESOURCES/venv/bin/python3"
/bin/ln -s python3.13 "$RESOURCES/venv/bin/python"

# Editable installs in the development venv point back to the checkout.  The
# runtime copy above wins because the service starts with RUNTIME on sys.path.
/usr/bin/codesign --force --deep --sign - "$STAGING_APP"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$STAGING_APP"

if [[ -e "$APP_PATH" ]]; then
  OLD_APP="$OUTPUT_DIR/StockWatch.previous.$$.app"
  /bin/mv "$APP_PATH" "$OLD_APP"
  /bin/mv "$STAGING_APP" "$APP_PATH"
  /bin/rm -rf "$OLD_APP"
else
  /bin/mv "$STAGING_APP" "$APP_PATH"
fi

print "Built $APP_PATH"

if (( INSTALL_APP )); then
  SUPPORT_DIR="$HOME/Library/Application Support/StockWatch"
  mkdir -p "$SUPPORT_DIR"
  chmod 700 "$SUPPORT_DIR"
  if (( MIGRATE_DATA )) && [[ -d "$ROOT_DIR/local-data" && ! -e "$SUPPORT_DIR/data" ]]; then
    /usr/bin/ditto "$ROOT_DIR/local-data" "$SUPPORT_DIR/data"
    print "Migrated existing runtime data to $SUPPORT_DIR/data"
  fi
  if [[ -f "$ROOT_DIR/.env" && ! -e "$SUPPORT_DIR/.env" ]]; then
    /usr/bin/ditto "$ROOT_DIR/.env" "$SUPPORT_DIR/.env"
    chmod 600 "$SUPPORT_DIR/.env"
    print "Copied the private environment file to Application Support"
  fi
  /usr/bin/ditto "$APP_PATH" "/Applications/StockWatch.app"
  print "Installed /Applications/StockWatch.app"
fi
