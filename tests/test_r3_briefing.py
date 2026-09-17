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
      and "_REGISTRY, memory, records, weather_line," in main_src
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


# --- calendar --------------------------------------------------------------------------

print("\n[calendar store]")
from core.events import Calendar, CalendarEvent, FeedError, check_feed, fetch_feed, parse_feed
from core.events import register as register_calendar

cal_mem = Memory(Path(tempfile.mkdtemp()) / "cal.db")
cal = Calendar(cal_mem.db)
day0 = datetime.date.today() + datetime.timedelta(days=3)
base = datetime.datetime.combine(day0, datetime.time())


def ts(hour, minute=0, days=0):
    return (base + datetime.timedelta(days=days, hours=hour, minutes=minute)).timestamp()


standup = cal.add("Team stand-up", ts(10), ts(10, 30), location="Meet")
lunch = cal.add("Lunch with Sita", ts(13))
holiday = cal.add("Office closed", ts(0), ts(0, days=1), all_day=True)
trip = cal.add("Trip to Pokhara", ts(0, days=1), ts(0, days=3), all_day=True)
check("an event reads with day, times, title and place",
      standup.describe() == f"#{standup.id} · {base:%a %d %b} 10:00-10:30 · Team stand-up · at Meet", standup.describe())
check("an all-day event reads as all day", holiday.when() == f"{base:%a %d %b}, all day")
check("a multi-day all-day event shows its last day", trip.when().endswith(f"to {(day0 + datetime.timedelta(days=2)):%a %d %b}"),
      trip.when())
check("the day lists all-day events first, then by time",
      [e.title for e in cal.on_day(day0)] == ["Office closed", "Team stand-up", "Lunch with Sita"],
      str([e.title for e in cal.on_day(day0)]))
check("an all-day event does not spill into the next day",
      "Office closed" not in [e.title for e in cal.on_day(day0 + datetime.timedelta(days=1))])
check("a multi-day event shows on each of its days",
      all("Trip to Pokhara" in [e.title for e in cal.on_day(day0 + datetime.timedelta(days=d))] for d in (1, 2)))
check("...and not the day after it ends",
      "Trip to Pokhara" not in [e.title for e in cal.on_day(day0 + datetime.timedelta(days=3))])
check("the briefing line is short", standup.short() == "10:00 Team stand-up" and holiday.short() == "all day: Office closed")
def raises(fn, exc):
    try:
        fn()
        return False
    except exc:
        return True


check("a title is required", raises(lambda: cal.add("  ", ts(9)), ValueError))
check("an event cannot end before it starts", raises(lambda: cal.add("Backwards", ts(11), ts(10)), ValueError))
check("cancelling removes Bantu's own event", cal.cancel(lunch.id).title == "Lunch with Sita" and cal.get(lunch.id) is None)
check("cancelling a missing event is a KeyError", raises(lambda: cal.cancel(9999), KeyError))

