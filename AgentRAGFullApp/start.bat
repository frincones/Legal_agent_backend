@echo off
REM ============================================
REM Agent RAG Full App - Windows launcher
REM Starts backend (FastAPI) and frontend (Vite) in separate windows
REM ============================================

echo Starting Agent RAG Full App...
echo.

start "Agent RAG Backend" cmd /k "cd /d %~dp0backend && python main.py"
timeout /t 2 /nobreak >nul
start "Agent RAG Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

echo.
echo Backend:  http://localhost:8000
echo API docs: http://localhost:8000/docs
echo Frontend: http://localhost:5173
echo.
echo Press any key to close this launcher window...
pause >nul
