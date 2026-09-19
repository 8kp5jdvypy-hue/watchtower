"""APNs push delivery for the iOS app — M2 of docs/telegram-sunset-plan-2026-09.md.

Deliberately independent of tradebot.telegram_bot (M4 deletes that
package): its own tables in users.db (push.store), its own worker
(push.worker), its own sender (push.apns). It dual-runs with Telegram
through runner's subscriber_hook: every HIGH alert reaches both
channels with the same alert_id, so M3's parity gate can compare
delivery per alert.
"""
