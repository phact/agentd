#!/usr/bin/env bash
# Build the binaries that ship in agentd's platform wheels into agentd/sandbox/bin,
# and record them in prebuilt.json (see agentd/sandbox/prebuilt.py).
#
#   Linux (run in a manylinux_2_28 container, as root): libkrun with networking,
#     the launcher and agentd-net for this arch.
#   macOS (needs Homebrew's libkrun and cargo): the signed launcher and agentd-net.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
sandbox="$root/agentd/sandbox"
bin="$sandbox/bin"
mkdir -p "$bin"
pin() { sed -n "s/^$1 = \"\(.*\)\"$/\1/p" "$sandbox/colima.py"; }
version="$(pin LIBKRUN_VERSION)"
sha="$(pin LIBKRUN_SHA256)"

if [[ "$(uname -s)" == "Darwin" ]]; then
  target=darwin
  export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-14.0}"
else
  arch="$(uname -m)"
  target="linux-$arch"
  [ -d /opt/python/cp312-cp312/bin ] && export PATH="/opt/python/cp312-cp312/bin:$PATH"  # manylinux
  command -v cargo >/dev/null || [ -x ~/.cargo/bin/cargo ] ||
    curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal
  export PATH="$HOME/.cargo/bin:$PATH"
  if command -v dnf >/dev/null; then dnf install -y -q clang-devel patchelf glibc-static >/dev/null; fi
  work="$(mktemp -d)"
  curl -fsSL -o "$work/src.tgz" "https://github.com/libkrun/libkrun/archive/refs/tags/v$version.tar.gz"
  echo "$sha  $work/src.tgz" | sha256sum -c -
  tar -xzf "$work/src.tgz" -C "$work"
  make -C "$work/libkrun-$version" NET=1 -j"$(nproc)"
  make -C "$work/libkrun-$version" install PREFIX=/usr/local
  install -m 755 "/usr/local/lib64/libkrun.so.$version" "$bin/libkrun-$target.so"
  strip "$bin/libkrun-$target.so"
  rm -rf "$work"
fi

"$sandbox/build.sh"
if [[ "$target" != darwin ]]; then strip "$bin/agentd-krun-$target" "$bin/agentd-net-$target"; fi
[ -x "$bin/$([[ $target == darwin ]] && echo agentd-net || echo "agentd-net-$target")" ] ||
  { echo "agentd-net was not built (needs cargo)" >&2; exit 1; }

python3 - "$bin/prebuilt.json" "$target" "$sandbox/launcher.c" "$version" <<'PY'
import hashlib, json, sys
path, target, launcher, version = sys.argv[1:]
try:
    m = json.load(open(path))
except (OSError, ValueError):
    m = {}
m["launcher"] = hashlib.sha256(open(launcher, "rb").read()).hexdigest()[:16]
m["libkrun"] = f"{version}+net"
m["targets"] = sorted(set(m.get("targets", [])) | {target})
json.dump(m, open(path, "w"), indent=1)
print(json.dumps(m))
PY
