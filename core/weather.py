"""Weather from Open-Meteo: free, no key, no account.

Verified live 2026-09-17: geocoding and a forecast each answer in under a second.
The city is typed once in Settings; nothing is guessed from the user's IP
address. Names must be in Latin letters - Open-Meteo's geocoder finds
"Kathmandu" but returns nothing for "काठमाडौं".

Portable: urllib only.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .tools.registry import Tier, ToolError, ToolRegistry

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
#: A forecast barely changes in a quarter of an hour, and every request is a round trip.
FORECAST_TTL_S = 15 * 60

FetchFn = Callable[[str, dict[str, Any]], dict[str, Any]]

#: WMO weather interpretation codes, as Open-Meteo documents them.
WMO = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "rain showers", 81: "rain showers", 82: "violent rain showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


class WeatherError(Exception):
    """Something the user can act on, in plain words."""


def fetch_json(url: str, params: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "Bantu"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise WeatherError(f"the weather service could not be reached ({e})") from e


@dataclass(frozen=True)
class Place:
    name: str
    region: str
    country: str
    latitude: float
    longitude: float

    @property
    def label(self) -> str:
        return ", ".join(p for p in (self.name, self.region, self.country) if p)


@dataclass
class Day:
    low: float
    high: float
    condition: str
    rain_chance: int | None


@dataclass
class Forecast:
    place: Place
    now_temp: float
    feels_like: float
    now_condition: str
    today: Day
    tomorrow: Day | None

    def describe(self) -> str:
        def day(label: str, d: Day) -> str:
            rain = f", {d.rain_chance}% chance of rain" if d.rain_chance is not None else ""
            return f"{label} {d.low:.0f}-{d.high:.0f}°C, {d.condition}{rain}."

        text = (f"{self.place.label}: {self.now_temp:.0f}°C now (feels like {self.feels_like:.0f}°C), "
                f"{self.now_condition}. " + day("Today", self.today))
        return text + (" " + day("Tomorrow", self.tomorrow) if self.tomorrow else "")


class Weather:
    """Geocode once per city, cache forecasts briefly."""

    def __init__(self, fetch: FetchFn = fetch_json, ttl_s: float = FORECAST_TTL_S):
        self.fetch = fetch
        self.ttl_s = ttl_s
        self._places: dict[str, Place] = {}
        self._forecasts: dict[str, tuple[float, Forecast]] = {}
        self._lock = threading.Lock()

    def find_place(self, city: str) -> Place:
        key = " ".join((city or "").split()).lower()
        if not key:
            raise WeatherError("no city is set. Add one in Settings, in Latin letters, e.g. Kathmandu.")
        with self._lock:
            if key in self._places:
                return self._places[key]
        data = self.fetch(GEOCODE_URL, {"name": city.strip(), "count": 1, "language": "en", "format": "json"})
        results = data.get("results") or []
        if not results:
            hint = " Write it in Latin letters, e.g. Kathmandu." if any(ord(c) > 0x2FF for c in city) else ""
            raise WeatherError(f"no place called {city.strip()!r} was found.{hint}")
        r = results[0]
        place = Place(r.get("name", city), r.get("admin1", "") or "", r.get("country", "") or "",
                      float(r["latitude"]), float(r["longitude"]))
        with self._lock:
            self._places[key] = place
        return place

    def forecast(self, city: str) -> Forecast:
        place = self.find_place(city)
        with self._lock:
            cached = self._forecasts.get(place.label)
            if cached and time.monotonic() - cached[0] < self.ttl_s:
                return cached[1]
        data = self.fetch(FORECAST_URL, {
            "latitude": place.latitude, "longitude": place.longitude,
            "current": "temperature_2m,apparent_temperature,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto", "forecast_days": 2,
        })
        try:
            now, daily = data["current"], data["daily"]

            def day(i: int) -> Day:
                rain = (daily.get("precipitation_probability_max") or [None] * 2)[i]
                return Day(float(daily["temperature_2m_min"][i]), float(daily["temperature_2m_max"][i]),
                           WMO.get(int(daily["weather_code"][i]), "unsettled"),
                           None if rain is None else int(rain))

            fc = Forecast(place, float(now["temperature_2m"]), float(now["apparent_temperature"]),
                          WMO.get(int(now["weather_code"]), "unsettled"), day(0),
                          day(1) if len(daily.get("time", [])) > 1 else None)
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise WeatherError(f"the weather service sent something unexpected ({e})") from e
        with self._lock:
            self._forecasts[place.label] = (time.monotonic(), fc)
        return fc


def register(reg: ToolRegistry, weather: Weather, settings: Any) -> None:
    reg.describe_category("weather", "current weather and today's and tomorrow's forecast for any city")

    @reg.register(tier=Tier.AUTO, category="weather")
    def get_weather(city: str = "") -> str:
        """Current weather and the forecast for today and tomorrow.

        Args:
            city: A city in Latin letters, e.g. "Pokhara". Leave empty for the user's own city from Settings.
        """
        chosen = city.strip() or getattr(settings, "weather_city", "")
        try:
            return weather.forecast(chosen).describe()
        except WeatherError as e:
            raise ToolError(str(e)) from e
