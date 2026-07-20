"""
Scotland Explorer MCP — HTTP server for a Scotland campervan trip (31 Jul–24 Aug 2026).

Two ways to reach the same four tools:

  1. REST (for curl / Postman / quick testing)
       GET  /health
       POST /tools/get_scotland_weather
       POST /tools/get_daylight_times
       POST /tools/get_road_incidents_scotland
       POST /tools/find_campervan_essentials

  2. MCP over Streamable HTTP (for Claude.ai desktop + mobile connectors)
       POST /mcp        (JSON-RPC, stateless — Railway friendly)

Every tool degrades gracefully: if its upstream API fails, it returns
{"ok": false, "error": ..., "fallback": <a human URL to try instead>}
instead of throwing, so the assistant can still give the user something useful.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Literal, Optional
from urllib.parse import urlencode

import ephem
import httpx
from opening_hours import OpeningHours
import pytz
from astral import LocationInfo
from astral.sun import sun
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Reference data
# --------------------------------------------------------------------------- #

# Named Scottish regions with representative coordinates. Keys are matched
# case-insensitively and loosely (see _resolve_region).
SCOTLAND_REGIONS: dict[str, tuple[float, float]] = {
    "edinburgh": (55.9533, -3.1883),
    "glasgow": (55.8642, -4.2518),
    "highlands": (57.5, -4.5),
    "inverness": (57.4778, -4.2247),
    "glen coe": (56.6, -5.1),
    "isle of skye": (57.5, -6.2),
    "fort william": (56.8198, -5.1052),
    "aviemore": (57.1958, -3.8258),
    "cairngorms": (57.0800, -3.6700),
    "loch ness": (57.3229, -4.4244),
    "oban": (56.4152, -5.4718),
    "stirling": (56.1165, -3.9369),
    "aberdeen": (57.1497, -2.0943),
    "john o groats": (58.6373, -3.0689),
    "loch lomond": (56.1000, -4.6200),
    "st andrews": (56.3398, -2.7967),
    "ullapool": (57.8955, -5.1600),
    "pitlochry": (56.7028, -3.7300),
}

SCOTLAND_TZ = "Europe/London"

# Open-Meteo WMO weather codes -> short human description.
WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snow",
    73: "Moderate snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

USER_AGENT = "Scotland-Explorer-MCP/1.0"
HTTP_TIMEOUT = httpx.Timeout(20.0, connect=10.0)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _resolve_region(region: str) -> Optional[tuple[str, float, float]]:
    """Loose lookup: exact, then substring either direction. Returns (name, lat, lon)."""
    if not region:
        return None
    key = region.strip().lower()
    if key in SCOTLAND_REGIONS:
        lat, lon = SCOTLAND_REGIONS[key]
        return key.title(), lat, lon
    for name, (lat, lon) in SCOTLAND_REGIONS.items():
        if key in name or name in key:
            return name.title(), lat, lon
    return None


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 2)


def _opening_status(raw: Optional[str]) -> dict[str, Any]:
    """Interpret an OSM opening_hours string into open/closed *right now*.

    In the Highlands this is the question that actually matters: a village shop open
    "Mo,We-Fr 09:00-12:00" is useless at 2pm on a Tuesday. OSM hours are often missing
    or stale, so we never claim certainty we don't have — unknown stays unknown, and
    the raw string is always returned so the user can judge for themselves.
    """
    if not raw:
        return {
            "state": "unknown",
            "raw": None,
            "summary": "Opening hours not recorded in OpenStreetMap — worth calling ahead.",
        }
    now = datetime.now(pytz.timezone(SCOTLAND_TZ)).replace(tzinfo=None)
    try:
        oh = OpeningHours(raw)
        is_open = bool(oh.is_open(now))
        nxt = oh.next_change(now)
    except Exception:  # noqa: BLE001 — unparseable hours are common in OSM
        return {
            "state": "unknown",
            "raw": raw,
            "summary": f"Hours listed as '{raw}' but couldn't be read reliably — check on arrival.",
        }

    out: dict[str, Any] = {"state": "open" if is_open else "closed", "raw": raw}
    if nxt is None:
        out["summary"] = "Open 24/7" if is_open else "Closed"
        return out

    stamp = nxt.strftime("%a %H:%M")
    if is_open:
        mins_left = int((nxt - now).total_seconds() // 60)
        out["closes_at"] = stamp
        left = f"{mins_left // 60}h {mins_left % 60}m" if mins_left >= 60 else f"{mins_left}m"
        out["summary"] = f"Open now — closes {stamp} ({left} left)"
    else:
        out["opens_at"] = stamp
        out["summary"] = f"Closed now — opens {stamp}"
    return out


def _camping_suitability(
    temp_min: Optional[float],
    temp_max: Optional[float],
    rain_prob: Optional[float],
    rain_mm: Optional[float],
    wind_kmh: Optional[float],
) -> dict[str, Any]:
    """Rough 0–100 campervan-friendliness score with a one-line reason."""
    score = 100
    reasons: list[str] = []

    if rain_prob is not None:
        if rain_prob >= 80:
            score -= 40
            reasons.append("very high chance of rain")
        elif rain_prob >= 50:
            score -= 20
            reasons.append("likely rain")
        elif rain_prob >= 30:
            score -= 10

    if rain_mm is not None and rain_mm >= 10:
        score -= 15
        reasons.append("heavy accumulated rain")

    if wind_kmh is not None:
        if wind_kmh >= 60:
            score -= 30
            reasons.append("strong winds")
        elif wind_kmh >= 40:
            score -= 15
            reasons.append("gusty")

    if temp_max is not None and temp_max < 10:
        score -= 15
        reasons.append("cold day")
    if temp_min is not None and temp_min < 3:
        score -= 15
        reasons.append("near-freezing nights")

    score = max(0, min(100, score))
    if score >= 75:
        rating = "great"
    elif score >= 55:
        rating = "good"
    elif score >= 35:
        rating = "marginal"
    else:
        rating = "poor"

    return {
        "score": score,
        "rating": rating,
        "notes": ", ".join(reasons) if reasons else "settled conditions",
    }


# --------------------------------------------------------------------------- #
# Core tool implementations (framework-agnostic, reused by REST + MCP)
# --------------------------------------------------------------------------- #

async def tool_get_scotland_weather(
    region: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    resolved = _resolve_region(region)
    if resolved is None:
        return {
            "ok": False,
            "error": f"Unknown region '{region}'.",
            "known_regions": sorted(n.title() for n in SCOTLAND_REGIONS),
            "fallback": "https://www.metoffice.gov.uk/weather/forecast/scotland",
        }

    name, lat, lon = resolved
    params: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "auto",
        "daily": ",".join(
            [
                "temperature_2m_max",
                "temperature_2m_min",
                "precipitation_probability_max",
                "precipitation_sum",
                "windspeed_10m_max",
                "weathercode",
            ]
        ),
    }
    if start_date and end_date:
        params["start_date"] = start_date
        params["end_date"] = end_date
    else:
        params["forecast_days"] = 7

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params=params,
                headers={"User-Agent": USER_AGENT},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "region": name,
            "error": f"Weather API failed: {exc}",
            "fallback": f"https://weather.com/weather/tenday/l/{lat},{lon}",
        }

    daily = data.get("daily", {})
    days: list[dict[str, Any]] = []
    dates = daily.get("time", [])
    for i, day in enumerate(dates):
        def g(key: str) -> Any:
            arr = daily.get(key) or []
            return arr[i] if i < len(arr) else None

        tmin = g("temperature_2m_min")
        tmax = g("temperature_2m_max")
        rain_prob = g("precipitation_probability_max")
        rain_mm = g("precipitation_sum")
        wind = g("windspeed_10m_max")
        code = g("weathercode")

        days.append(
            {
                "date": day,
                "temp_min": tmin,
                "temp_max": tmax,
                "conditions": WMO_CODES.get(code, f"Code {code}") if code is not None else None,
                "rain_probability": rain_prob,
                "rain_mm": rain_mm,
                "wind_kmh": wind,
                "camping_suitability": _camping_suitability(tmin, tmax, rain_prob, rain_mm, wind),
            }
        )

    return {
        "ok": True,
        "region": name,
        "coordinates": {"latitude": lat, "longitude": lon},
        "units": {"temp": "°C", "rain": "mm", "wind": "km/h"},
        "forecast": days,
    }


async def tool_get_daylight_times(
    latitude: float,
    longitude: float,
    date_str: Optional[str] = None,
) -> dict[str, Any]:
    tz = pytz.timezone(SCOTLAND_TZ)
    try:
        if date_str:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        else:
            target = datetime.now(tz).date()
    except ValueError:
        return {
            "ok": False,
            "error": f"Invalid date '{date_str}'. Use YYYY-MM-DD.",
            "fallback": f"https://www.timeanddate.com/sun/@{latitude},{longitude}",
        }

    try:
        loc = LocationInfo(latitude=latitude, longitude=longitude, timezone=SCOTLAND_TZ)
        s = sun(loc.observer, date=target, tzinfo=tz)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"Daylight calculation failed: {exc}",
            "fallback": f"https://www.timeanddate.com/sun/@{latitude},{longitude}",
        }

    sunrise, sunset = s["sunrise"], s["sunset"]
    daylight = sunset - sunrise
    daylight_hours = round(daylight.total_seconds() / 3600, 2)

    if daylight_hours >= 16:
        planning = (
            "Long Scottish summer day. Plenty of light for driving and hikes; "
            "you can start late and still finish walks before dusk."
        )
    elif daylight_hours >= 13:
        planning = "Good daylight window. Aim to reach camp and cook before dusk."
    else:
        planning = "Shorter day for the season. Plan hikes early and drive twisty roads in daylight."

    def fmt(dt: datetime) -> str:
        return dt.strftime("%H:%M")

    return {
        "ok": True,
        "date": target.isoformat(),
        "coordinates": {"latitude": latitude, "longitude": longitude},
        "timezone": SCOTLAND_TZ,
        "sunrise": fmt(sunrise),
        "sunset": fmt(sunset),
        "twilight_dawn": fmt(s["dawn"]),
        "twilight_dusk": fmt(s["dusk"]),
        "daylight_hours": daylight_hours,
        "planning_notes": planning,
    }


# Live layer API used by the Traffic Scotland mobile site. The documented
# www.traffic.gov.scot/api/v2/incidents path 404s; this is the working feed.
# The list returns points (id + coordinates); per-incident detail lives at detailsUrl.
TRAFFIC_SCOTLAND_LIST = "https://myapi.trafficscotland.org/v2.0/layers/current-incidents"
MAX_INCIDENT_DETAILS = 80  # cap detail fetches so a busy day can't stall the request


def _incident_severity(type_name: str, subtype: str, description: str) -> str:
    """Traffic Scotland has no severity field; derive a rough one from type/text."""
    blob = f"{type_name} {subtype} {description}".lower()
    if any(k in blob for k in ("closed", "closure", "accident", "crash", "overturned")):
        return "high"
    if "roadwork" in blob or "planned" in blob:
        return "low"
    return "medium"


async def tool_get_road_incidents_scotland(region: Optional[str] = None) -> dict[str, Any]:
    fallback = "https://www.traffic.gov.scot/tsr/"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(TRAFFIC_SCOTLAND_LIST, headers=headers)
            resp.raise_for_status()
            listing = resp.json()

            points = (listing.get("layer") or {}).get("points") or listing.get("points") or []
            truncated = len(points) > MAX_INCIDENT_DETAILS
            points = points[:MAX_INCIDENT_DETAILS]

            async def fetch_detail(pt: dict[str, Any]) -> Optional[dict[str, Any]]:
                url = pt.get("detailsUrl")
                if not url:
                    return None
                try:
                    r = await client.get(url, headers=headers)
                    r.raise_for_status()
                    return r.json()
                except Exception:  # noqa: BLE001 — skip individual bad records
                    return None

            import asyncio

            details = await asyncio.gather(*(fetch_detail(p) for p in points))
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"Traffic Scotland API failed: {exc}",
            "fallback": fallback,
        }

    incidents: list[dict[str, Any]] = []
    for d in details:
        if not d:
            continue
        type_name = d.get("incidentTypeName") or "Incident"
        subtype = d.get("incidentSubTypeName") or ""
        description = d.get("description") or ""
        incidents.append(
            {
                "location": d.get("locationName") or d.get("title") or "Unknown location",
                "route": d.get("routeName"),
                "region": d.get("regionName"),
                "type": type_name,
                "subtype": subtype or None,
                "direction": d.get("directionName") or None,
                "severity": _incident_severity(type_name, subtype, description),
                "last_updated": d.get("lastModified") or d.get("startTime"),
                "description": description.strip() or None,
                "coordinates": {"latitude": d.get("latitude"), "longitude": d.get("longitude")},
            }
        )

    if region:
        needle = region.strip().lower()
        filtered = [
            i
            for i in incidents
            if needle in str(i.get("route", "")).lower()
            or needle in str(i.get("location", "")).lower()
            or needle in str(i.get("region", "")).lower()
            or needle in str(i.get("description", "")).lower()
        ]
    else:
        filtered = incidents

    filtered.sort(key=lambda i: str(i.get("last_updated") or ""), reverse=True)

    result: dict[str, Any] = {
        "ok": True,
        "region_filter": region,
        "count": len(filtered),
        "incidents": filtered,
        "source": TRAFFIC_SCOTLAND_LIST,
        "fallback": fallback,
    }
    if truncated:
        result["note"] = (
            f"Showing first {MAX_INCIDENT_DETAILS} of {len(points)}+ incidents to keep the "
            "response fast; narrow with a region filter for the rest."
        )
    return result


# Overpass tag selectors per essential type.
_ESSENTIAL_QUERIES: dict[str, list[str]] = {
    "fuel": ['node["amenity"="fuel"]'],
    "supermarket": [
        'node["shop"="supermarket"]',
        'node["shop"="convenience"]',
    ],
    "water_waste": [
        'node["amenity"="sanitary_dump_station"]',
        'node["amenity"="drinking_water"]',
        'node["waterway"="water_point"]',
    ],
}


async def tool_find_campervan_essentials(
    latitude: float,
    longitude: float,
    radius_km: float = 20,
    essential_type: Literal["fuel", "supermarket", "water_waste", "all"] = "all",
) -> dict[str, Any]:
    fallback = (
        f"https://www.google.com/maps/search/{essential_type.replace('_', '+')}"
        f"/@{latitude},{longitude},11z"
    )

    if essential_type == "all":
        selectors = [s for group in _ESSENTIAL_QUERIES.values() for s in group]
        type_lookup = {sel: t for t, group in _ESSENTIAL_QUERIES.items() for sel in group}
    elif essential_type in _ESSENTIAL_QUERIES:
        selectors = _ESSENTIAL_QUERIES[essential_type]
        type_lookup = {sel: essential_type for sel in selectors}
    else:
        return {
            "ok": False,
            "error": f"Unknown essential_type '{essential_type}'. Use fuel, supermarket, water_waste, or all.",
            "fallback": fallback,
        }

    radius_m = int(radius_km * 1000)
    # Shares the mirror-fallback helper with the other OSM tools, so a busy
    # Overpass instance rolls over to the next rather than failing the request.
    try:
        elements = await _overpass_elements(selectors, latitude, longitude, radius_m)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": f"Overpass API failed: {exc}",
            "fallback": fallback,
        }

    def classify(tags: dict[str, Any]) -> str:
        if tags.get("amenity") == "fuel":
            return "fuel"
        if tags.get("shop") in ("supermarket", "convenience"):
            return "supermarket"
        if (
            tags.get("amenity") in ("sanitary_dump_station", "drinking_water")
            or tags.get("waterway") == "water_point"
        ):
            return "water_waste"
        return "other"

    results: list[dict[str, Any]] = []
    for el in elements:
        tags = el.get("tags", {})
        lat, lon = _el_coords(el)
        if lat is None or lon is None:
            continue
        results.append(
            {
                "name": tags.get("name") or tags.get("operator") or "Unnamed",
                "type": classify(tags),
                "distance_km": _haversine_km(latitude, longitude, lat, lon),
                "coordinates": {"latitude": lat, "longitude": lon},
                "opening_hours": tags.get("opening_hours"),
                "open_now": _opening_status(tags.get("opening_hours")),
            }
        )

    results.sort(key=lambda r: r["distance_km"])

    open_now = [r for r in results if r["open_now"]["state"] == "open"]
    return {
        "ok": True,
        "query": {
            "latitude": latitude,
            "longitude": longitude,
            "radius_km": radius_km,
            "essential_type": essential_type,
        },
        "count": len(results),
        "open_now_count": len(open_now),
        "nearest_open_now": (
            {
                "name": open_now[0]["name"],
                "type": open_now[0]["type"],
                "distance_km": open_now[0]["distance_km"],
                "closes": open_now[0]["open_now"].get("closes_at"),
            }
            if open_now
            else None
        ),
        "essentials": results,
        "hours_caveat": "Opening hours come from OpenStreetMap and can be stale or missing, "
        "especially for rural Highland shops. Treat 'open now' as a strong hint, not a promise.",
        "fallback": fallback,
    }


# --------------------------------------------------------------------------- #
# Eclipse viewing — 12 Aug 2026 partial solar eclipse over Scotland
# --------------------------------------------------------------------------- #

# Evening of 12 Aug 2026: from Scotland the Moon covers ~85–92% of the Sun,
# low in the WNW (~275° az), max ~19:05 BST, ending ~20:00 BST — before sunset,
# but with the Sun only 10–17° up. So a clear low WNW horizon (ideally sea) plus
# clear skies decide where it's best seen. Local circumstances are computed with
# ephem (validated to ~1 min / <0.5% obscuration against timeanddate for Edinburgh).
ECLIPSE_DATE = date(2026, 8, 12)

# Curated viewing spots. horizon = quality of the low WNW skyline (the limiting
# factor here); score is a 0–1 weight for that horizon used in ranking.
ECLIPSE_SPOTS: dict[str, dict[str, Any]] = {
    "Neist Point, Isle of Skye": {"lat": 57.423, "lon": -6.788, "horizon": "excellent — open Atlantic to the WNW", "score": 1.00, "on_route": True},
    "Ardnamurchan Point": {"lat": 56.727, "lon": -6.227, "horizon": "excellent — westernmost mainland, open sea", "score": 1.00, "on_route": False},
    "Stornoway, Isle of Lewis": {"lat": 58.209, "lon": -6.389, "horizon": "excellent — sea horizon, highest obscuration", "score": 0.95, "on_route": False},
    "Ullapool": {"lat": 57.895, "lon": -5.160, "horizon": "very good — Loch Broom opens to the W", "score": 0.85, "on_route": False},
    "Mallaig": {"lat": 57.006, "lon": -5.828, "horizon": "very good — sea to the W, isles low on horizon", "score": 0.85, "on_route": False},
    "Oban": {"lat": 56.415, "lon": -5.472, "horizon": "good — Mull/Kerrera partly block the low W", "score": 0.70, "on_route": True},
    "Glen Coe": {"lat": 56.6, "lon": -5.1, "horizon": "poor — mountains to the W block the low Sun", "score": 0.25, "on_route": True},
    "Edinburgh (Calton/Blackford Hill)": {"lat": 55.9533, "lon": -3.1883, "horizon": "moderate — urban; use an elevated W viewpoint", "score": 0.50, "on_route": True},
}


def _overlap_fraction(sun_r: float, moon_r: float, sep: float) -> float:
    """Fraction of the Sun's disk (radius sun_r) covered by the Moon (radius moon_r), centres `sep` apart."""
    if sep >= sun_r + moon_r:
        return 0.0
    if sep <= abs(sun_r - moon_r):
        return (min(sun_r, moon_r) ** 2) / (sun_r * sun_r)
    r2, R2, d2 = moon_r * moon_r, sun_r * sun_r, sep * sep
    area = (
        R2 * math.acos((d2 + R2 - r2) / (2 * sep * sun_r))
        + r2 * math.acos((d2 + r2 - R2) / (2 * sep * moon_r))
        - 0.5 * math.sqrt(max(0.0, (-sep + moon_r + sun_r) * (sep + moon_r - sun_r) * (sep - moon_r + sun_r) * (sep + moon_r + sun_r)))
    )
    return area / (math.pi * sun_r * sun_r)


