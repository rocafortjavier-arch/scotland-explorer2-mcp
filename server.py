#!/usr/bin/env python3
"""
Scotland Explorer MCP Server
Logistics-focused tools for campervan trip planning and real-time support.
Trip dates: 31 Jul - 24 Aug 2026

Usage:
    python3 -m mcp.server server:mcp
    Or with mcp cli:
    mcp run server.py
"""

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from fastmcp import FastMCP

from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, date
import httpx
import json

# Initialize MCP server
mcp = FastMCP("scotland-explorer")

# ============================================================================
# TOOL 1: GET SCOTLAND WEATHER
# ============================================================================

class WeatherResponse(BaseModel):
    region: str
    forecast_date: str
    temperature_min: float
    temperature_max: float
    rain_probability: int
    wind_speed: float
    wind_direction: str
    conditions: str
    visibility: str
    camping_suitability: str  # logistics insight
    notes: str


@mcp.tool()
def get_scotland_weather(region: str, start_date: Optional[str] = None, end_date: Optional[str] = None) -> dict:
    """
    Get weather forecast for a Scottish region.
    
    Args:
        region: Region name (e.g., 'Edinburgh', 'Highlands', 'Isle of Skye', 'Glasgow')
        start_date: Optional start date (YYYY-MM-DD). Defaults to today.
        end_date: Optional end date (YYYY-MM-DD). Defaults to 7 days from start.
    
    Returns:
        Weather forecast with logistics-relevant insights for campervan planning.
    """
    # Scottish region coordinates (latitude, longitude)
    regions = {
        "edinburgh": (55.9533, -3.1883),
        "glasgow": (55.8642, -4.2518),
        "highlands": (57.5, -4.5),
        "isle of skye": (57.5, -6.2),
        "loch ness": (57.3, -4.5),
        "glen coe": (56.6, -5.1),
        "cairngorms": (57.1, -3.8),
        "west coast": (56.5, -5.5),
        "outer hebrides": (57.5, -7.0),
        "default": (56.5, -4.0)
    }
    
    coords = regions.get(region.lower(), regions["default"])
    lat, lon = coords
    
    # Use Open-Meteo API (free, no key required)
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,windspeed_10m_max,weathercode",
        "temperature_unit": "celsius",
        "windspeed_unit": "kmh",
        "timezone": "Europe/London"
    }
    
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date
    
    try:
        response = httpx.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        forecasts = []
        daily = data.get("daily", {})
        times = daily.get("time", [])
        temps_max = daily.get("temperature_2m_max", [])
        temps_min = daily.get("temperature_2m_min", [])
        precip = daily.get("precipitation_sum", [])
        wind = daily.get("windspeed_10m_max", [])
        codes = daily.get("weathercode", [])
        
        for i, time in enumerate(times):
            # WMO weather codes interpretation
            code = codes[i] if i < len(codes) else 0
            if code in [0]:
                conditions = "Clear"
            elif code in [1, 2]:
                conditions = "Mostly clear"
            elif code in [3]:
                conditions = "Overcast"
            elif code in [45, 48]:
                conditions = "Foggy"
            elif code in [51, 53, 55]:
                conditions = "Light drizzle"
            elif code in [61, 63, 65]:
                conditions = "Rain"
            elif code in [71, 73, 75, 77]:
                conditions = "Snow"
            else:
                conditions = "Variable"
            
            precip_val = precip[i] if i < len(precip) else 0
            rain_prob = min(int(precip_val * 15), 95)  # Rough estimate
            
            # Camping suitability assessment
            if rain_prob > 70 or precip_val > 10:
                suitability = "⚠ Wet — expect soggy ground"
            elif wind[i] > 40 if i < len(wind) else False:
                suitability = "⚠ Windy — secure tent well"
            elif temps_min[i] < 5 if i < len(temps_min) else False:
                suitability = "Cold but clear — good for sleeping"
            else:
                suitability = "✓ Good camping conditions"
            
            forecasts.append({
                "date": time,
                "temp_min": round(temps_min[i], 1) if i < len(temps_min) else None,
                "temp_max": round(temps_max[i], 1) if i < len(temps_max) else None,
                "conditions": conditions,
                "rain_probability": rain_prob,
                "rain_mm": round(precip_val, 1) if i < len(precip) else 0,
                "wind_kmh": round(wind[i], 1) if i < len(wind) else None,
                "camping_suitability": suitability
            })
        
        return {
            "status": "success",
            "region": region,
            "coordinates": {"lat": lat, "lon": lon},
            "forecast_count": len(forecasts),
            "forecasts": forecasts,
            "notes": "Based on Open-Meteo data. Camping suitability is advisory only."
        }
    
    except Exception as e:
        return {
            "status": "error",
            "region": region,
            "error": str(e),
            "message": "Failed to fetch weather data"
        }


# ============================================================================
# TOOL 2: GET ROAD INCIDENTS SCOTLAND
# ============================================================================

class RoadIncident(BaseModel):
    location: str
    incident_type: str  # closure, roadworks, accident, etc.
    severity: str  # minor, moderate, severe
    description: str
    expected_duration: Optional[str]
    start_date: str


