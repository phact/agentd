# Releasing

1. Bump `version` in `pyproject.toml`, commit (`vX.Y.Z: what changed`).
2. `git tag vX.Y.Z && git push origin main vX.Y.Z`.

The tag runs `.github/workflows/release.yml`:

- **linux** (manylinux_2_28 containers on x86_64 and aarch64 runners) and **macos** (macos-14) run `scripts/build-binaries.sh`: libkrun with networking (Linux), the launcher and `agentd-net`.
- **wheels** runs `scripts/build-wheels.sh`: `macosx_14_0_arm64` (darwin + linux-aarch64 binaries, for Colima), `manylinux_2_28_x86_64`, `manylinux_2_28_aarch64`, a pure `py3-none-any` wheel and the sdist. The tag must match the version.
- **test** installs each wheel on its platform and runs `scripts/check-wheel.py`.
- **publish** uploads to PyPI with trusted publishing (environment `pypi`).

Run the workflow by hand (Actions → release → Run workflow) to build and test without publishing.

Locally: `scripts/build-binaries.sh` on a Mac builds the darwin binaries; for Linux run it in `quay.io/pypa/manylinux_2_28_<arch>`. `AGENTD_WHEEL_PLATFORM=<tag> uv build --wheel` then makes a platform wheel from what's in `agentd/sandbox/bin`.