def _eclipse_circumstances(lat: float, lon: float) -> dict[str, Any]:
    """Compute local circumstances for the 12 Aug 2026 eclipse at lat/lon. Sync (ephem is CPU-only, fast)."""
    obs = ephem.Observer()
    obs.lat, obs.lon = str(lat), str(lon)
    obs.elevation = 0
    sun_body, moon_body = ephem.Sun(), ephem.Moon()

    tz = pytz.timezone(SCOTLAND_TZ)
    best = {"obsc": 0.0, "mag": 0.0, "time_utc": None, "sun_alt": None, "sun_az": None}
    first_utc = last_utc = None

    # Scan 17:00–20:40 UTC (18:00–21:40 BST) at 1-minute steps.
    t = datetime(2026, 8, 12, 17, 0, 0)
    for _ in range(220):
        obs.date = ephem.Date(t)
        sun_body.compute(obs)
        moon_body.compute(obs)
        sun_r = sun_body.size / 2.0   # arcsec (angular radius)
        moon_r = moon_body.size / 2.0
        sep = float(ephem.separation((sun_body.az, sun_body.alt), (moon_body.az, moon_body.alt))) * 206264.806
        frac = _overlap_fraction(sun_r, moon_r, sep)
        alt = math.degrees(float(sun_body.alt))
        if frac > 0.001 and alt > 0:  # only count while the Sun is above the horizon
            if first_utc is None:
                first_utc = t
            last_utc = t
            if frac > best["obsc"]:
                mag = (sun_r + moon_r - sep) / (2 * sun_r) if sep < sun_r + moon_r else 0.0
                best = {"obsc": frac, "mag": mag, "time_utc": t, "sun_alt": alt, "sun_az": math.degrees(float(sun_body.az))}
        t += timedelta(minutes=1)

    def to_bst(dt: Optional[datetime]) -> Optional[str]:
        if dt is None:
            return None
        return pytz.utc.localize(dt).astimezone(tz).strftime("%H:%M")

    # Sunset that evening, for context (is the Sun still up at max?).
    try:
        s = sun(LocationInfo(latitude=lat, longitude=lon, timezone=SCOTLAND_TZ).observer, date=ECLIPSE_DATE, tzinfo=tz)
        sunset_bst = s["sunset"].strftime("%H:%M")
    except Exception:  # noqa: BLE001
        sunset_bst = None

    return {
        "max_obscuration_pct": round(best["obsc"] * 100, 1),
        "magnitude": round(best["mag"], 3),
        "max_time_bst": to_bst(best["time_utc"]),
        "sun_altitude_deg": round(best["sun_alt"], 1) if best["sun_alt"] is not None else None,
        "sun_azimuth_deg": round(best["sun_az"], 0) if best["sun_az"] is not None else None,
        "partial_begins_bst": to_bst(first_utc),
        "partial_ends_bst": to_bst(last_utc),
        "sunset_bst": sunset_bst,
    }


