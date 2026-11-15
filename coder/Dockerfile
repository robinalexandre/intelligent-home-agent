# The coder: Claude Code through the Agent SDK (which bundles the Claude Code binary) for the
# jobs, the Claude Code CLI for Remote Control, git and gh, Python and Node for the
# repositories' own test suites. Add what your repositories need to build and test here.
# Built from the repository root (compose/coder.yml): docker build -f coder/Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs gh \
    && npm install -g @anthropic-ai/claude-code@2.1.325 \
    && rm -rf /var/lib/apt/lists/* /root/.npm

# A wheel, so the Claude Code binary comes with the SDK.
RUN pip install --only-binary=claude-agent-sdk claude-agent-sdk \
        "fastapi>=0.115" "uvicorn[standard]>=0.30" \
    && useradd --create-home --uid 10002 coder && mkdir /work && chown coder /work

COPY coder /app/coder
COPY agents /app/agents
WORKDIR /app
RUN chmod +x /app/coder/entrypoint.sh
USER coder
# Transcripts on the volume, so a session resumes after a restart.
ENV HOME=/home/coder CODER_WORK=/work CLAUDE_CONFIG_DIR=/work/.claude
EXPOSE 8100
VOLUME ["/work"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health')"

ENTRYPOINT ["/app/coder/entrypoint.sh"]
