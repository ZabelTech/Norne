# Orchestrator image for the GitHub agentic pipeline.
# Runs on a single persistent Fly Machine. Subscription-only:
#   - Claude  -> Claude Code CLI authed with CLAUDE_CODE_OAUTH_TOKEN (Max)
#   - GLM     -> z.ai GLM Coding Plan via Pi (or Claude Code pointed at z.ai)
# IMPORTANT: never put ANTHROPIC_API_KEY in this image or in fly secrets,
# or Claude Code will bill per-token instead of using your subscription.

FROM node:22-bookworm-slim

# System deps: git (agents commit/push), ca-certs, tini for clean signals.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates curl tini python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/*

# Agent CLIs. Pin if you want reproducibility.
RUN npm install -g @anthropic-ai/claude-code @mariozechner/pi-coding-agent

# Non-root user (Pi has no permission sandbox; the Machine is the boundary,
# but we still avoid running as root inside it).
RUN useradd -m -u 10001 agent
WORKDIR /app

# Python orchestrator deps.
COPY requirements.txt .
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
ENV PATH="/opt/venv/bin:${PATH}"

COPY orchestrator/ ./orchestrator/
COPY CLAUDE.md ./CLAUDE.md

# Data dir is a Fly Volume mount (ledger + per-issue metadata persist here).
ENV DATA_DIR=/data
ENV WORKDIR_ROOT=/data/work
RUN mkdir -p /data && chown -R agent:agent /app /data

USER agent
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "-m", "orchestrator.main"]