async def _eclipse_cloud_cover(lat: float, lon: float) -> dict[str, Any]:
    """Average cloud cover over the eclipse window (18:00–20:00 BST) on 12 Aug 2026, if within forecast range."""
    unavailable = {
        "available": False,
        "note": "Cloud forecast reaches only ~16 days ahead, so it's not published for 12 Aug yet. "
        "Check again from about 27 Jul onward (and it will be accurate during the trip).",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "cloud_cover",
                    "start_date": ECLIPSE_DATE.isoformat(),
                    "end_date": ECLIPSE_DATE.isoformat(),
                    "timezone": SCOTLAND_TZ,
                },
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code >= 400:
                return unavailable
            data = resp.json()
    except Exception:  # noqa: BLE001
        return unavailable

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    clouds = hourly.get("cloud_cover", [])
    window = [c for tm, c in zip(times, clouds) if c is not None and tm[11:13] in ("18", "19", "20")]
    if not window:
        return unavailable
    avg = round(sum(window) / len(window))
    if avg <= 25:
        outlook = "mostly clear — promising"
    elif avg <= 55:
        outlook = "part cloud — worth a look, bring patience"
    else:
        outlook = "cloudy — have a backup spot or plan"
    return {"available": True, "avg_cloud_pct": avg, "outlook": outlook}


