#!/usr/bin/env bash
# Build the frontend and package the plugin as out/a1x-integrated-controller.zip,
# ready for Decky's "Install Plugin from ZIP File".
#
# Uses a local pnpm if available, otherwise a throwaway Node container
# (podman or docker), so nothing needs to be installed on the host.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$PWD
NAME=a1x-integrated-controller
OUT=$ROOT/out

# Use the lockfile once one exists; the first build creates it.
INSTALL="pnpm install"
[ -f pnpm-lock.yaml ] && INSTALL="pnpm install --frozen-lockfile"

if command -v pnpm >/dev/null 2>&1; then
  $INSTALL
  pnpm build
else
  ENGINE=$(command -v podman || command -v docker || true)
  if [ -z "$ENGINE" ]; then
    echo "Need pnpm, podman or docker to build the frontend." >&2
    exit 1
  fi
  "$ENGINE" run --rm -v "$ROOT":/src:Z -w /src docker.io/library/node:22-alpine \
    sh -c "corepack enable >/dev/null 2>&1 && $INSTALL && pnpm build"
fi

python3 -I -m py_compile main.py

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/$NAME/dist"
cp plugin.json package.json main.py README.md "$STAGE/$NAME/"
[ -f LICENSE ] && cp LICENSE "$STAGE/$NAME/"
cp -r udev "$STAGE/$NAME/"
cp dist/index.js "$STAGE/$NAME/dist/"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +

mkdir -p "$OUT"
rm -f "$OUT/$NAME.zip"
(cd "$STAGE" && python3 -I -m zipfile -c "$OUT/$NAME.zip" "$NAME")
echo "Created $OUT/$NAME.zip"
