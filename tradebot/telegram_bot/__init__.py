"""The Telegram command layer — per-user onboarding, journaling of trades
users actually took, personal limits/pauses, and account admin. This is
still read-only with respect to the market: no command here ever places
an order or touches a broker (see CLAUDE.md). It only lets a user record
what THEY did and lets the bot decide who gets which alerts.
"""
