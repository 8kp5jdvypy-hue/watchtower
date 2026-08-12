# One image for all three long-running processes (worker, bot, runner) —
# they share the exact same code and dependencies; only the CMD differs
# per docker-compose service. Building three near-identical images would
# just be the same layers three times.
FROM python:3.11-slim

# sqlite3 CLI is useful for debugging/backups inside the container;
# everything else here is what alpaca-py/pandas need to build wheels on
# a slim base.
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tradebot/ tradebot/

# The image has no .git (only tradebot/ is copied above), so
# journal.code_version() -- which stamps every detection row with the
# code that produced it -- always fell through to "unknown" in
# production (found 2026-08-12). Baked in at build time instead; see
# docker-compose.yml's build.args and docs/DEPLOYMENT.md's deploy
# routine for where GIT_SHA actually comes from.
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

# data/ is a volume mount at runtime (see docker-compose.yml), never
# baked into the image — it's the one thing that must survive a
# container rebuild.
RUN mkdir -p data

# No ENTRYPOINT/CMD here on purpose — each docker-compose service sets
# its own `command:` (worker / bot / runner), same image, different
# process, matching what scripts/start.sh already does on bare metal.
