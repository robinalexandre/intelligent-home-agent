FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Optional parts of the agent (pyproject.toml): "documents" for PDFs, forms and drawings.
# Add "video" for MP4 animations (some 40 MB more): --build-arg EXTRAS=documents,video.
# Add "subscription" for Claude on a Claude plan (docs/subscription.md; Claude Code
# itself, some 400 MB): EXTRAS=documents,subscription.
# "voice" transcribes the voice messages sent in a chat (docs/chat.md).
# Empty for the smallest image: --build-arg EXTRAS= --build-arg FONT_PACKAGES=
ARG EXTRAS=documents
# Fonts for the PDFs and drawings the agent makes: DejaVu covers Latin with accents, Greek,
# Cyrillic and currency signs in 3 MB. For other scripts see docs/documents.md.
ARG FONT_PACKAGES=fonts-dejavu-core

WORKDIR /app
RUN if [ -n "$FONT_PACKAGES" ]; then apt-get update \
    && apt-get install -y --no-install-recommends $FONT_PACKAGES \
    && rm -rf /var/lib/apt/lists/*; fi
COPY pyproject.toml README.md ./
COPY src ./src
# claude-agent-sdk only as a wheel: its source package has no Claude Code in it.
RUN pip install --only-binary=claude-agent-sdk ".[crypt${EXTRAS:+,$EXTRAS}]" && useradd --create-home --uid 10001 agent \
    && mkdir /app/data && chown agent /app/data

USER agent
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["uvicorn", "intelligent_home_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