def _eclipse_verdict(circ: dict[str, Any], horizon_score: float, cloud: dict[str, Any]) -> dict[str, Any]:
    """Blend obscuration, Sun altitude, horizon quality and (if known) cloud into a 0–100 viewing score."""
    obsc = (circ.get("max_obscuration_pct") or 0) / 100
    alt = circ.get("sun_altitude_deg") or 0
    base = 0.55 * horizon_score + 0.25 * min(alt / 18.0, 1.0) + 0.20 * obsc
    if cloud.get("available"):
        base *= 1 - 0.7 * (cloud["avg_cloud_pct"] / 100)
    score = round(base * 100)
    if score >= 75:
        rating = "excellent"
    elif score >= 60:
        rating = "good"
    elif score >= 40:
        rating = "fair"
    else:
        rating = "poor"
    return {"score": score, "rating": rating}


async def tool_get_eclipse_viewing(
    region: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> dict[str, Any]:
    fallback = "https://www.timeanddate.com/eclipse/in/uk/scotland?iso=20260812"
    header = {
        "event": "Partial solar eclipse over Scotland",
        "date": ECLIPSE_DATE.isoformat(),
        "summary": "The Moon covers ~85–92% of the Sun in the early evening, low in the WNW. "
        "A clear low western horizon (sea is ideal) and clear skies are what matter most.",
        "fallback": fallback,
    }

    # Case 1: explicit coordinates.
    if latitude is not None and longitude is not None:
        circ = _eclipse_circumstances(latitude, longitude)
        cloud = await _eclipse_cloud_cover(latitude, longitude)
        return {
            "ok": True,
            **header,
            "location": {"name": "custom location", "latitude": latitude, "longitude": longitude},
            "circumstances": circ,
            "cloud": cloud,
            "viewing": _eclipse_verdict(circ, 0.7, cloud),
            "horizon_tip": "Face west-northwest (~275°). You need an open view down to ~10° above the horizon.",
        }

    # Case 2: a named spot/region.
    if region:
        key = region.strip().lower()
        spot = next((n for n in ECLIPSE_SPOTS if key in n.lower() or n.lower().split(",")[0] in key), None)
        if spot:
            info = ECLIPSE_SPOTS[spot]
            circ = _eclipse_circumstances(info["lat"], info["lon"])
            cloud = await _eclipse_cloud_cover(info["lat"], info["lon"])
            return {
                "ok": True,
                **header,
                "location": {"name": spot, "latitude": info["lat"], "longitude": info["lon"]},
                "horizon": info["horizon"],
                "on_trip_route": info["on_route"],
                "circumstances": circ,
                "cloud": cloud,
                "viewing": _eclipse_verdict(circ, info["score"], cloud),
                "horizon_tip": "Face west-northwest (~275°) with a clear low skyline; the Sun sits only ~10–17° up.",
            }
        resolved = _resolve_region(region)
        if resolved:
            name, lat, lon = resolved
            circ = _eclipse_circumstances(lat, lon)
            cloud = await _eclipse_cloud_cover(lat, lon)
            return {
                "ok": True,
                **header,
                "location": {"name": name, "latitude": lat, "longitude": lon},
                "circumstances": circ,
                "cloud": cloud,
                "viewing": _eclipse_verdict(circ, 0.6, cloud),
                "horizon_tip": "Face west-northwest (~275°) with a clear low skyline.",
            }
        return {
            "ok": False,
            **header,
            "error": f"Unknown location '{region}'.",
            "suggested_spots": list(ECLIPSE_SPOTS),
        }

    # Case 3: no location -> rank the curated spots best-first.
    import asyncio

    async def rank_one(name: str) -> dict[str, Any]:
        info = ECLIPSE_SPOTS[name]
        circ = _eclipse_circumstances(info["lat"], info["lon"])
        cloud = await _eclipse_cloud_cover(info["lat"], info["lon"])
        verdict = _eclipse_verdict(circ, info["score"], cloud)
        return {
            "location": name,
            "on_trip_route": info["on_route"],
            "horizon": info["horizon"],
            "max_obscuration_pct": circ["max_obscuration_pct"],
            "max_time_bst": circ["max_time_bst"],
            "sun_altitude_deg": circ["sun_altitude_deg"],
            "cloud": cloud,
            "viewing": verdict,
        }

    ranked = await asyncio.gather(*(rank_one(n) for n in ECLIPSE_SPOTS))
    ranked.sort(key=lambda r: r["viewing"]["score"], reverse=True)
    return {
        "ok": True,
        **header,
        "best_spots": ranked,
        "note": "Ranked by horizon quality, Sun altitude and obscuration"
        + (" plus live cloud forecast" if ranked and ranked[0]["cloud"].get("available") else "; cloud forecast will refine this closer to the date")
        + ". Spots flagged on_trip_route match your Edinburgh → Highlands → Skye plan.",
    }


# --------------------------------------------------------------------------- #
# Midge forecast — computed from Open-Meteo (Highland camping essential in August)
# --------------------------------------------------------------------------- #

# Highland midges (Culicoides impunctatus) peak late June–August. They need still,
# mild, damp air: they can't fly much above ~11 km/h wind, go quiet below ~9–10°C,
# love high humidity, and bite hardest at dawn and dusk. All of that comes from the
# hourly Open-Meteo feed we already use, so risk is computed rather than scraped.

def _midge_index(temp: Optional[float], rh: Optional[float], wind: Optional[float], precip: Optional[float]) -> float:
    """0–1 midge activity index for one hour."""
    if temp is None or wind is None:
        return 0.0
    if wind <= 6:
        wf = 1.0
    elif wind <= 11:
        wf = 0.75
    elif wind <= 16:
        wf = 0.35
    elif wind <= 22:
        wf = 0.1
    else:
        wf = 0.0  # too breezy to fly
    if temp >= 14:
        tf = 1.0
    elif temp >= 11:
        tf = 0.7
    elif temp >= 9:
        tf = 0.35
    else:
        tf = 0.0  # too cold
    rh = 75.0 if rh is None else rh
    if rh >= 88:
        hf = 1.0
    elif rh >= 75:
        hf = 0.8
    elif rh >= 60:
        hf = 0.5
    else:
        hf = 0.25
    idx = wf * tf * hf
    if precip and precip >= 2:
        idx *= 0.5  # heavy rain grounds them too
    return idx


def _midge_level(idx: float) -> dict[str, Any]:
    """Map the 0–1 index onto the familiar 1–5 midge scale."""
    if idx < 0.1:
        lvl, label = 1, "Minimal"
    elif idx < 0.3:
        lvl, label = 2, "Low"
    elif idx < 0.5:
        lvl, label = 3, "Moderate"
    elif idx < 0.75:
        lvl, label = 4, "High"
    else:
        lvl, label = 5, "Very High"
    return {"level": lvl, "label": label}


async def tool_get_midge_forecast(
    region: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    date_str: Optional[str] = None,
) -> dict[str, Any]:
    fallback = "https://www.smidgeup.com/midge-forecast/"

    if latitude is not None and longitude is not None:
        name, lat, lon = "your location", latitude, longitude
    elif region:
        resolved = _resolve_region(region)
        if not resolved:
            return {
                "ok": False,
                "error": f"Unknown region '{region}'.",
                "known_regions": sorted(n.title() for n in SCOTLAND_REGIONS),
                "fallback": fallback,
            }
        name, lat, lon = resolved
    else:
        return {"ok": False, "error": "Provide a region or latitude/longitude.", "fallback": fallback}

    tz = pytz.timezone(SCOTLAND_TZ)
    if date_str:
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return {"ok": False, "error": f"Invalid date '{date_str}'. Use YYYY-MM-DD.", "fallback": fallback}
    else:
        target = datetime.now(tz).date()

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation",
                    "start_date": target.isoformat(),
                    "end_date": target.isoformat(),
                    "timezone": SCOTLAND_TZ,
                },
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code >= 400:
                return {
                    "ok": False,
                    "region": name,
                    "error": "No forecast for that date (only ~16 days ahead is available).",
                    "fallback": fallback,
                }
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "region": name, "error": f"Weather API failed: {exc}", "fallback": fallback}

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    rhs = hourly.get("relative_humidity_2m", [])
    winds = hourly.get("wind_speed_10m", [])
    precs = hourly.get("precipitation", [])

    evening: list[dict[str, Any]] = []  # prime camping window 18:00–22:00
    peak = {"idx": -1.0}
    for i, tm in enumerate(times):
        hour = tm[11:13]

        def g(arr: list[Any]) -> Any:
            return arr[i] if i < len(arr) else None

        idx = _midge_index(g(temps), g(rhs), g(winds), g(precs))
        if hour in ("18", "19", "20", "21", "22"):
            row = {"time": tm[11:16], "wind_kmh": g(winds), "temp_c": g(temps), **_midge_level(idx)}
            evening.append(row)
            if idx > peak["idx"]:
                peak = {"idx": idx, "time": tm[11:16], **_midge_level(idx)}

    verdict = _midge_level(peak["idx"] if peak["idx"] >= 0 else 0.0)
    lvl = verdict["level"]
    if lvl >= 4:
        advice = (
            "Keep the van closed around dusk. Use a repellent (Smidge/DEET), and favour higher, "
            "breezier ground away from still water, bogs and bracken. Even a light wind clears them."
        )
    elif lvl == 3:
        advice = "Moderate. Have repellent ready for dusk; a breeze or open ground keeps them down."
    else:
        advice = "Low. Wind or cool air is keeping midges grounded. Good evening to sit out."

    return {
        "ok": True,
        "region": name,
        "coordinates": {"latitude": lat, "longitude": lon},
        "date": target.isoformat(),
        "evening_verdict": {**verdict, "worst_time": peak.get("time")},
        "scale": "1 Minimal · 2 Low · 3 Moderate · 4 High · 5 Very High",
        "hourly_evening": evening,
        "advice": advice,
        "note": "Dawn (roughly 04:00–07:00) is usually just as bad as dusk. Bright sun and wind are your friends.",
        "fallback": fallback,
    }


