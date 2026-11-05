# Bridge image: the bridge process. Codex and Claude install only when their build
# argument is 1, at the latest version at build time. The Python side is the stdlib
# only: no aiohttp. The CLI logins are not in the image: compose mounts them from
# SECRETS_DIR. The base is a Node minor tag.
FROM node:22-slim

ARG INSTALL_CODEX=0
ARG INSTALL_CLAUDE=0

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && if [ "$INSTALL_CODEX" = "1" ]; then npm install -g @openai/codex; fi \
    && if [ "$INSTALL_CLAUDE" = "1" ]; then npm install -g @anthropic-ai/claude-code; fi \
    && npm cache clean --force \
    && mkdir -p /app/cwd-codex /app/cwd-claude \
    && chown -R node:node /app

WORKDIR /app
COPY --chown=node:node bridge_cli.py /app/bridge_cli.py

USER node
ENV BRIDGE_HOST=0.0.0.0
CMD ["python3", "/app/bridge_cli.py"]
