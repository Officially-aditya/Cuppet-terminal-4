FROM node:22.21.0-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git ripgrep procps \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @openai/codex@0.162.1
WORKDIR /app
RUN printf 'cuppet codex preflight\n' > /app/input.txt
COPY check.sh /app/check.sh
RUN chown -R nobody:nogroup /app
ENV HOME=/tmp
USER nobody
