"""Platform wheels: with AGENTD_WHEEL_PLATFORM set (e.g. macosx_14_0_arm64), the
wheel carries agentd/sandbox/bin (prebuilt by scripts/build-binaries.sh) and
that platform tag. Without it, a pure-Python wheel with no binaries."""
import os
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        platform = os.environ.get("AGENTD_WHEEL_PLATFORM")
        if self.target_name != "wheel" or not platform:
            return
        bin_dir = Path(self.root) / "agentd" / "sandbox" / "bin"
        if not (bin_dir / "prebuilt.json").exists():
            raise RuntimeError(f"no prebuilt binaries in {bin_dir}: run scripts/build-binaries.sh first")
        build_data["pure_python"] = False
        build_data["tag"] = f"py3-none-{platform}"
        for f in sorted(bin_dir.iterdir()):
            if f.is_file():
                build_data["force_include"][str(f)] = f"agentd/sandbox/bin/{f.name}"