# --------------------------------------------------------------------------- #
# OpenStreetMap helpers — overnight/camping spots and attractions
# --------------------------------------------------------------------------- #

# Public Overpass instances 504/rate-limit under load, so try mirrors in turn.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
# Per-mirror budget kept short so a busy server fails fast to the next / to the
# tool's graceful fallback, rather than making the user wait a full minute.
OVERPASS_TIMEOUT = httpx.Timeout(15.0, connect=6.0)


async def _overpass_elements(selectors: list[str], lat: float, lon: float, radius_m: int) -> list[dict[str, Any]]:
    """Run one Overpass query around a point, falling back across mirrors. Raises if all fail."""
    around = f"(around:{radius_m},{lat},{lon})"
    body = "".join(f"{sel}{around};" for sel in selectors)
    query = f"[out:json][timeout:20];({body});out center;"
    last_exc: Optional[Exception] = None
    async with httpx.AsyncClient(timeout=OVERPASS_TIMEOUT) as client:
        for url in OVERPASS_MIRRORS:
            try:
                resp = await client.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT})
                resp.raise_for_status()
                return resp.json().get("elements", [])
            except Exception as exc:  # noqa: BLE001 — try the next mirror
                last_exc = exc
    # Surface a useful message even when the underlying error stringifies to "".
    detail = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "all mirrors failed"
    raise RuntimeError(detail)


def _el_coords(el: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    return lat, lon


SCOTLAND_CAMPING_NOTE = (
    "Scotland's Outdoor Access Code allows responsible wild camping (mainly for tents/small groups). "
    "Campervans should use car parks, laybys, aires or campsites, not verges. Note the Loch Lomond & "
    "Trossachs seasonal camping byelaws (permit zones)."
)


async def tool_find_overnight_spots(
    latitude: float,
    longitude: float,
    radius_km: float = 20,
    spot_type: Literal["free", "campsite", "motorhome", "all"] = "all",
) -> dict[str, Any]:
    fallback = f"https://park4night.com/en/search?lat={latitude}&lng={longitude}"
    radius_m = int(radius_km * 1000)

    camp = ['node["tourism"="camp_site"]', 'way["tourism"="camp_site"]']
    caravan = ['node["tourism"="caravan_site"]', 'way["tourism"="caravan_site"]']
    # Node-only for motorhome parking keeps this (the heaviest) query light on Overpass.
    mh_park = ['node["amenity"="parking"]["motorhome"~"yes|designated|permissive"]']
    if spot_type == "free":
        selectors = [
            'node["tourism"="camp_site"]["fee"="no"]',
            'way["tourism"="camp_site"]["fee"="no"]',
            'node["tourism"="caravan_site"]["fee"="no"]',
            'way["tourism"="caravan_site"]["fee"="no"]',
        ] + mh_park
    elif spot_type == "campsite":
        selectors = camp
    elif spot_type == "motorhome":
        selectors = caravan + mh_park
    else:
        selectors = camp + caravan + mh_park

    try:
        elements = await _overpass_elements(selectors, latitude, longitude, radius_m)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Overpass API failed: {exc}", "fallback": fallback}

    def classify(tags: dict[str, Any]) -> str:
        if tags.get("tourism") == "camp_site":
            return "campsite"
        if tags.get("tourism") == "caravan_site":
            return "motorhome/caravan site"
        if tags.get("amenity") == "parking":
            return "motorhome-friendly parking"
        return "other"

    results: list[dict[str, Any]] = []
    for el in elements:
        tags = el.get("tags", {})
        lat, lon = _el_coords(el)
        if lat is None or lon is None:
            continue
        fee = tags.get("fee")
        results.append(
            {
                "name": tags.get("name") or tags.get("operator") or "Unnamed spot",
                "type": classify(tags),
                "cost": "free" if fee == "no" else ("paid" if fee == "yes" else "unknown"),
                "tents": tags.get("tents") == "yes" or None,
                "motorhome": tags.get("motorhome") in ("yes", "designated", "permissive") or tags.get("caravans") == "yes" or None,
                "backcountry": tags.get("backcountry") == "yes" or None,
                "distance_km": _haversine_km(latitude, longitude, lat, lon),
                "coordinates": {"latitude": lat, "longitude": lon},
                "opening_hours": tags.get("opening_hours"),
                "open_now": _opening_status(tags.get("opening_hours")),
            }
        )
    results.sort(key=lambda r: r["distance_km"])

    return {
        "ok": True,
        "query": {"latitude": latitude, "longitude": longitude, "radius_km": radius_km, "spot_type": spot_type},
        "count": len(results),
        "spots": results[:50],
        "access_note": SCOTLAND_CAMPING_NOTE,
        "source": "OpenStreetMap (Overpass)",
        "fallback": fallback,
    }


async def tool_find_attractions(
    latitude: float,
    longitude: float,
    radius_km: float = 25,
    category: Literal["distillery", "castle", "viewpoint", "attraction", "all"] = "all",
) -> dict[str, Any]:
    fallback = f"https://www.google.com/maps/search/things+to+do/@{latitude},{longitude},11z"
    radius_m = int(radius_km * 1000)

    groups: dict[str, list[str]] = {
        "distillery": ['node["craft"="distillery"]', 'way["craft"="distillery"]', 'node["tourism"="attraction"]["name"~"[Dd]istillery"]'],
        "castle": ['node["historic"="castle"]', 'way["historic"="castle"]'],
        "viewpoint": ['node["tourism"="viewpoint"]'],
        "attraction": ['node["tourism"="attraction"]', 'way["tourism"="attraction"]', 'node["tourism"="museum"]', 'way["tourism"="museum"]'],
    }
    if category == "all":
        selectors = [s for g in groups.values() for s in g]
    elif category in groups:
        selectors = groups[category]
    else:
        return {"ok": False, "error": f"Unknown category '{category}'.", "fallback": fallback}

    try:
        elements = await _overpass_elements(selectors, latitude, longitude, radius_m)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Overpass API failed: {exc}", "fallback": fallback}

    def classify(tags: dict[str, Any]) -> str:
        if tags.get("craft") == "distillery" or "distillery" in str(tags.get("name", "")).lower():
            return "distillery"
        if tags.get("historic") == "castle":
            return "castle"
        if tags.get("tourism") == "viewpoint":
            return "viewpoint"
        if tags.get("tourism") == "museum":
            return "museum"
        return "attraction"

    seen: set[tuple[float, float]] = set()
    results: list[dict[str, Any]] = []
    for el in elements:
        tags = el.get("tags", {})
        lat, lon = _el_coords(el)
        if lat is None or lon is None:
            continue
        cat = classify(tags)
        name = tags.get("name")
        if not name and cat != "viewpoint":
            continue  # skip anonymous POIs except viewpoints
        key = (round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "name": name or "Unnamed viewpoint",
                "category": cat,
                "distance_km": _haversine_km(latitude, longitude, lat, lon),
                "coordinates": {"latitude": lat, "longitude": lon},
                "website": tags.get("website") or tags.get("contact:website"),
                "opening_hours": tags.get("opening_hours"),
                "open_now": _opening_status(tags.get("opening_hours")),
            }
        )
    results.sort(key=lambda r: r["distance_km"])

    return {
        "ok": True,
        "query": {"latitude": latitude, "longitude": longitude, "radius_km": radius_km, "category": category},
        "count": len(results),
        "attractions": results[:40],
        "source": "OpenStreetMap (Overpass)",
        "fallback": fallback,
    }


