# Sandbox base rootfs for agentd's harnesses (Claude Code, Codex). Build with
#   python -m agentd.sandbox.rootfs build agentd/sandbox/images/agents agents
# libkrun boots the exported directory read-only (no Docker at runtime).
FROM python:3.11-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates git ripgrep procps \
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
USER agent
RUN curl -fsSL https://claude.ai/install.sh | bash && /home/agent/.local/bin/claude --version
USER root
RUN ln -sf /home/agent/.local/bin/claude /usr/local/bin/claude
