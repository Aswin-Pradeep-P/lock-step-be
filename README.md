# lock-step-be

Lockstep backend: GST ITC reconciliation. Upload a purchase-ledger CSV and a GSTR-2B CSV,
get every invoice matched (exact / clerical typo / missing) and risk-classified (safe /
low risk / high risk / cannot be claimed) with an AI-written explanation, statutory
citations, and — where still recoverable — the Sec 16(4) deadline.

See `/home/rahul/.claude/plans/splendid-brewing-bachman.md` for the full architecture writeup.

## Stack

FastAPI (async) + SQLAlchemy 2.0 (async) + Postgres, Celery + Redis for the reconciliation
pipeline, Anthropic Claude for per-invoice risk classification, `uv` for dependency management.

## Setup

```bash
uv sync --group dev
cp .env.example .env   # fill in ANTHROPIC_API_KEY for real AI summaries
docker compose up -d   # postgres (5432) + redis (6380 — 6379 was already taken locally)
uv run alembic upgrade head
```

## Running

```bash
# Terminal 1
uv run uvicorn lockstep.main:app --reload --port 8010

# Terminal 2
uv run celery -A lockstep.celery_app worker --loglevel=info -P solo
```

API docs: http://localhost:8010/docs

## Testing

```bash
uv run pytest
uv run ruff check .
```

## Notes

- **`ANTHROPIC_MODEL`** defaults to `claude-opus-5`. This call runs once per non-exact-match
  invoice, so at high monthly invoice volumes, switching to `claude-sonnet-5` (2.5x cheaper)
  may be worth it — it's a one-line env change, not a code change.
- **Storage** is local disk (`./data/uploads`) behind an S3-shaped interface
  (`lockstep.storage.StorageBackend`). Swapping to real S3 later means writing one new
  class with the same `save`/`read` signatures.
- Without a valid `ANTHROPIC_API_KEY`, invoices still get matched and bucketed correctly —
  the AI classifier falls back to a deterministic risk tier and a placeholder summary
  instead of failing the run.
