#!/usr/bin/env bash
# Build the agentd-krun launcher (and, on macOS, ad-hoc sign it: Hypervisor.framework
# only runs in binaries carrying the com.apple.security.hypervisor entitlement).
# Output: bin/agentd-krun on macOS, bin/agentd-krun-linux-<arch> on Linux, so a
# checkout shared between the two keeps both.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
if [[ "$(uname -s)" == "Darwin" ]]; then
  default_out="$here/bin/agentd-krun"
  prefix="$(brew --prefix 2>/dev/null || echo /usr/local)"
  libs=(-L"$prefix/lib" -Wl,-rpath,"$prefix/lib")
  inc="$prefix/include"
else
  default_out="$here/bin/agentd-krun-linux-$(uname -m)"
  prefix="${LIBKRUN_PREFIX:-/usr/local}"
  # libkrun's `make install` puts the library in lib64.
  libs=(-L"$prefix/lib64" -L"$prefix/lib" -Wl,-rpath,"$prefix/lib64" -Wl,-rpath,"$prefix/lib")
  inc="$prefix/include"
fi
out="${1:-$default_out}"
mkdir -p "$(dirname "$out")"
cc -O2 -Wall -o "$out" "$here/launcher.c" -I"$inc" "${libs[@]}" -lkrun
if [[ "$(uname -s)" == "Darwin" ]]; then
  codesign --force --sign - --entitlements "$here/entitlements.plist" "$out"
fi
echo "$out"

# agentd-net (Rust): the network card of sandboxes that get one. Optional: without
# cargo, sandboxes simply can't be given a network.
if command -v cargo >/dev/null 2>&1; then
  if [[ "$(uname -s)" == "Darwin" ]]; then net_out="$here/bin/agentd-net"; else net_out="$here/bin/agentd-net-linux-$(uname -m)"; fi
  cargo build --release --quiet --manifest-path "$here/net/Cargo.toml"
  install -m 755 "$here/net/target/release/agentd-net" "$net_out"
  echo "$net_out"
else
  echo "cargo not found: skipping agentd-net (sandboxes can't get a network card)" >&2
fi