@mcp.tool()
def get_road_incidents_scotland(region: Optional[str] = None) -> dict:
    """
    Get current road incidents and closures in Scotland.
    
    Args:
        region: Optional region filter (e.g., 'A82', 'M8', 'Highlands').
                If None, returns all major incidents.
    
    Returns:
        List of road incidents affecting travel planning.
    """
    # Transport Scotland open data feed
    url = "https://www.traffic.gov.scot/api/v2/incidents"
    
    try:
        response = httpx.get(url, timeout=10)
        response.raise_for_status()
        
        # The API returns XML, try JSON endpoint if available
        # Fallback: parse the response
        data = response.json() if response.headers.get("content-type") == "application/json" else response.text
        
        incidents = []
        
        # Parse based on format
        if isinstance(data, dict):
            # JSON format
            features = data.get("features", [])
        elif isinstance(data, str):
            # XML or other format - return raw for now with guidance
            return {
                "status": "partial",
                "message": "Transport Scotland feed returned non-JSON format. Manual check recommended.",
                "url": "https://trafficscotland.org/tsr/",
                "note": "Check live at https://trafficscotland.org/ for real-time incidents"
            }
        else:
            features = []
        
        # Process incidents
        for feature in features:
            props = feature.get("properties", {})
            coords = feature.get("geometry", {}).get("coordinates", [0, 0])
            
            incident = {
                "location": props.get("description", "Unknown"),
                "type": props.get("category", "Incident"),
                "severity": props.get("severity", "Unknown"),
                "last_updated": props.get("lastUpdated", "Unknown"),
                "latitude": coords[1] if len(coords) > 1 else None,
                "longitude": coords[0] if len(coords) > 0 else None
            }
            
            # Apply region filter if provided
            if region:
                if region.upper() in incident["location"].upper():
                    incidents.append(incident)
            else:
                incidents.append(incident)
        
        return {
            "status": "success" if incidents else "no_incidents",
            "region_filter": region or "All Scotland",
            "incident_count": len(incidents),
            "incidents": sorted(incidents, key=lambda x: x["severity"], reverse=True)[:20],  # Top 20 by severity
            "last_updated": datetime.now().isoformat(),
            "source": "Transport Scotland",
            "note": "Check https://trafficscotland.org/ for live updates"
        }
    
    except Exception as e:
        return {
            "status": "error",
            "message": "Failed to fetch Transport Scotland data",
            "error": str(e),
            "fallback_url": "https://trafficscotland.org/tsr/",
            "advice": "Check live incident map for real-time updates"
        }


# ============================================================================
# TOOL 3: GET DAYLIGHT TIMES
# ============================================================================

class DaylightResponse(BaseModel):
    location: str
    date: str
    sunrise: str
    sunset: str
    twilight_dawn: str
    twilight_dusk: str
    daylight_hours: float
    planning_notes: str


@mcp.tool()
def get_daylight_times(latitude: float, longitude: float, date_str: Optional[str] = None) -> dict:
    """
    Get sunrise, sunset, and twilight times for a location.
    Useful for planning hikes, photography, and knowing when to stop driving.
    
    Args:
        latitude: Latitude coordinate
        longitude: Longitude coordinate
        date_str: Date in YYYY-MM-DD format. Defaults to today.
    
    Returns:
        Sunrise/sunset times and logistics notes for daylight-dependent activities.
    """
    try:
        from astral import LocationInfo
        from astral.sun import sun
        import pytz
        
        if date_str:
            target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        else:
            target_date = date.today()
        
        # Create location info
        location = LocationInfo("", "", "UTC", latitude, longitude)
        
        # Get sun times for the date (UTC, then convert to Europe/London)
        tz = pytz.timezone("Europe/London")
        s = sun(location.observer, date=target_date, tzinfo=tz)
        
        sunrise = s["sunrise"]
        sunset = s["sunset"]
        dawn = s["dawn"]
        dusk = s["dusk"]
        
        # Calculate daylight hours
        daylight_hours = (sunset - sunrise).total_seconds() / 3600
        
        # Logistics planning notes
        if daylight_hours < 8:
            daylight_note = "Short day — limit long hikes, plan early."
        elif daylight_hours < 12:
            daylight_note = "Moderate daylight — manage hiking schedule carefully."
        elif daylight_hours < 16:
            daylight_note = "Good daylight — plenty of time for activities."
        else:
            daylight_note = "Very long day — nearly midnight twilight possible."
        
        driving_note = "Twilight ends" if dusk.hour < 23 else "No full darkness (midnight sun effect)"
        
        return {
            "status": "success",
            "date": date_str or str(date.today()),
            "location": {"latitude": latitude, "longitude": longitude},
            "timezone": "Europe/London",
            "sunrise": sunrise.strftime("%H:%M"),
            "sunset": sunset.strftime("%H:%M"),
            "twilight_dawn": dawn.strftime("%H:%M"),
            "twilight_dusk": dusk.strftime("%H:%M"),
            "daylight_hours": round(daylight_hours, 1),
            "planning_notes": {
                "daylight": daylight_note,
                "driving": f"{driving_note} at {dusk.strftime('%H:%M')}",
                "activities": "Plan outdoor activities between sunrise and sunset for best light"
            }
        }
    
    except Exception as e:
        return {
            "status": "error",
            "latitude": latitude,
            "longitude": longitude,
            "date": date_str or str(date.today()),
            "error": str(e),
            "fallback": "Use online sunrise/sunset calculator for location"
        }


