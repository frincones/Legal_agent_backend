#!/usr/bin/env bash
# ============================================
# Agent RAG Full App - Bash launcher
# Starts backend (FastAPI) and frontend (Vite) in parallel
# ============================================

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

cleanup() {
    echo ""
    echo "Stopping..."
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    exit 0
}
trap cleanup INT TERM

echo "Starting backend..."
cd "$DIR/backend"
python main.py &
BACKEND_PID=$!

sleep 2

echo "Starting frontend..."
cd "$DIR/frontend"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "Backend:  http://localhost:8000"
echo "API docs: http://localhost:8000/docs"
echo "Frontend: http://localhost:5173"
echo ""
echo "Press Ctrl+C to stop both."

wait
