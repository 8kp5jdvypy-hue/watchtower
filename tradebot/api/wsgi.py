"""Production entrypoint for gunicorn: `gunicorn tradebot.api.wsgi:app`.

Separate from tradebot/api/app.py on purpose — create_app() opens real
sqlite connections to data/users.db and data/journal.db as a side
effect, and that must only happen when something actually intends to
serve requests. Importing this module does trigger that (module-level
`app = create_app()` below) — that's fine here, since nothing except a
real WSGI server ever imports tradebot.api.wsgi. Tests import
create_app directly from tradebot.api.app instead, with tmp_path
databases, and never touch this file.
"""
from __future__ import annotations

from tradebot.api.app import create_app

app = create_app()
