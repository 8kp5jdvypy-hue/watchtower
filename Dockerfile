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

# data/ is a volume mount at runtime (see docker-compose.yml), never
# baked into the image — it's the one thing that must survive a
# container rebuild.
RUN mkdir -p data

# No ENTRYPOINT/CMD here on purpose — each docker-compose service sets
# its own `command:` (worker / bot / runner), same image, different
# process, matching what scripts/start.sh already does on bare metal.