# ============================================================================
# TOOL 4: FIND CAMPERVAN ESSENTIALS
# ============================================================================

class Essential(BaseModel):
    name: str
    type: str  # fuel, supermarket, water_waste, etc.
    distance_km: float
    lat: float
    lon: float
    opening_hours: Optional[str]
    notes: Optional[str]


@mcp.tool()
def find_campervan_essentials(latitude: float, longitude: float, radius_km: float = 20, 
                              essential_type: str = "all") -> dict:
    """
    Find campervan essentials near a location.
    
    Args:
        latitude: Latitude coordinate
        longitude: Longitude coordinate
        radius_km: Search radius in kilometers (default 20)
        essential_type: Type of essential ('fuel', 'supermarket', 'water_waste', 'all')
    
    Returns:
        List of nearby essentials with distance and opening hours.
    """
    # Overpass API queries for different essential types
    queries = {
        "fuel": '[out:json];(node["amenity"="fuel"](around:RADIUS,LAT,LON););out center;',
        "supermarket": '[out:json];(node["shop"="supermarket"](around:RADIUS,LAT,LON););out center;',
        "water_waste": '[out:json];(node["amenity"="waste_disposal"](around:RADIUS,LAT,LON);node["amenity"="water_point"](around:RADIUS,LAT,LON););out center;',
        "all": '[out:json];(node["amenity"="fuel"](around:RADIUS,LAT,LON);node["shop"="supermarket"](around:RADIUS,LAT,LON);node["amenity"="waste_disposal"](around:RADIUS,LAT,LON););out center;'
    }
    
    if essential_type not in queries:
        essential_type = "all"
    
    query_template = queries[essential_type]
    radius_m = int(radius_km * 1000)
    query = query_template.replace("RADIUS", str(radius_m)).replace("LAT", str(latitude)).replace("LON", str(longitude))
    
    url = "https://overpass-api.de/api/interpreter"
    
    try:
        response = httpx.post(url, data=query, timeout=15)
        response.raise_for_status()
        data = response.json()
        
        essentials = []
        
        for element in data.get("elements", []):
            tags = element.get("tags", {})
            center = element.get("center") or {"lat": element.get("lat"), "lon": element.get("lon")}
            
            if not center:
                continue
            
            # Calculate distance
            import math
            lat2 = center.get("lat")
            lon2 = center.get("lon")
            
            # Haversine formula
            R = 6371  # Earth radius in km
            dLat = math.radians(lat2 - latitude)
            dLon = math.radians(lon2 - longitude)
            a = math.sin(dLat/2)**2 + math.cos(math.radians(latitude)) * math.cos(math.radians(lat2)) * math.sin(dLon/2)**2
            c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
            distance = R * c
            
            # Determine type and name
            if tags.get("amenity") == "fuel":
                etype = "fuel"
                name = tags.get("name", "Fuel Station")
                brand = tags.get("brand", "")
            elif tags.get("shop") == "supermarket":
                etype = "supermarket"
                name = tags.get("name", "Supermarket")
                brand = ""
            elif tags.get("amenity") in ["waste_disposal", "water_point"]:
                etype = "waste/water"
                name = tags.get("name", f"{tags.get('amenity')}")
                brand = ""
            else:
                continue
            
            essential = {
                "name": f"{name} {brand}".strip(),
                "type": etype,
                "distance_km": round(distance, 2),
                "latitude": lat2,
                "longitude": lon2,
                "opening_hours": tags.get("opening_hours", "Check locally"),
                "phone": tags.get("phone", "")
            }
            
            essentials.append(essential)
        
        # Sort by distance and limit to top results
        essentials.sort(key=lambda x: x["distance_km"])
        essentials = essentials[:15]
        
        return {
            "status": "success",
            "location": {"latitude": latitude, "longitude": longitude},
            "search_radius_km": radius_km,
            "essential_type": essential_type,
            "results_count": len(essentials),
            "essentials": essentials,
            "source": "OpenStreetMap via Overpass API",
            "note": "Opening hours may be outdated. Verify before visiting."
        }
    
    except Exception as e:
        return {
            "status": "error",
            "location": {"latitude": latitude, "longitude": longitude},
            "error": str(e),
            "message": "Failed to fetch essentials data",
            "fallback": "Use Google Maps or offline maps app to find fuel, supermarkets, and waste disposal"
        }


# ============================================================================
# HEALTH CHECK
# ============================================================================

@mcp.tool()
def health_check() -> dict:
    """Quick health check to verify server is running."""
    return {
        "status": "ok",
        "server": "scotland-explorer-mcp",
        "tools": ["get_scotland_weather", "get_road_incidents_scotland", "get_daylight_times", "find_campervan_essentials"]
    }


if __name__ == "__main__":
    mcp.run()