print("\n[Google Calendar feed]")
monday = datetime.date.today() + datetime.timedelta(days=(0 - datetime.date.today().weekday()) % 7 or 7)
second = monday + datetime.timedelta(days=7)
ICS = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
X-WR-CALNAME:Harsh
X-WR-TIMEZONE:Asia/Kathmandu
BEGIN:VEVENT
DTSTART;TZID=Asia/Kathmandu:{monday:%Y%m%d}T100000
DTEND;TZID=Asia/Kathmandu:{monday:%Y%m%d}T103000
RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=3
EXDATE;TZID=Asia/Kathmandu:{second:%Y%m%d}T100000
SUMMARY:Weekly review
LOCATION:Office
UID:review@test
END:VEVENT
BEGIN:VEVENT
DTSTART:{monday:%Y%m%d}T090000Z
DURATION:PT45M
SUMMARY:Vendor call
UID:vendor@test
END:VEVENT
BEGIN:VEVENT
DTSTART;VALUE=DATE:{monday:%Y%m%d}
DTEND;VALUE=DATE:{(monday + datetime.timedelta(days=1)):%Y%m%d}
UID:untitled@test
END:VEVENT
BEGIN:VEVENT
DTSTART:{(datetime.date.today() + datetime.timedelta(days=200)):%Y%m%d}T090000Z
SUMMARY:Far future
UID:far@test
END:VEVENT
END:VCALENDAR
"""
name, occ = parse_feed(ICS.encode())
titles = [o["title"] for o in occ]
check("the calendar's own name is read", name == "Harsh")
check("a weekly event repeats, minus the excluded week", titles.count("Weekly review") == 2, str(titles))
vendor = next(o for o in occ if o["title"] == "Vendor call")
check("a UTC time becomes the real moment, and DURATION sets the end",
      vendor["starts_at"] == datetime.datetime(monday.year, monday.month, monday.day, 9, tzinfo=datetime.timezone.utc)
      .timestamp() and vendor["ends_at"] - vendor["starts_at"] == 45 * 60)
check("an event with no title still shows", "(no title)" in titles)
check("events beyond the window are left out", "Far future" not in titles)
check("garbage is not a calendar", raises(lambda: parse_feed(b"<html>sign in</html>"), FeedError))
check("a non-address is refused before any request", raises(lambda: fetch_feed("my calendar"), FeedError))


class FakeFeed:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        item = self.payloads.pop(0) if len(self.payloads) > 1 else self.payloads[0]
        if isinstance(item, Exception):
            raise item
        return item


feed_mem = Memory(Path(tempfile.mkdtemp()) / "feed.db")
feed = FakeFeed([ICS.encode()])
fcal = Calendar(feed_mem.db, fetch=feed)
mine = fcal.add("My own reminder event", ts(8))
check("with nothing connected, the status says so", fcal.feed_status() == "No Google Calendar connected.")
count = fcal.sync_feed("https://calendar.google.com/calendar/ical/x/private-y/basic.ics")
check("a sync copies the feed's events into the window", count == 4 and "Harsh: 4 events copied" in fcal.feed_status(),
      fcal.feed_status())
review = next(e for e in fcal.between(0, 10**11) if e.title == "Weekly review")
check("feed events are marked as coming from Google Calendar", review.source == "feed" and "from Google Calendar" in review.describe())
check("a feed event cannot be cancelled here", raises(lambda: fcal.cancel(review.id), PermissionError))
fcal.sync_feed("https://example/basic.ics")
check("syncing again replaces the copy rather than duplicating it",
      sum(1 for e in fcal.between(0, 10**11) if e.title == "Weekly review") == 2)
check("Bantu's own events are untouched by a sync", fcal.get(mine.id) is not None)
fcal.fetch = FakeFeed([FeedError("the calendar could not be reached (timed out).")])
check("a failed sync raises", raises(lambda: fcal.sync_feed("https://example/basic.ics"), FeedError))
check("...keeps the last good copy", sum(1 for e in fcal.between(0, 10**11) if e.source == "feed") == 4)
check("...and the status shows why", "Last update failed: the calendar could not be reached" in fcal.feed_status(),
      fcal.feed_status())
fcal.sync_feed(None)
check("disconnecting removes the copied events and the status",
      not any(e.source == "feed" for e in fcal.between(0, 10**11)) and fcal.feed_status() == "No Google Calendar connected."
      and fcal.get(mine.id) is not None)
check("check_feed describes what it found", check_feed("https://x/basic.ics", fetch=FakeFeed([ICS.encode()]))
      == (True, "Found 'Harsh' with 4 events in the next 60 days."))
ok, message = check_feed("https://x/basic.ics", fetch=FakeFeed([FeedError("the calendar address was refused.")]))
check("check_feed explains a refusal, capitalised", not ok and message == "The calendar address was refused.", message)
import core.events as events_module


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n):
        return b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"


fetched = []
real_urlopen = events_module.urllib.request.urlopen
events_module.urllib.request.urlopen = lambda request, timeout=0: (fetched.append(request.full_url), _Response())[1]
try:
    fetch_feed("webcal://calendar.google.com/calendar/ical/x/basic.ics")
finally:
    events_module.urllib.request.urlopen = real_urlopen
check("a webcal:// address is fetched over https", fetched == ["https://calendar.google.com/calendar/ical/x/basic.ics"],
      str(fetched))

print("\n[event alerts]")
alert_mem = Memory(Path(tempfile.mkdtemp()) / "alerts.db")
acal = Calendar(alert_mem.db)
now = datetime.datetime.now().timestamp()
soon = acal.add("Call with Ram", now + 8 * 60, location="Zoom")
later = acal.add("Dentist", now + 50 * 60)
acal.add("All-day thing", datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp(),
         datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp() + 86400, all_day=True)
acal.add("Already started", now - 60)
check("alerts are off at 0 minutes", acal.due_alerts(0) == [])
check("an event starting within the window is due, others are not",
      [e.title for e in acal.due_alerts(10)] == ["Call with Ram"], str([e.title for e in acal.due_alerts(10)]))
said = []
from core.reminders import ReminderScheduler

minutes = {"value": 10}
sched = ReminderScheduler(alert_mem, lambda t, b: said.append((t, b)), calendar=acal, alert_minutes=lambda: minutes["value"])
sched.check_now()
check("the scheduler announces it, with time, place and how soon",
      len(said) == 1 and said[0][0] == "Coming up" and "Call with Ram at" in said[0][1] and "Zoom" in said[0][1]
      and "(in 8 min)" in said[0][1], str(said))
sched.check_now()
check("an event is announced once", len(said) == 1)
minutes["value"] = 0
acal.add("Another soon", now + 3 * 60)
sched.check_now()
check("turning alerts off in Settings stops them at once", len(said) == 1)
minutes["value"] = 60
sched.check_now()
check("widening the window catches the later event, not the one already announced",
      [b.split(" at ")[0] for _, b in said[1:]] == ["Another soon", "Dentist"] or
      sorted(b.split(" at ")[0] for _, b in said[1:]) == ["Another soon", "Dentist"], str(said))
silent = ReminderScheduler(alert_mem, lambda t, b: None)
check("a scheduler with no calendar still runs", silent.check_now() == 0)

print("\n[calendar tools]")
tool_mem = Memory(Path(tempfile.mkdtemp()) / "tools.db")
tcal = Calendar(tool_mem.db)
reg = ToolRegistry()
register_calendar(reg, tcal)
check("calendar tools run without asking", all(reg.tools[n].tier.value == "auto"
                                                for n in ("add_event", "list_events", "cancel_event")))
target = datetime.date.today() + datetime.timedelta(days=2)
added = reg.execute(ToolCall("a", "add_event", {"title": "Call with Ram", "start": f"{target}T15:00",
                                               "end": f"{target}T15:30", "location": "Zoom"}))
check("add_event adds a timed event", added.startswith("Added #") and "15:00-15:30" in added and "Zoom" in added, added)
allday = reg.execute(ToolCall("a", "add_event", {"title": "Holiday", "start": str(target)}))
check("a bare date is an all-day event", "all day" in allday, allday)
span = reg.execute(ToolCall("a", "add_event", {"title": "Conference", "start": str(target),
                                              "end": str(target + datetime.timedelta(days=1))}))
check("an all-day event ending on a day includes that day", f"to {(target + datetime.timedelta(days=1)):%a %d %b}" in span, span)
past = reg.execute(ToolCall("a", "add_event", {"title": "Oops", "start": f"{datetime.date.today() - datetime.timedelta(days=2)}T09:00"}))
check("an event in the past is added with a warning to check it", "Warning: that is already in the past" in past, past)
check("an unreadable start says how to fix it", reg.execute(ToolCall("a", "add_event", {"title": "x", "start": "next Tuesday"}))
      .startswith("Error: could not read"))
listed = reg.execute(ToolCall("l", "list_events", {"start": str(target), "end": str(target)}))
check("list_events lists a day", "Call with Ram" in listed and "Holiday" in listed and "Oops" not in listed, listed)
check("list_events defaults to the coming week", "Call with Ram" in reg.execute(ToolCall("l", "list_events", {})))
empty = reg.execute(ToolCall("l", "list_events", {"start": "2030-01-01", "end": "2030-01-02"}))
check("an empty range says so and whether Google Calendar is connected",
      empty.startswith("Nothing on the calendar") and "No Google Calendar connected" in empty, empty)
check("dates in the wrong order are an error",
      reg.execute(ToolCall("l", "list_events", {"start": "2030-01-05", "end": "2030-01-01"})).startswith("Error"))
event_id = int(added.split("#")[1].split(" ")[0])
check("cancel_event removes Bantu's own event", reg.execute(ToolCall("c", "cancel_event", {"event_id": event_id}))
      .startswith("Cancelled"))
check("cancel_event on a missing event is an error", reg.execute(ToolCall("c", "cancel_event", {"event_id": 999}))
      .startswith("Error: there is no event"))

lazy = ToolRegistry()
register_calendar(lazy, tcal)
lazy.enable_lazy_loading(base={"core"})
catalog = next(sp for sp in lazy.specs() if sp.name == "load_tools").description
check("calendar tools load on demand, with a catalog line naming meetings and Google Calendar",
      "calendar:" in catalog and "meetings" in catalog and "Google Calendar" in catalog, catalog)

brief = build_briefing(tool_mem, Records(tool_mem.db), None,
                       lambda day: [e.short() for e in tcal.on_day(day)], datetime.datetime.combine(target, datetime.time(8)))
check("the briefing lists the day's events", "Calendar today: all day: Conference; all day: Holiday" in brief
      or "Calendar today: all day: Holiday; all day: Conference" in brief, brief)

print("\n[calendar in Settings]")
from core.providers.validate import KeyCheck

stored = {}
forgotten = []


def fake_check(provider, key):
    if provider == "calendar":
        return KeyCheck(key.endswith("basic.ics"), "Found 'Harsh' with 3 events in the next 60 days."
                        if key.endswith("basic.ics") else "That is not a web address.")
    return KeyCheck(True, "ok")


cal_services = Services(check_key=fake_check, store_key=lambda p, k: stored.__setitem__(p, k),
                        existing_key=lambda p: ("gsk_existing", "keyring") if p != "calendar" else (None, None),
                        list_devices=lambda: [], preview_voice=lambda s, g, t: None, open_url=lambda u: None,
                        forget_key=forgotten.append)
settings = cfg.Settings(username="Harsh")
dlg = SettingsDialog(settings, cal_services, calendar=fcal)
check("the Keys tab has an optional Google Calendar field with a 'where to find it' link",
      dlg.calendar_field.provider == "calendar" and dlg.calendar_status.text() == "No Google Calendar connected.")
changes = []
dlg.applied.connect(changes.append)
dlg.calendar_field.edit.setText("https://calendar.google.com/calendar/ical/x/private-y/basic.ics")
dlg.save()
check("an untested address cannot be saved", "Test the new Google Calendar address" in dlg.error.text() and not stored,
      dlg.error.text())
dlg.calendar_field.test()
pump(0.5)
dlg.save()
check("a tested address is stored with the keys and applied", stored.get("calendar", "").endswith("basic.ics")
      and changes and "calendar" in changes[-1] and "keys" not in changes[-1], str(changes))

connected = SettingsDialog(cfg.Settings(), Services(
    check_key=fake_check, store_key=lambda p, k: None,
    existing_key=lambda p: ("https://calendar.google.com/x/basic.ics", "keyring") if p == "calendar" else ("gsk", "keyring"),
    list_devices=lambda: [], preview_voice=lambda s, g, t: None, open_url=lambda u: None, forget_key=forgotten.append))
changes2 = []
connected.applied.connect(changes2.append)
connected.calendar_field.edit.setText("")
connected.save()
check("clearing a stored address disconnects it", forgotten == ["calendar"] and changes2 and "calendar" in changes2[-1],
      f"{forgotten} {changes2}")

alerts = SettingsDialog(settings, cal_services)
check("event alerts default to off", alerts.event_alert.currentData() == 0)
alerts.event_alert.setCurrentIndex(alerts.event_alert.findData(10))
alerts.save()
check("choosing an alert time saves it", settings.event_alert_minutes == 10)

main_src = (ROOT / "main.py").read_text(encoding="utf-8")
check("the app polls the feed from the credential store, briefs today's events and alerts before events",
      'cfg.get_key("calendar")' in main_src and "events_for=" in main_src and "calendar=_CALENDAR" in main_src
      and 'alert_minutes=lambda: getattr(settings, "event_alert_minutes", 0)' in main_src)
requirements = (ROOT / "Requirements.txt").read_text(encoding="utf-8")
check("the calendar libraries are in Requirements.txt", "icalendar" in requirements and "recurring-ical-events" in requirements)


if __name__ == "__main__":
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for f in FAIL:
        print("  FAILED:", f)
    sys.exit(1 if FAIL else 0)
