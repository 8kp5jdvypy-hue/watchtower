# Signal incident reconstruction

`tradebot.signal_incident_reconstruction` is the read-only forensic path for
questions such as “how did Perch miss GPRO?”  It deliberately does not answer
that question from a chart or from the absence of one candidate row.  The word
“caught” can mean at least four different things:

1. a provider or full-universe lane admitted and evaluated the symbol;
2. a detector or shadow lane recorded a qualifying candidate;
3. an owner-only notification was placed in the outbox; or
4. a customer alert was delivered.

The report preserves those facts separately.  It only calls a direction missed
when an invariant-checked full-universe census explicitly recorded a missed
direction.  Otherwise the broad caught/missed verdict remains unproven.

## Sources

For one symbol and XNYS session the report inspects, when present:

- asset identity and active/tradable state;
- live Stage-1 ticks/events or a digest-verified immutable screening archive;
- bar evaluations, detections, decisions, regular-session marks, catalysts,
  and catalyst-ingestion provenance;
- final-RTH ticks, symbol observations, candidates, handoff transitions, and
  the independent missed-mover census;
- scheduled and market-wide postmarket observations/candidates;
- lifecycle observations/transitions, context, rank decomposition, and
  append-only outcome marks; and
- owner-operator outbox state keyed by the candidate ID.  Recipient identity
  and message text are not exported.

Each SQLite file is opened with `mode=ro`, `PRAGMA query_only=ON`, and a read
transaction.  Source path, size, mtime, schema version, data version, and
journal mode are recorded.  Mutable databases are not hashed as though a file
hash were an atomic snapshot.  The report states that its transactions across
separate database files are not cross-file atomic.

Required UTC timestamps, strict JSON (including duplicate keys), duplicate
identities, and stored-order timestamp regressions are checked.  Missing
databases/tables, incompatible schemas, corrupt archives, and query errors are
visible degraded states rather than empty-success results.

## Run

Run inside an application container so the reviewed dependency set is used:

```bash
docker compose run --rm -T runner \
  python3 -m tradebot.signal_incident_reconstruction GPRO 2026-08-31 \
  --data-dir data \
  --output-dir data/postmarket_evidence/incidents \
  --pretty --fail-on-degraded
```

The JSON artifact is published with no-replace semantics, read-only file mode,
and a SHA-256 bound into its filename.  The documented location is recursively
included in the encrypted postmarket artifact backup.  Re-running the same exact report does
not overwrite the first artifact.  Exit code `1` with `--fail-on-degraded`
means the report was still written but one or more requested evidence sources
was missing, malformed, unsafe, or schema-incompatible.  Exit code `2` means
the reconstruction itself could not be built or published.

## Claim boundary

- A Stage-1 row proves Stage-1 evidence, not downstream evaluation.
- A candidate proves qualification in that named lane/version, not delivery.
- An outbox row proves queue state; its `status`/`delivered_at` fields determine
  whether the owner-only delivery record advanced.
- No row is never silently translated into “missed.”  It may mean the lane did
  not run, the schema predates the lane, evidence was not retained, the symbol
  was not selected, or the symbol was evaluated elsewhere.
- The utility performs no vendor call, no alert delivery, no broker action, and
  no order action.
