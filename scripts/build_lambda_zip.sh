#!/usr/bin/env bash
# Build a zip Lambda: the service's top-level .py files plus the shared event and log contracts, and the packages in
# the service's requirements.txt if it has one (arm64 Linux wheels for Python 3.14, the functions' runtime,
# installed with uv whatever this machine is; binary wheels only, so nothing is compiled here).
# Usage: scripts/build_lambda_zip.sh <service>   (e.g. stream-router) -> dist/<service>.zip
# Upload it in the console (Code -> Upload from -> .zip file) or with `aws lambda update-function-code`.
set -euo pipefail

service="${1:?usage: $0 <service>}"
root="$(cd "$(dirname "$0")/.." && pwd)"
src="$root/services/$service"
out="$root/dist/$service.zip"
[[ -d "$src" ]] || { echo "no such service: $src" >&2; exit 1; }

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT

cp "$src"/*.py "$stage/"
mkdir "$stage/contracts"
cp "$root/services/shared/contracts/__init__.py" "$root/services/shared/contracts/events.py" \
  "$root/services/shared/contracts/logs.py" "$root/services/shared/contracts/prices.py" "$stage/contracts/"
if [[ -f "$src/requirements.txt" ]]; then
  uv pip install --quiet --target "$stage" --python-version 3.14 --python-platform aarch64-manylinux_2_28 \
    --only-binary :all: -r "$src/requirements.txt"
  rm -rf "$stage/bin"
  find "$stage" -name __pycache__ -type d -prune -exec rm -rf {} +
fi

mkdir -p "$root/dist"
rm -f "$out"
(cd "$stage" && zip -qrX "$out" .)
if [[ -f "$src/requirements.txt" ]]; then
  echo "$out ($(du -h "$out" | cut -f1), $(unzip -Z1 "$out" | grep -c .) files; top level: $(cd "$stage" && ls | tr '\n' ' '))"
else
  echo "$out ($(unzip -Z1 "$out" | tr '\n' ' '))"
fi
