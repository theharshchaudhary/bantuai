"""R3 tests: personality, greeting, weather, the briefing and the calendar.

No API key, no network: weather and calendar feeds are faked.

    .venv/Scripts/python.exe tests/test_r3_briefing.py
"""

from __future__ import annotations

import datetime
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="bantu_r3_appdata_")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core import config as cfg
from core.agent import SYSTEM_TEMPLATE, TONES, Agent
from core.memory import Memory
from core.providers.base import LLMProvider, LLMResponse
from core.providers.router import ProviderRouter
from core.tools.registry import ToolRegistry

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


class Capture(LLMProvider):
    name = "capture"

    def __init__(self):
        self.systems = []

    def available_models(self):
        return ["c"]

    def resolve_model(self, preferences):
        return "c"

    def supports_vision(self):
        return False

    def chat(self, messages, tools=None, system=None, **kw):
        self.systems.append(system or "")
        return LLMResponse(text="hi", provider=self.name, model="c")


def system_for(**settings):
    prov = Capture()
    s = cfg.Settings(username="Harsh", **settings)
    Agent(ProviderRouter([prov]), ToolRegistry(), Memory(Path(tempfile.mkdtemp()) / "t.db"), s).run("hello")
    return prov.systems[0]


# --- personality ---------------------------------------------------------------

print("\n[personality]")
check("warm professional is the default", cfg.Settings().tone == "warm")
check("the default prompt carries the warm manner", TONES["warm"] in system_for())
check("playful is used when chosen", TONES["playful"] in system_for(tone="playful")
      and TONES["warm"] not in system_for(tone="playful"))
check("strictly professional is used when chosen", TONES["professional"] in system_for(tone="professional"))
check("an unknown tone falls back to warm rather than breaking the prompt", TONES["warm"] in system_for(tone="grumpy"))
check("every tone keeps the work brief and exact where it matters",
      "brief and exact" in TONES["playful"] and "never gushing" in TONES["warm"])
check("the template has one tone slot", SYSTEM_TEMPLATE.count("{tone}") == 1)
s = cfg.Settings(tone="playful")
s.save()
check("the tone survives a restart", cfg.Settings.load().tone == "playful")

print("\n[reply language follows the message, not the data]")
from core.agent import language_hint

# Found live: "Brief me." in English, answered in Hindi by gemini-3.5-flash-lite
# because one commitment in the briefing had been noted in Hindi.
check("an English message is told to get a reply in Latin letters",
      "Latin letters" in language_hint("Brief me."))
check("a Devanagari message is told to get a Devanagari reply", "Devanagari" in language_hint("मुझे ब्रीफ करो"))
check("romanized Nepali also stays in Latin letters", "Latin letters" in language_hint("mero battery kati cha?"))
check("the hint reaches the model with every request", language_hint("hello") in system_for())
check("...and the general rule covers tool results in another language",
      "even when tool results" in " ".join(SYSTEM_TEMPLATE.split()))

print("\n[greeting]")
from ui.app import greeting


def at(hour):
    return datetime.datetime(2026, 9, 17, hour, 5)


check("morning, afternoon, evening by the hour",
      [greeting("Harsh", at(h)) for h in (7, 13, 19)] == ["Good morning, Harsh.", "Good afternoon, Harsh.",
                                                          "Good evening, Harsh."])
check("late at night it just says hello", greeting("Harsh", at(2)) == "Hello, Harsh.")
check("without a name there is no dangling comma", greeting("", at(9)) == "Good morning." and
      greeting("  ", at(9)) == "Good morning.")
check("the boundaries fall where people expect", greeting("", at(5)) == "Good morning." and
      greeting("", at(12)) == "Good afternoon." and greeting("", at(17)) == "Good evening." and
      greeting("", at(22)) == "Hello.")

from PyQt5.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])
from ui.app import BantuApp
from ui.widgets import Bubble

