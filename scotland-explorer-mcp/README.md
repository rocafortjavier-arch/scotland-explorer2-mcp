# Scotland Explorer MCP

An **HTTP-based** MCP server for a Scotland campervan trip (**31 Jul – 24 Aug 2026**).

The previous build used the stdio transport, which Claude.ai mobile can't reach.
This one speaks **MCP over Streamable HTTP**, so it works as a Claude.ai connector on
desktop *and* mobile. The same four tools are also exposed as plain REST endpoints for
`curl`/Postman testing.

## Tools

| Tool | Data source | What it returns |
| --- | --- | --- |
| `get_scotland_weather` | [Open-Meteo](https://open-meteo.com) | 7-day forecast per region with temps, rain %, wind, and a campervan-suitability score |
| `get_daylight_times` | Astral + pytz | Sunrise, sunset, civil twilight, daylight hours, planning notes |
| `get_road_incidents_scotland` | Traffic Scotland live API | Current incidents/roadworks with location, type, severity, last updated |
| `find_campervan_essentials` | OpenStreetMap Overpass | Nearby fuel, supermarkets, water/waste points sorted by distance |
| `get_eclipse_viewing` | ephem (computed) + Open-Meteo | 12 Aug 2026 partial solar eclipse: local % covered, timing, Sun altitude, cloud, and a ranked list of best Scottish viewing spots |
| `get_midge_forecast` | Open-Meteo (computed) | Midge risk 1–5 for an evening from wind, temp and humidity — Highland camping essential in August |
| `find_overnight_spots` | OpenStreetMap Overpass | Campsites, motorhome/caravan sites and stopovers (with a free-only filter) plus Scotland wild-camping note |
| `find_attractions` | OpenStreetMap Overpass | Nearby distilleries, castles, viewpoints and attractions, sorted by distance |
| `find_pubs` | OpenStreetMap Overpass | Nearby pubs/bars, filterable by real ale, food, step-free entry or outdoor seating |
| `get_train_departures` | National Rail (Darwin) via Huxley2 | Live train departures/arrivals for Scottish stations: scheduled vs expected, platform, cancellations, disruption notices. No API key |

Every tool degrades gracefully: on an upstream failure it returns
`{"ok": false, "error": ..., "fallback": <a human URL to try instead>}` rather than crashing.

## Endpoints

```
GET  /health                              -> {"status":"ok","tools":[...]}
GET  /                                     -> service info
POST /mcp                                  -> MCP Streamable HTTP (JSON-RPC) — the connector endpoint
POST /tools/get_scotland_weather          -> {"region": "Glen Coe"}
POST /tools/get_daylight_times            -> {"latitude": 56.6, "longitude": -5.1, "date_str": "2026-07-31"}
POST /tools/get_road_incidents_scotland   -> {"region": "A82"}
POST /tools/find_campervan_essentials     -> {"latitude": 56.8, "longitude": -5.1, "radius_km": 20, "essential_type": "fuel"}
POST /tools/get_eclipse_viewing           -> {"region": "Isle of Skye"}   (or {} to rank spots, or {"latitude":.., "longitude":..})
POST /tools/get_midge_forecast            -> {"region": "Glen Coe", "date_str": "2026-08-05"}
POST /tools/find_overnight_spots          -> {"latitude": 57.27, "longitude": -6.21, "radius_km": 20, "spot_type": "free"}
POST /tools/find_attractions              -> {"latitude": 57.19, "longitude": -3.82, "radius_km": 30, "category": "distillery"}
POST /tools/find_pubs                     -> {"latitude": 55.9503, "longitude": -3.1866, "radius_km": 5, "filter_by": "real_ale"}
POST /tools/get_train_departures          -> {"station": "Fort William", "destination": "Glasgow", "rows": 8}
```

Interactive REST docs (FastAPI) live at `/docs`.

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate           # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py                       # serves on http://0.0.0.0:8080
```

Then in another terminal:

```bash
./test_local.sh                     # smoke-tests every endpoint
# or point it at production:
./test_local.sh https://scotland-explorer2-mcp-production.up.railway.app
```

## Deploy to Railway

Railway builds with Nixpacks and runs the `startCommand` in `railway.json`
(`uvicorn app:app --host 0.0.0.0 --port $PORT`). Railway injects `$PORT`; locally we
default to `8080`.

```bash
git init
git add .
git commit -m "Scotland Explorer MCP (HTTP)"
railway up            # or: git push to a Railway-connected repo
```

Health checks hit `/health`. Once deployed, the MCP endpoint is:

```
https://scotland-explorer2-mcp-production.up.railway.app/mcp
```

## Add to Claude.ai (desktop + mobile)

1. Claude.ai → **Settings → Connectors → Add custom connector**.
2. Name: `Scotland Explorer`. URL: the `/mcp` URL above.
3. Save. The four tools appear in chat and work on the mobile app.

## Notes

- The documented `www.traffic.gov.scot/api/v2/incidents` path 404s; the live feed the
  Traffic Scotland mobile site actually uses is
  `https://myapi.trafficscotland.org/v2.0/layers/current-incidents`, which this server
  calls (fetching per-incident detail concurrently, capped for speed).
- Overpass requests send a `User-Agent: Scotland-Explorer-MCP/1.0` header, as required.
- The server runs **stateless** MCP (`stateless_http=True`), so it needs no sticky
  sessions behind Railway's load balancer.