async def tool_find_pubs(
    latitude: float,
    longitude: float,
    radius_km: float = 10,
    filter_by: Literal["all", "real_ale", "food", "step_free", "outdoor"] = "all",
) -> dict[str, Any]:
    fallback = f"https://www.google.com/maps/search/pub/@{latitude},{longitude},13z"
    radius_m = int(radius_km * 1000)
    selectors = [
        'node["amenity"="pub"]',
        'way["amenity"="pub"]',
        'node["amenity"="bar"]',
        'way["amenity"="bar"]',
    ]

    try:
        elements = await _overpass_elements(selectors, latitude, longitude, radius_m)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Overpass API failed: {exc}", "fallback": fallback}

    pubs: list[dict[str, Any]] = []
    for el in elements:
        tags = el.get("tags", {})
        lat, lon = _el_coords(el)
        if lat is None or lon is None or not tags.get("name"):
            continue
        real_ale = tags.get("real_ale") in ("yes", "only")
        serves_food = tags.get("food") == "yes" or bool(tags.get("cuisine")) or tags.get("kitchen") == "yes"
        step_free = tags.get("wheelchair")  # OSM tag: yes | limited | no | None
        outdoor = tags.get("outdoor_seating") == "yes" or tags.get("outdoor_seating") == "designated"
        pubs.append(
            {
                "name": tags.get("name"),
                "kind": tags.get("amenity"),
                "distance_km": _haversine_km(latitude, longitude, lat, lon),
                "real_ale": real_ale or None,
                "serves_food": serves_food or None,
                "step_free_entry": step_free,
                "outdoor_seating": outdoor or None,
                "opening_hours": tags.get("opening_hours"),
                "open_now": _opening_status(tags.get("opening_hours")),
                "website": tags.get("website") or tags.get("contact:website"),
                "coordinates": {"latitude": lat, "longitude": lon},
            }
        )

    if filter_by == "real_ale":
        pubs = [p for p in pubs if p["real_ale"]]
    elif filter_by == "food":
        pubs = [p for p in pubs if p["serves_food"]]
    elif filter_by == "step_free":
        # OSM's wheelchair=yes flags a step-free entrance — useful for anyone who
        # walks but can't manage steps, not only wheelchair users.
        pubs = [p for p in pubs if p["step_free_entry"] in ("yes", "limited")]
    elif filter_by == "outdoor":
        pubs = [p for p in pubs if p["outdoor_seating"]]

    pubs.sort(key=lambda p: p["distance_km"])

    return {
        "ok": True,
        "query": {"latitude": latitude, "longitude": longitude, "radius_km": radius_km, "filter_by": filter_by},
        "count": len(pubs),
        "pubs": pubs[:50],
        "note": "Attributes (food, real ale, step-free entry) come from OpenStreetMap and are only as "
        "complete as contributors have made them — a blank field means unknown, not no. "
        "step_free_entry describes a step-free door, helpful for anyone who walks but can't manage steps.",
        "source": "OpenStreetMap (Overpass)",
        "fallback": fallback,
    }


# --------------------------------------------------------------------------- #
# Live trains — National Rail (Darwin) via the free, key-less Huxley2 proxy
# --------------------------------------------------------------------------- #

# Huxley2 is a public REST proxy in front of National Rail's Darwin feed. It needs
# no API key, which is why it's used here rather than Darwin/RTT (both require
# registration). It's community-hosted, so it can rate-limit or go down — every
# failure falls back to the National Rail live board for that station.
HUXLEY2 = "https://huxley2.azurewebsites.net"

# CRS codes, all verified live against the API (incl. the West Highland Line).
SCOTTISH_STATIONS: dict[str, str] = {
    "edinburgh waverley": "EDB",
    "edinburgh": "EDB",
    "haymarket": "HYM",
    "musselburgh": "MUB",
    "glasgow queen street": "GLQ",
    "glasgow central": "GLC",
    "glasgow": "GLQ",
    "inverness": "INV",
    "fort william": "FTW",
    "mallaig": "MLG",
    "arisaig": "ARG",
    "glenfinnan": "GLF",
    "spean bridge": "SBR",
    "corrour": "CRR",
    "rannoch": "RAN",
    "bridge of orchy": "BRO",
    "crianlarich": "CNR",
    "kyle of lochalsh": "KYL",
    "oban": "OBN",
    "aviemore": "AVM",
    "pitlochry": "PIT",
    "stirling": "STG",
    "perth": "PTH",
    "dundee": "DEE",
    "aberdeen": "ABD",
}


def _resolve_station(station: str) -> Optional[tuple[str, str]]:
    """Return (display_name, CRS). Accepts a station name or a raw 3-letter CRS code."""
    if not station:
        return None
    key = station.strip().lower()
    if key in SCOTTISH_STATIONS:
        return key.title(), SCOTTISH_STATIONS[key]
    for name, crs in SCOTTISH_STATIONS.items():
        if key in name or name in key:
            return name.title(), crs
    if len(key) == 3 and key.isalpha():  # raw CRS, e.g. "EDB"
        return key.upper(), key.upper()
    return None


# Fares are the one part of UK rail that is NOT openly available: live pricing runs
# through the licensed Online Journey Planner, and the free bulk fares dataset would
# mean reimplementing the routeing guide (easy to get wrong). Rather than quote a
# price that might be false, we hand back a pre-filled link to the official planner.
NATIONAL_RAIL_PLANNER = "https://www.nationalrail.co.uk/journey-planner/"
TRAINLINE_SEARCH = "https://www.thetrainline.com/book/results"


def _booking_url(origin_crs: str, dest_crs: str, when: datetime, adults: int = 1) -> str:
    """Pre-filled National Rail journey planner link (the authoritative price)."""
    minute = min((0, 15, 30, 45), key=lambda m: abs(m - when.minute))
    params = {
        "type": "single",
        "origin": origin_crs,
        "destination": dest_crs,
        "leavingType": "departing",
        "leavingDate": when.strftime("%d%m%y"),
        "leavingHour": f"{when.hour:02d}",
        "leavingMin": f"{minute:02d}",
        "adults": str(max(1, adults)),
        "extraTime": "0",
    }
    return f"{NATIONAL_RAIL_PLANNER}?{urlencode(params)}"


def _trainline_url(origin_crs: str, dest_crs: str, when: datetime, adults: int = 1) -> str:
    params = {
        "origin": origin_crs,
        "destination": dest_crs,
        "outwardDate": when.strftime("%Y-%m-%dT%H:%M:%S"),
        "journeySearchType": "single",
        "adults": str(max(1, adults)),
    }
    return f"{TRAINLINE_SEARCH}?{urlencode(params)}"


async def tool_get_train_tickets(
    origin: str,
    destination: str,
    date_str: Optional[str] = None,
    time_str: str = "09:00",
    adults: int = 1,
) -> dict[str, Any]:
    """Pre-filled booking links so the user sees the real, authoritative price."""
    o = _resolve_station(origin)
    d = _resolve_station(destination)
    if not o or not d:
        bad = origin if not o else destination
        return {
            "ok": False,
            "error": f"Unknown station '{bad}'.",
            "known_stations": sorted({n.title() for n in SCOTTISH_STATIONS}),
            "fallback": NATIONAL_RAIL_PLANNER,
        }
    o_name, o_crs = o
    d_name, d_crs = d

    tz = pytz.timezone(SCOTLAND_TZ)
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else datetime.now(tz).date()
        hh, mm = (int(x) for x in time_str.split(":")[:2])
        when = datetime(day.year, day.month, day.day, hh, mm)
    except (ValueError, TypeError):
        return {
            "ok": False,
            "error": f"Invalid date/time ('{date_str}' / '{time_str}'). Use YYYY-MM-DD and HH:MM.",
            "fallback": NATIONAL_RAIL_PLANNER,
        }

    return {
        "ok": True,
        "journey": {
            "from": o_name, "from_crs": o_crs,
            "to": d_name, "to_crs": d_crs,
            "date": day.isoformat(), "time": f"{hh:02d}:{mm:02d}", "adults": max(1, adults),
        },
        "booking_url": _booking_url(o_crs, d_crs, when, adults),
        "alternative_url": _trainline_url(o_crs, d_crs, when, adults),
        "why_no_price_here": (
            "Live UK rail prices come from a licensed journey-planner engine and are not "
            "available via any free API, so this returns a pre-filled link to the official "
            "planner rather than risk quoting a wrong fare."
        ),
        "fare_notes": [
            "Most Scottish regional journeys (including the West Highland and Kyle lines) are "
            "walk-up fares: the price is the same whenever you buy, so there's little gain in booking early.",
            "Advance fares (cheaper, tied to one train) mainly apply to longer routes and do vary.",
            "A Railcard is usually the biggest saving — roughly a third off — if either of you qualifies.",
            "If you plan several scenic rides, a Spirit of Scotland / Highland Rover pass can beat singles.",
        ],
    }


