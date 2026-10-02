# Sandbox base rootfs for agentd's harnesses (Claude Code, Codex, OpenCode, omp). Build with
#   python -m agentd.sandbox.rootfs build agentd/sandbox/images/agents agents
# libkrun boots the exported directory read-only (no Docker at runtime).
FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git ripgrep procps unzip \
 && rm -rf /var/lib/apt/lists/*
# Harnesses run as `agent`, created with the host user's uid/gid so files
# shared from the host (workspace, transcripts) are its own.
ARG AGENT_UID=1000
ARG AGENT_GID=1000
RUN groupadd -o -g "$AGENT_GID" agent \
 && useradd -o -u "$AGENT_UID" -g "$AGENT_GID" --create-home --shell /bin/bash agent
# Codex CLI: the full release package (codex + its code-mode host, rg, bwrap),
# which Codex expects laid out next to its real binary. Voice is not needed.
ARG CODEX_VERSION=0.159.2
RUN arch="$(uname -m)" && mkdir -p /opt/codex \
 && curl -fsSL "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-package-${arch}-unknown-linux-musl.tar.gz" \
    | tar -xz -C /opt/codex \
 && rm -rf /opt/codex/codex-resources/voice \
 && ln -sf /opt/codex/bin/codex /usr/local/bin/codex \
 && codex --version
# Bun (omp runs on it), then omp (oh-my-pi) from npm, pinned.
ARG BUN_VERSION=1.4.2
ARG OMP_VERSION=18.4.10
ENV BUN_INSTALL=/opt/bun
RUN curl -fsSL https://bun.sh/install | bash -s "bun-v${BUN_VERSION}" >/dev/null \
 && ln -sf /opt/bun/bin/bun /usr/local/bin/bun \
 && bun install -g "@oh-my-pi/pi-coding-agent@${OMP_VERSION}" \
 && ln -sf /opt/bun/bin/omp /usr/local/bin/omp \
 && chmod -R a+rX /opt/bun \
 && omp --version
# OpenCode: its prebuilt Linux binary, taken straight from the npm package for this arch.
ARG OPENCODE_VERSION=1.18.34
RUN case "$(uname -m)" in aarch64) a=arm64 ;; x86_64) a=x64 ;; *) echo "unsupported arch" >&2; exit 1 ;; esac \
 && curl -fsSL "https://registry.npmjs.org/opencode-linux-${a}/-/opencode-linux-${a}-${OPENCODE_VERSION}.tgz" \
    | tar -xz -C /tmp \
 && install -m 755 /tmp/package/bin/opencode /usr/local/bin/opencode && rm -rf /tmp/package \
 && opencode --version
USER agent
RUN curl -fsSL https://claude.ai/install.sh | bash && /home/agent/.local/bin/claude --version
USER root
RUN ln -sf /home/agent/.local/bin/claude /usr/local/bin/claude
