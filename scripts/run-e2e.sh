#!/usr/bin/env bash
# Run the full E2E test suite against a local Docker stack.
# Usage: bash scripts/run-e2e.sh
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== [1/5] Creating test video fixture ==="
bash scripts/create_test_fixture.sh

echo ""
echo "=== [2/5] Starting test stack (Whisper base.en) ==="
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

echo ""
echo "=== [3/5] Waiting for backend to be healthy ==="
TRIES=0
until curl -sf http://localhost/api/health > /dev/null 2>&1; do
    TRIES=$((TRIES + 1))
    if [ "$TRIES" -ge 40 ]; then
        echo "ERROR: Backend did not become healthy after 120 seconds." >&2
        docker compose logs --tail=50
        exit 1
    fi
    echo "  waiting... ($TRIES/40)"
    sleep 3
done
echo "  Backend is healthy."

echo ""
echo "=== [4/5] API integration tests (pytest) ==="
pip install -q -r backend/requirements-test.txt
pytest backend/tests/ -v --tb=short

echo ""
echo "=== [5/5] Browser E2E tests (Playwright) ==="
cd e2e
npm install --silent
npx playwright install --with-deps chromium
npx playwright test

echo ""
echo "=== All E2E tests passed ==="
echo "Tear down with: docker compose down"
