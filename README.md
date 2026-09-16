# lock-step-be

Backend for the GST ITC early-warning system. Read `CLAUDE.md` first — it holds the
product thesis, the GST domain rules, the schema and the matching tiers.

**The short version:** GSTR-2B is generated on the 14th from supplier filings made by
the 13th, so every tool built on 2B reports a missing invoice after the deadline that
decided the outcome. This one works in the 1st-to-13th window, while the vendor can
still act.

## Stack

FastAPI (async) + SQLAlchemy 2.0 (async) + Postgres, `uv` for dependencies, Anthropic
Claude for per-vendor summaries only. Matching is deterministic — no LLM in that path.

## Run it

```powershell
.\start.ps1           # containers, deps, migrations, demo data, API on :8010
.\start.ps1 -Reset    # same, but rebuild the demo data first
```

That is the only command. It is idempotent, so it is also the reboot command.
API docs: http://localhost:8010/docs · login `demo@lockstep.test` / `lockstep`.

The frontend lives in `../lock-step-fe` (`npm run dev`, proxies `/api` to :8010).

## Tests

```bash
uv run pytest          # 82 tests; every matching rule has a named fixture
uv run ruff check .
```

## Demo data

`uv run python -m lockstep.seed --reset` builds 20 vendors, 6 periods of filing
history and ~900 invoices across all ten statuses, deterministically.

**This data is seeded, not real — say so in the demo.** The prediction feature needs
filing history that a day-one deployment would not have, and being straight about that
is cheaper than being asked.

## Notes

- **`ANTHROPIC_API_KEY`** is optional. Without it, vendor summaries fall back to a
  deterministic sentence built from the same facts — nothing else changes, because
  statuses, amounts and risk bands are all rules.
- **`ANTHROPIC_MODEL`** defaults to `claude-opus-5`. One call per *affected vendor*
  (~20), not per invoice (~500), so cost is not the constraint it would otherwise be.
- **Storage** is local disk (`./data/uploads`) behind an S3-shaped interface
  (`lockstep.storage.StorageBackend`).
- **Ingestion** takes CSV and Excel behind `parse_file`, so a GSTN API source can
  produce the same `CanonicalRow` list later without touching anything downstream.
- A check runs inline (no queue): it is seconds of work, and a synchronous result lets
  the UI show the new delta immediately.
