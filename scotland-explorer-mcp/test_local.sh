#!/usr/bin/env bash
# Smoke-test every endpoint against a running server.
# Usage: ./test_local.sh            (defaults to http://localhost:8080)
#        ./test_local.sh https://scotland-explorer2-mcp-production.up.railway.app
set -euo pipefail
BASE="${1:-http://localhost:8080}"
echo "Testing $BASE"
echo

echo "== GET /health =="
curl -fsS "$BASE/health"; echo; echo

echo "== POST /tools/get_scotland_weather (Isle of Skye) =="
curl -fsS -X POST "$BASE/tools/get_scotland_weather" \
  -H 'Content-Type: application/json' \
  -d '{"region":"Isle of Skye"}' | head -c 300; echo; echo

echo "== POST /tools/get_daylight_times (Glen Coe, trip start) =="
curl -fsS -X POST "$BASE/tools/get_daylight_times" \
  -H 'Content-Type: application/json' \
  -d '{"latitude":56.6,"longitude":-5.1,"date_str":"2026-07-31"}'; echo; echo

echo "== POST /tools/get_road_incidents_scotland (A82) =="
curl -fsS -X POST "$BASE/tools/get_road_incidents_scotland" \
  -H 'Content-Type: application/json' \
  -d '{"region":"A82"}' | head -c 300; echo; echo

echo "== POST /tools/find_campervan_essentials (Fort William, fuel) =="
curl -fsS -X POST "$BASE/tools/find_campervan_essentials" \
  -H 'Content-Type: application/json' \
  -d '{"latitude":56.8198,"longitude":-5.1052,"radius_km":15,"essential_type":"fuel"}' | head -c 300; echo; echo

echo "== POST /tools/get_eclipse_viewing (rank best spots) =="
curl -fsS -X POST "$BASE/tools/get_eclipse_viewing" \
  -H 'Content-Type: application/json' \
  -d '{}' | head -c 300; echo; echo

echo "== POST /tools/get_midge_forecast (Glen Coe) =="
curl -fsS -X POST "$BASE/tools/get_midge_forecast" \
  -H 'Content-Type: application/json' \
  -d '{"region":"Glen Coe"}' | head -c 300; echo; echo

echo "== POST /tools/find_overnight_spots (Skye) =="
curl -fsS -X POST "$BASE/tools/find_overnight_spots" \
  -H 'Content-Type: application/json' \
  -d '{"latitude":57.27,"longitude":-6.21,"radius_km":25,"spot_type":"all"}' | head -c 300; echo; echo

echo "== POST /tools/find_attractions (distilleries near Aviemore) =="
curl -fsS -X POST "$BASE/tools/find_attractions" \
  -H 'Content-Type: application/json' \
  -d '{"latitude":57.19,"longitude":-3.82,"radius_km":30,"category":"distillery"}' | head -c 300; echo; echo

echo "== POST /tools/find_pubs (Royal Mile, real ale) =="
curl -fsS -X POST "$BASE/tools/find_pubs" \
  -H 'Content-Type: application/json' \
  -d '{"latitude":55.9503,"longitude":-3.1866,"radius_km":2,"filter_by":"real_ale"}' | head -c 300; echo; echo

echo "== POST /tools/get_train_departures (Fort William) =="
curl -fsS -X POST "$BASE/tools/get_train_departures" \
  -H 'Content-Type: application/json' \
  -d '{"station":"Fort William","rows":4}' | head -c 300; echo; echo

echo "== POST /mcp  (MCP JSON-RPC tools/list) =="
curl -fsS -X POST "$BASE/mcp" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | head -c 400; echo; echo

echo "All endpoints responded."
