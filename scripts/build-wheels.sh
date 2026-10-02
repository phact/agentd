#!/usr/bin/env bash
# Build agentd's wheels from the release workflow's binary artifacts
# (ARTIFACTS/bin-darwin, bin-linux-x86_64, bin-linux-aarch64):
#   macosx_14_0_arm64       darwin binaries + linux-aarch64 ones (for Colima)
#   manylinux_2_28_x86_64   linux-x86_64
#   manylinux_2_28_aarch64  linux-aarch64
#   py3-none-any + sdist    no binaries
set -euo pipefail
artifacts="$(cd "$1" && pwd)"
out="$(mkdir -p "$2" && cd "$2" && pwd)"
root="$(cd "$(dirname "$0")/.." && pwd)"
bin="$root/agentd/sandbox/bin"
cd "$root"

wheel() {
  local platform="$1"; shift
  rm -rf "$bin"; mkdir -p "$bin"
  for target in "$@"; do cp -R "$artifacts/bin-$target/." "$bin/"; done
  python3 - "$bin/prebuilt.json" "$@" <<'PY'
import json, os, sys
path, targets = sys.argv[1], sys.argv[2:]
ms = [json.load(open(f"{os.environ['ARTIFACTS']}/bin-{t}/prebuilt.json")) for t in targets]
assert len({(m["launcher"], m["libkrun"]) for m in ms}) == 1, f"binaries built from different sources: {ms}"
json.dump({**ms[0], "targets": sorted({t for m in ms for t in m["targets"]})}, open(path, "w"), indent=1)
PY
  chmod 755 "$bin"/agentd-* "$bin"/libkrun-* 2>/dev/null || true  # artifacts drop the mode
  AGENTD_WHEEL_PLATFORM="$platform" uv build --wheel -o "$out"
}

export ARTIFACTS="$artifacts"
wheel macosx_14_0_arm64 darwin linux-aarch64
wheel manylinux_2_28_x86_64 linux-x86_64
wheel manylinux_2_28_aarch64 linux-aarch64
rm -rf "$bin"
uv build -o "$out"  # sdist and the pure wheel
ls -l "$out"
