#!/usr/bin/env bash
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "=== Athena GUI Dev Mode ==="
echo "Root: $ROOT"
echo ""

echo -n "[1/2] Checking Python... "
uv run python -c "from gui_gateway.__main__ import start_server; print('OK')" 2>/dev/null
echo "OK"

echo "[2/2] Starting Tauri dev server..."
echo "  - Python backend auto-spawns on first request"
echo "  - Frontend at http://localhost:1421"
echo "  - Ctrl+C to stop"
echo ""

cd athena-gui
npm run tauri dev
