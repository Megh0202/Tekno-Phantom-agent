@echo off
echo Starting Tekno Phantom in dev mode (hot-reload)...
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
echo.
echo Done. Open http://localhost in your browser.
echo Backend changes reload automatically (~1s). Frontend changes appear instantly (HMR).
echo.
echo To view logs:
echo   docker compose logs -f backend
echo   docker compose logs -f frontend