hud = BantuApp(Agent(ProviderRouter([Capture()]), ToolRegistry(), Memory(Path(tempfile.mkdtemp()) / "g.db"),
                     cfg.Settings(username="Harsh")), cfg.Settings(username="Harsh"),
               install_hotkey=False, show_tray=False)
first = [w.label.text() for w in hud.panel.items() if isinstance(w, Bubble)][0]
check("the HUD greets by name and time of day", first.startswith(greeting("Harsh")) and "Bantu is ready" in first, first)
hud.shutdown()

from ui.settings_dialog import SettingsDialog
from ui.setup_parts import Services

services = Services(check_key=lambda p, k: None, store_key=lambda p, k: None,
                    existing_key=lambda p: ("gsk_existing", "keyring"), list_devices=lambda: [],
                    preview_voice=lambda s, g, t: None, open_url=lambda u: None)
settings = cfg.Settings(username="Harsh")
dlg = SettingsDialog(settings, services)
check("Settings shows the current tone", dlg.tone.currentData() == "warm")
changes = []
dlg.applied.connect(changes.append)
dlg.tone.setCurrentIndex(dlg.tone.findData("professional"))
dlg.save()
check("choosing a tone in Settings applies it at once", settings.tone == "professional" and changes
      and "tone" in changes[0], str(changes))


# --- weather -----------------------------------------------------------------------

print("\n[weather]")
import time as _time

from core.weather import FORECAST_URL, GEOCODE_URL, WMO, Weather, WeatherError
from core.weather import register as register_weather
from core.providers.base import ToolCall

GEO = {"results": [{"name": "Kathmandu", "admin1": "Bagmati Province", "country": "Nepal",
                    "latitude": 27.70169, "longitude": 85.3206}]}
FORECAST = {
    "current": {"temperature_2m": 24.7, "apparent_temperature": 28.2, "weather_code": 53},
    "daily": {"time": ["2026-09-17", "2026-09-18"], "weather_code": [55, 80],
              "temperature_2m_max": [25.3, 25.5], "temperature_2m_min": [17.5, 17.4],
              "precipitation_probability_max": [96, 100]},
}


class FakeFetch:
    def __init__(self, responses=None, fail=None):
        self.responses = responses or {GEOCODE_URL: GEO, FORECAST_URL: FORECAST}
        self.fail = fail
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if self.fail:
            raise WeatherError(self.fail)
        return self.responses[url]


fetch = FakeFetch()
w = Weather(fetch)
fc = w.forecast("Kathmandu")
text = fc.describe()
check("a forecast reads naturally, with place, now, today and tomorrow",
      text == "Kathmandu, Bagmati Province, Nepal: 25°C now (feels like 28°C), drizzle. "
              "Today 18-25°C, heavy drizzle, 96% chance of rain. Tomorrow 17-26°C, rain showers, 100% chance of rain.",
      text)
check("the forecast asks for the place's own time zone", fetch.calls[1][1]["timezone"] == "auto")
w.forecast("  kathmandu ")
check("the same city is not looked up or fetched again within the cache window",
      len(fetch.calls) == 2, str([c[0] for c in fetch.calls]))
w_expired = Weather(FakeFetch(), ttl_s=0)
w_expired.forecast("Kathmandu")
w_expired.forecast("Kathmandu")
check("after the cache window the forecast is fetched again, the place is not",
      [c[0] for c in w_expired.fetch.calls] == [GEOCODE_URL, FORECAST_URL, FORECAST_URL])

try:
    Weather(FakeFetch({GEOCODE_URL: {"generationtime_ms": 0.6}})).find_place("Zzqxplonk")
    check("an unknown city is an error", False)
except WeatherError as e:
    check("an unknown city says so plainly", "no place called 'Zzqxplonk' was found" in str(e), str(e))
try:
    Weather(FakeFetch({GEOCODE_URL: {}})).find_place("काठमाडौं")
    check("a Devanagari city is an error", False)