async def tool_get_train_departures(
    station: str,
    destination: Optional[str] = None,
    rows: int = 8,
    board: Literal["departures", "arrivals"] = "departures",
) -> dict[str, Any]:
    resolved = _resolve_station(station)
    if not resolved:
        return {
            "ok": False,
            "error": f"Unknown station '{station}'.",
            "known_stations": sorted({n.title() for n in SCOTTISH_STATIONS}),
            "fallback": "https://www.scotrail.co.uk/plan-your-journey/check-your-journey",
        }
    name, crs = resolved
    fallback = f"https://www.nationalrail.co.uk/live-trains/departures/{crs}/"

    rows = max(1, min(int(rows), 10))
    url = f"{HUXLEY2}/{board}/{crs}"
    if destination:
        dest = _resolve_station(destination)
        if dest:
            url += f"/to/{dest[1]}"
    url += f"/{rows}"

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "station": name,
            "error": f"Live rail feed failed: {exc}",
            "fallback": fallback,
        }

    services: list[dict[str, Any]] = []
    for svc in (data.get("trainServices") or []):
        dests = svc.get("destination") or []
        origins = svc.get("origin") or []
        endpoint = (dests[0].get("locationName") if board == "departures" and dests
                    else origins[0].get("locationName") if origins else None)
        scheduled = svc.get("std") if board == "departures" else svc.get("sta")
        expected = svc.get("etd") if board == "departures" else svc.get("eta")
        services.append(
            {
                "scheduled": scheduled,
                "expected": expected,  # "On time", "Delayed", or an actual time
                "destination" if board == "departures" else "origin": endpoint,
                "platform": svc.get("platform"),
                "operator": svc.get("operator"),
                "cancelled": svc.get("isCancelled") or None,
                "cancel_reason": svc.get("cancelReason"),
                "delay_reason": svc.get("delayReason"),
            }
        )

    replacement_buses = [
        {
            "scheduled": b.get("std") or b.get("sta"),
            "destination": (b.get("destination") or [{}])[0].get("locationName"),
            "operator": b.get("operator"),
        }
        for b in (data.get("busServices") or [])
    ]

    result: dict[str, Any] = {
        "ok": True,
        "station": data.get("locationName") or name,
        "crs": crs,
        "board": board,
        "filtered_to": destination or None,
        "generated_at": data.get("generatedAt"),
        "services": services,
        "replacement_buses": replacement_buses or None,
        "disruption_messages": data.get("nrccMessages") or None,
        "source": "National Rail (Darwin) via Huxley2 — no API key required",
        "fallback": fallback,
    }

    # Live times are free; live prices are not. Hand back a pre-filled planner link
    # for this journey rather than guess at a fare.
    if destination:
        dest = _resolve_station(destination)
        if dest:
            now = datetime.now(pytz.timezone(SCOTLAND_TZ))
            result["booking_url"] = _booking_url(crs, dest[1], now.replace(tzinfo=None))
            result["price_note"] = (
                "Live fares aren't available via any free API. This link opens the official "
                "National Rail planner with your journey pre-filled, for the real price."
            )
    return result


# --------------------------------------------------------------------------- #
# MCP server (Streamable HTTP) — real connector for Claude.ai desktop + mobile
# --------------------------------------------------------------------------- #

