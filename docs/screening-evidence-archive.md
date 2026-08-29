# Stage-1 screening evidence archive

`universe.db` contains two different kinds of state: the rebuildable current
asset catalog and the irrebuildable Stage-1 screening funnel. The latter is the
only answer to questions such as “was this symbol screened, rejected, stale,
or promoted at this tick?” It must survive long-term database retention.

`tradebot.screening_archive` creates the durable prerequisite for retention.
It does **not** delete, update, vacuum, or prune the source database.

## Archive contract

One artifact contains every `screening_ticks` row and every joined
`screening_events` row for one completed XNYS session. The first JSONL record
pins:

- schema version and exact session;
- exact tick/event column contracts;
- tick and event counts;
- first and last tick timestamps; and
- the count of failed conservation invariants.

Tick and event records follow in primary-key order. The writer holds one
read-only SQLite transaction, uses canonical JSON, a fixed gzip timestamp, and
no embedded source filename. Identical source evidence therefore produces
identical bytes and the same SHA-256-addressed filename:

```text
data/screening_archives/screening_<session>_<sha256>.jsonl.gz
```

Publication uses a same-directory temporary file, flush, `fsync`, and an
atomic hard-link. Published files are read-only. A same-content retry is an
idempotent success; a later source append creates a new digest without
replacing the earlier artifact. The source tables themselves reject updates
and deletes, so any post-archive change can only append rows and must change
the manifest reconciliation counts.

The independent verifier recomputes the filename digest and reconciles schema,
session, column sets, primary-key ordering, event-to-tick references, counts,
timestamps, and invariant failures. A malformed or corrupt existing artifact
fails the nightly job loudly instead of being hidden by a replacement.

## Scheduling and catch-up

`perch-screening-archive.timer` runs at 02:30 UTC, before the 03:00 backup. A
session is eligible only after its actual XNYS close plus 15 minutes. The job
archives every closed session lacking an artifact that both verifies and
reconciles to the current append-only source summary, not merely the newest.
Downtime and late appends therefore cannot create a permanent hole. Weekends
and fully reconciled sessions are clean no-ops.

The exact-revision deployment wrapper installs and enables both archive units.
For a manual run or verification:

```bash
docker compose exec -T runner python3 -m tradebot.screening_archive
docker compose exec -T runner python3 -m tradebot.screening_archive \
  --verify data/screening_archives/screening_<session>_<sha256>.jsonl.gz
```

## Backup and restore custody

`scripts/backup.sh` includes `data/screening_archives/` in the encrypted
off-box artifact set. `scripts/verify_backup.py` fully verifies every screening
archive both when creating a backup and during isolated restore; a safe tar
path and matching outer manifest are not sufficient on their own.

## Deliberate non-goal of this release

No retention deletion exists yet. A later, separately reviewed pruning change
must first prove all of the following against real production artifacts:

1. multiple scheduled archives and downtime catch-up completed;
2. encrypted off-box backup and isolated restore preserved exact artifacts;
3. restored archive counts and identities reconcile to their source sessions;
4. the proposed retention boundary cannot select an unarchived or unverified
   session; and
5. failure injection leaves `universe.db` untouched.

Until then, archive creation reduces future retention risk without claiming
that destructive pruning is safe.