except WeatherError as e:
    check("a city in Devanagari is told to use Latin letters (the geocoder finds nothing otherwise)",
          "Latin letters" in str(e), str(e))
try:
    Weather(FakeFetch()).find_place("   ")
    check("no city is an error", False)
except WeatherError as e:
    check("no city set says where to set it", "Settings" in str(e), str(e))
try:
    Weather(FakeFetch(fail="the weather service could not be reached (timed out)")).forecast("Kathmandu")
    check("an outage is an error", False)
except WeatherError as e:
    check("an outage is reported, not crashed on", "could not be reached" in str(e))
bad = dict(FORECAST, current={})
try:
    Weather(FakeFetch({GEOCODE_URL: GEO, FORECAST_URL: bad})).forecast("Kathmandu")
    check("a malformed forecast is an error", False)
except WeatherError as e:
    check("a malformed forecast is reported plainly", "unexpected" in str(e), str(e))
check("every WMO code in the forecast maps to words", all(isinstance(v, str) and v for v in WMO.values()))

reg = ToolRegistry()
settings_w = cfg.Settings(weather_city="Kathmandu")
register_weather(reg, Weather(FakeFetch()), settings_w)
check("get_weather uses the city from Settings when none is named",
      reg.execute(ToolCall("w", "get_weather", {})).startswith("Kathmandu"))
other = FakeFetch({GEOCODE_URL: {"results": [dict(GEO["results"][0], name="Pokhara", admin1="Gandaki Province")]},
                   FORECAST_URL: FORECAST})
reg2 = ToolRegistry()
register_weather(reg2, Weather(other), settings_w)
check("get_weather takes another city when asked", reg2.execute(ToolCall("w", "get_weather", {"city": "Pokhara"}))
      .startswith("Pokhara") and other.calls[0][1]["name"] == "Pokhara")
reg3 = ToolRegistry()
register_weather(reg3, Weather(FakeFetch()), cfg.Settings())
check("with no city anywhere, get_weather says how to fix it",
      reg3.execute(ToolCall("w", "get_weather", {})).startswith("Error: no city is set"))


# --- the briefing --------------------------------------------------------------------

print("\n[briefing]")
from core.briefing import build_briefing
from core.briefing import register as register_briefing
from core.records import Records

memory = Memory(Path(tempfile.mkdtemp()) / "b.db")
records = Records(memory.db)
now = datetime.datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
ts = now.timestamp()
memory.add_reminder("Stand-up call", ts + 3600)
memory.add_reminder("Pay rent tomorrow", ts + 26 * 3600)
records.add("commitment", "Send Ram the vendor report", "Ram", due_at=ts - 3600)
records.add("task", "Renew the domain", due_at=ts + 5 * 3600)
records.add("task", "Plan the offsite", due_at=ts + 6 * 86400)
done = records.add("task", "Old finished task", due_at=ts - 7200)
records.set_status(done.id, "done")
records.add("decision", "Launch moves back one week")

text = build_briefing(memory, records, lambda: "Kathmandu: 18°C, clear.", lambda day: ["10:00 Team stand-up"], now)
lines = {line.split(":")[0]: line for line in text.splitlines()}
check("the briefing opens with the date and time", text.startswith(f"Now: {now:%A %d %B %Y}, 09:00."), text)
check("weather is included", lines["Weather"] == "Weather: Kathmandu: 18°C, clear.")
check("today's calendar is included", lines["Calendar today"] == "Calendar today: 10:00 Team stand-up")
check("only today's reminders are listed", "Stand-up call" in lines["Reminders still to come today"]
      and "rent" not in lines["Reminders still to come today"])
check("overdue work is named with its person and due date", "Send Ram the vendor report (Ram), due" in lines["Overdue"])
check("work due later today is separate from overdue", "Renew the domain" in lines["Due today"]
      and "Renew" not in lines["Overdue"])
check("later open work is listed after", "Plan the offsite" in lines["Other open commitments and tasks"])
check("finished work is left out", "Old finished task" not in text)
check("recent decisions are mentioned", "Launch moves back one week" in text)