mcp = FastMCP(
    "Scotland Explorer",
    stateless_http=True,  # no session stickiness -> plays nicely with Railway
    json_response=True,
    streamable_http_path="/mcp",  # the MCP app is mounted at root, so this is the final path
    # Behind Railway's proxy the request Host is the public domain, not localhost.
    # The default DNS-rebinding guard only trusts localhost and would 421 those
    # requests. This is a public, read-only trip helper with no secrets, so we
    # turn the guard off rather than hard-code an allow-list of Railway hostnames.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool()
async def get_scotland_weather(
    region: str,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """7-day campervan weather forecast for a named Scottish region (Open-Meteo).

    region: e.g. "Edinburgh", "Highlands", "Glen Coe", "Isle of Skye".
    start_date / end_date: optional YYYY-MM-DD window (else next 7 days).
    """
    return await tool_get_scotland_weather(region, start_date, end_date)


@mcp.tool()
async def get_daylight_times(
    latitude: float,
    longitude: float,
    date_str: Optional[str] = None,
) -> dict[str, Any]:
    """Sunrise, sunset, civil twilight and daylight hours for a lat/lon (Astral).

    date_str: optional YYYY-MM-DD (defaults to today, Europe/London).
    """
    return await tool_get_daylight_times(latitude, longitude, date_str)


@mcp.tool()
async def get_road_incidents_scotland(region: Optional[str] = None) -> dict[str, Any]:
    """Live road incidents from Traffic Scotland, optionally filtered (e.g. "A82")."""
    return await tool_get_road_incidents_scotland(region)


@mcp.tool()
async def find_campervan_essentials(
    latitude: float,
    longitude: float,
    radius_km: float = 20,
    essential_type: Literal["fuel", "supermarket", "water_waste", "all"] = "all",
) -> dict[str, Any]:
    """Nearby fuel, supermarkets and water/waste points via OpenStreetMap Overpass."""
    return await tool_find_campervan_essentials(latitude, longitude, radius_km, essential_type)


@mcp.tool()
async def get_eclipse_viewing(
    region: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> dict[str, Any]:
    """Viewing guide for the 12 Aug 2026 partial solar eclipse over Scotland.

    Computes local circumstances (max % of Sun covered, timing in BST, Sun altitude)
    and folds in the cloud forecast when it's within range (~16 days out).
    - Pass a `region`/spot name (e.g. "Isle of Skye", "Edinburgh", "Ullapool") for one location.
    - Or pass `latitude`/`longitude` for a custom point.
    - Or pass nothing to rank the best Scottish viewing spots.
    """
    return await tool_get_eclipse_viewing(region, latitude, longitude)


@mcp.tool()
async def get_midge_forecast(
    region: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    date_str: Optional[str] = None,
) -> dict[str, Any]:
    """Midge risk (1–5) for a Scottish spot on a given evening, computed from wind, temp and humidity.

    Pass a `region` name or `latitude`/`longitude`, and optionally `date_str` (YYYY-MM-DD,
    defaults to today). Peak-season essential for Highland camping.
    """
    return await tool_get_midge_forecast(region, latitude, longitude, date_str)


@mcp.tool()
async def find_overnight_spots(
    latitude: float,
    longitude: float,
    radius_km: float = 20,
    spot_type: Literal["free", "campsite", "motorhome", "all"] = "all",
) -> dict[str, Any]:
    """Campsites, motorhome/caravan sites and stopovers near a point (OpenStreetMap).

    spot_type "free" favours no-fee sites and motorhome-friendly parking. Includes a
    note on Scotland's wild-camping access rules.
    """
    return await tool_find_overnight_spots(latitude, longitude, radius_km, spot_type)


@mcp.tool()
async def find_attractions(
    latitude: float,
    longitude: float,
    radius_km: float = 25,
    category: Literal["distillery", "castle", "viewpoint", "attraction", "all"] = "all",
) -> dict[str, Any]:
    """Nearby distilleries, castles, viewpoints and attractions (OpenStreetMap), sorted by distance."""
    return await tool_find_attractions(latitude, longitude, radius_km, category)


@mcp.tool()
async def find_pubs(
    latitude: float,
    longitude: float,
    radius_km: float = 10,
    filter_by: Literal["all", "real_ale", "food", "step_free", "outdoor"] = "all",
) -> dict[str, Any]:
    """Nearby pubs and bars (OpenStreetMap), sorted by distance.

    filter_by narrows to real-ale pubs, food-serving pubs, step_free (no steps at the door —
    helpful for anyone who walks but can't manage steps), or those with outdoor seating.
    Attributes are as complete as OSM contributors made them.
    """
    return await tool_find_pubs(latitude, longitude, radius_km, filter_by)


@mcp.tool()
async def get_train_departures(
    station: str,
    destination: Optional[str] = None,
    rows: int = 8,
    board: Literal["departures", "arrivals"] = "departures",
) -> dict[str, Any]:
    """Live train departures/arrivals for a Scottish station (National Rail Darwin feed).

    station: name or CRS code — e.g. "Edinburgh", "Fort William", "Mallaig", "Glenfinnan",
    "Kyle of Lochalsh", "Musselburgh". Optionally filter by `destination`.
    Returns scheduled vs expected time, platform, operator, cancellations and disruption notices.
    """
    return await tool_get_train_departures(station, destination, rows, board)


@mcp.tool()
async def get_train_tickets(
    origin: str,
    destination: str,
    date_str: Optional[str] = None,
    time_str: str = "09:00",
    adults: int = 1,
) -> dict[str, Any]:
    """Pre-filled booking link for a Scottish rail journey, so the user sees the real price.

    Live UK rail fares are behind a licensed API, so this deliberately does NOT quote a price.
    It returns a National Rail journey-planner link with origin, destination, date and time
    filled in, plus practical fare notes (walk-up vs advance, railcards, rover passes).
    """
    return await tool_get_train_tickets(origin, destination, date_str, time_str, adults)


# --------------------------------------------------------------------------- #
# FastAPI app — REST endpoints + health, with the MCP app mounted at /mcp
# --------------------------------------------------------------------------- #

TOOL_NAMES = [
    "get_scotland_weather",
    "get_daylight_times",
    "get_road_incidents_scotland",
    "find_campervan_essentials",
    "get_eclipse_viewing",
    "get_midge_forecast",
    "find_overnight_spots",
    "find_attractions",
    "find_pubs",
    "get_train_departures",
    "get_train_tickets",
]

# Build the MCP ASGI sub-app once and reuse its lifespan (starts the session manager).
mcp_app = mcp.streamable_http_app()

app = FastAPI(
    title="Scotland Explorer MCP",
    version="1.0.0",
    description="Campervan trip helper (weather, daylight, road incidents, essentials).",
    lifespan=mcp_app.router.lifespan_context,
)


# ---- Request models (pydantic) -------------------------------------------- #

class WeatherRequest(BaseModel):
    region: str = Field(..., examples=["Isle of Skye"])
    start_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    end_date: Optional[str] = Field(None, description="YYYY-MM-DD")


class DaylightRequest(BaseModel):
    latitude: float = Field(..., examples=[57.5])
    longitude: float = Field(..., examples=[-6.2])
    date_str: Optional[str] = Field(None, description="YYYY-MM-DD")


class IncidentsRequest(BaseModel):
    region: Optional[str] = Field(None, examples=["A82"])


class EssentialsRequest(BaseModel):
    latitude: float = Field(..., examples=[56.6])
    longitude: float = Field(..., examples=[-5.1])
    radius_km: float = 20
    essential_type: Literal["fuel", "supermarket", "water_waste", "all"] = "all"


class EclipseRequest(BaseModel):
    region: Optional[str] = Field(None, examples=["Isle of Skye"])
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class MidgeRequest(BaseModel):
    region: Optional[str] = Field(None, examples=["Glen Coe"])
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    date_str: Optional[str] = Field(None, description="YYYY-MM-DD")


class OvernightRequest(BaseModel):
    latitude: float = Field(..., examples=[57.27])
    longitude: float = Field(..., examples=[-6.21])
    radius_km: float = 20
    spot_type: Literal["free", "campsite", "motorhome", "all"] = "all"


class AttractionsRequest(BaseModel):
    latitude: float = Field(..., examples=[57.27])
    longitude: float = Field(..., examples=[-6.21])
    radius_km: float = 25
    category: Literal["distillery", "castle", "viewpoint", "attraction", "all"] = "all"


class TrainsRequest(BaseModel):
    station: str = Field(..., examples=["Fort William"])
    destination: Optional[str] = None
    rows: int = 8
    board: Literal["departures", "arrivals"] = "departures"


class TicketsRequest(BaseModel):
    origin: str = Field(..., examples=["Musselburgh"])
    destination: str = Field(..., examples=["Edinburgh"])
    date_str: Optional[str] = Field(None, description="YYYY-MM-DD")
    time_str: str = "09:00"
    adults: int = 1


class PubsRequest(BaseModel):
    latitude: float = Field(..., examples=[55.9486])
    longitude: float = Field(..., examples=[-3.1999])
    radius_km: float = 10
    filter_by: Literal["all", "real_ale", "food", "step_free", "outdoor"] = "all"


# ---- Health --------------------------------------------------------------- #

@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "tools": TOOL_NAMES, "mcp_endpoint": "/mcp"}


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "Scotland Explorer MCP",
        "trip": "31 Jul – 24 Aug 2026",
        "health": "/health",
        "mcp": "/mcp (Streamable HTTP for Claude.ai connectors)",
        "rest_tools": [f"POST /tools/{n}" for n in TOOL_NAMES],
    }


# ---- REST tool endpoints -------------------------------------------------- #

@app.post("/tools/get_scotland_weather")
async def rest_weather(req: WeatherRequest) -> JSONResponse:
    return JSONResponse(await tool_get_scotland_weather(req.region, req.start_date, req.end_date))


@app.post("/tools/get_daylight_times")
async def rest_daylight(req: DaylightRequest) -> JSONResponse:
    return JSONResponse(await tool_get_daylight_times(req.latitude, req.longitude, req.date_str))


@app.post("/tools/get_road_incidents_scotland")
async def rest_incidents(req: IncidentsRequest) -> JSONResponse:
    return JSONResponse(await tool_get_road_incidents_scotland(req.region))


@app.post("/tools/find_campervan_essentials")
async def rest_essentials(req: EssentialsRequest) -> JSONResponse:
    return JSONResponse(
        await tool_find_campervan_essentials(
            req.latitude, req.longitude, req.radius_km, req.essential_type
        )
    )


@app.post("/tools/get_eclipse_viewing")
async def rest_eclipse(req: EclipseRequest) -> JSONResponse:
    return JSONResponse(await tool_get_eclipse_viewing(req.region, req.latitude, req.longitude))


@app.post("/tools/get_midge_forecast")
async def rest_midge(req: MidgeRequest) -> JSONResponse:
    return JSONResponse(await tool_get_midge_forecast(req.region, req.latitude, req.longitude, req.date_str))


@app.post("/tools/find_overnight_spots")
async def rest_overnight(req: OvernightRequest) -> JSONResponse:
    return JSONResponse(await tool_find_overnight_spots(req.latitude, req.longitude, req.radius_km, req.spot_type))


@app.post("/tools/find_attractions")
async def rest_attractions(req: AttractionsRequest) -> JSONResponse:
    return JSONResponse(await tool_find_attractions(req.latitude, req.longitude, req.radius_km, req.category))


@app.post("/tools/find_pubs")
async def rest_pubs(req: PubsRequest) -> JSONResponse:
    return JSONResponse(await tool_find_pubs(req.latitude, req.longitude, req.radius_km, req.filter_by))


@app.post("/tools/get_train_departures")
async def rest_trains(req: TrainsRequest) -> JSONResponse:
    return JSONResponse(await tool_get_train_departures(req.station, req.destination, req.rows, req.board))


@app.post("/tools/get_train_tickets")
async def rest_tickets(req: TicketsRequest) -> JSONResponse:
    return JSONResponse(
        await tool_get_train_tickets(req.origin, req.destination, req.date_str, req.time_str, req.adults)
    )


# Mount the MCP Streamable HTTP app at root. FastAPI's own routes (/, /health,
# /tools/*) are registered above and match first; anything else — i.e. /mcp —
# falls through to the MCP app, giving a clean POST https://host/mcp with no redirect.
app.mount("/", mcp_app)


if __name__ == "__main__":
    import os

    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
