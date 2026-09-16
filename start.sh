#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# 0. Ensure uv is available
if ! command -v uv &> /dev/null; then
  echo "⬇️  Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# 1. Install dependencies
echo "📦 Installing dependencies..."
uv sync --group dev

# 2. Create .env if missing
if [ ! -f .env ]; then
  echo "📝 Creating .env from .env.example..."
  cp .env.example .env
  echo "⚠️  Edit .env to set ANTHROPIC_API_KEY and JWT_SECRET before using the app."
fi

# 3. Start Postgres & Redis
echo "🐳 Starting Postgres & Redis..."
docker compose up -d
echo "⏳ Waiting for Postgres to be ready..."
until docker compose exec -T postgres pg_isready -U lockstep > /dev/null 2>&1; do
  sleep 1
done
echo "✅ Postgres is ready."

# 4. Run migrations
echo "🗃️  Running database migrations..."
uv run alembic upgrade head

# 5. Start Celery worker in background
echo "🔧 Starting Celery worker..."
uv run celery -A lockstep.celery_app worker --loglevel=info -P solo &
CELERY_PID=$!

# 6. Start API server (foreground)
echo "🚀 Starting API server on http://localhost:8010/docs"
uv run uvicorn lockstep.main:app --reload --port 8010

# Cleanup on exit
kill $CELERY_PID 2>/dev/null || true