quiet = build_briefing(Memory(Path(tempfile.mkdtemp()) / "q.db"),
                       Records(Memory(Path(tempfile.mkdtemp()) / "q2.db").db), None, None, now)
check("an empty day still briefs, saying there is nothing", "Overdue: nothing." in quiet and "Due today: nothing." in quiet
      and "Reminders still to come today: none." in quiet)
check("without a city it says weather can be added in Settings", "no city is set" in quiet and "Settings" in quiet)
check("without a calendar the calendar section is simply absent", "Calendar" not in quiet)


def broken_weather():
    raise WeatherError("the weather service could not be reached")


def broken_calendar(day):
    raise RuntimeError("feed timed out")


rough = build_briefing(memory, records, broken_weather, broken_calendar, now)
check("a failing section is reported and the rest of the briefing still comes",
      "Weather: unavailable" in rough and "Calendar: unavailable" in rough and "Send Ram" in rough, rough)

many = Records(Memory(Path(tempfile.mkdtemp()) / "m.db").db)
for i in range(9):
    many.add("task", f"Chore number {i}")
long_text = build_briefing(Memory(Path(tempfile.mkdtemp()) / "m2.db"), many, None, None, now)
check("a long list is capped with a count of the rest", "and 4 more" in long_text, long_text)

reg = ToolRegistry()
city = {"value": ""}
register_briefing(reg, memory, records,
                  lambda: (lambda: f"{city['value']}: 20°C.") if city["value"] else None)
check("daily_briefing is a core tool, always available for 'brief me'",
      reg.tools["daily_briefing"].category == "core" and reg.tools["daily_briefing"].tier.value == "auto")
check("before a city is set the briefing says so", "no city is set" in reg.execute(ToolCall("b", "daily_briefing", {})))
city["value"] = "Pokhara"
check("a city set later is used without restarting", "Weather: Pokhara: 20°C." in
      reg.execute(ToolCall("b", "daily_briefing", {})))

main_src = (ROOT / "main.py").read_text(encoding="utf-8")
check("the app registers weather and the briefing, reading the city at call time",
      "weather_tools.register(_REGISTRY, weather, settings)" in main_src
      and "briefing.register(_REGISTRY, memory, records, weather_line)" in main_src
      and 'getattr(settings, "weather_city", "")' in main_src)


print("\n[weather city in Settings]")

def pump(seconds=1.0):
    end = _time.monotonic() + seconds
    while _time.monotonic() < end:
        app.processEvents()
        _time.sleep(0.01)


def lookup(city):
    if city.lower() == "kathmandu":
        return "Kathmandu, Bagmati Province, Nepal"
    raise WeatherError(f"no place called {city!r} was found.")


city_services = Services(check_key=lambda p, k: None, store_key=lambda p, k: None,
                         existing_key=lambda p: ("gsk_existing", "keyring"), list_devices=lambda: [],
                         preview_voice=lambda s, g, t: None, open_url=lambda u: None, find_place=lookup)
settings = cfg.Settings(username="Harsh")
dlg = SettingsDialog(settings, city_services)
dlg.city.setText("Kathmandu")
dlg._check_city()
pump(0.5)
check("checking a city shows where it found", dlg.city_result.text() == "Found: Kathmandu, Bagmati Province, Nepal",
      dlg.city_result.text())
dlg.city.setText("Zzqxplonk")
check("editing the city clears the old result", dlg.city_result.text() == "")
dlg._check_city()
pump(0.5)
check("a city that does not exist says so once, not twice",
      dlg.city_result.text() == "No place called 'Zzqxplonk' was found.", dlg.city_result.text())
dlg.city.setText("  Kathmandu  ")
dlg.save()
check("saving stores the city, trimmed", settings.weather_city == "Kathmandu")
check("without a lookup service the Check button is disabled", not SettingsDialog(cfg.Settings(), services).check_city.isEnabled())


if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
