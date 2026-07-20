#!/usr/bin/env python3
"""
Scotland Explorer MCP Server
Logistics-focused tools for campervan trip planning and real-time support.
Trip dates: 31 Jul - 24 Aug 2026

Usage:
    python3 -m mcp.server.stdio server:mcp
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
import math

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
    camping_suitability: str  # logistics