#!/usr/bin/env bash
# Build and ad-hoc sign the agentd-krun launcher.
# Hypervisor.framework only runs in binaries carrying the
# com.apple.security.hypervisor entitlement.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
out="${1:-$here/bin/agentd-krun}"
prefix="$(brew --prefix 2>/dev/null || echo /usr/local)"
mkdir -p "$(dirname "$out")"
cc -O2 -Wall -o "$out" "$here/launcher.c" \
  -I"$prefix/include" -L"$prefix/lib" -lkrun -Wl,-rpath,"$prefix/lib"
if [[ "$(uname -s)" == "Darwin" ]]; then
  codesign --force --sign - --entitlements "$here/entitlements.plist" "$out"
fi
echo "$out"
