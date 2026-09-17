"""R2 tests: structured records, notes tools, honest recall, follow-ups, Devanagari search.

No API key, no network.

    .venv/Scripts/python.exe tests/test_r2_memory.py
"""

from __future__ import annotations

import datetime
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from core.agent import SYSTEM_TEMPLATE
from core.memory import Memory, _fts_query
from core.providers.base import Message, ToolCall
from core.records import KINDS, Records, clean_people
from core.reminders import ReminderScheduler
from core.tools import builtin, notes
from core.tools.registry import Tier, ToolRegistry

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  -> {detail}" if detail and not cond else ""))


def fresh():
    memory = Memory(Path(tempfile.mkdtemp()) / "r2.db")
    return memory, Records(memory.db)


def raises(fn, exc=ValueError):
    try:
        fn()
        return False
    except exc:
        return True


def day(offset_days: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=offset_days)).isoformat()


# --- records ------------------------------------------------------------------

print("\n[records]")
memory, records = fresh()
promise = records.add("commitment", "Send Ram the vendor report", "Ram", time.time() + 86400)
check("a commitment starts open", promise.status == "open" and promise.id == 1)
decision = records.add("decision", "Launch moves back one week", ["Product team", "marketing"])
check("a decision has no status to finish", decision.status is None)
check("people are kept as a list", decision.people == ["Product team", "marketing"])
check("names are split, trimmed and de-duplicated in any case",
      clean_people(" Ram, sita ;  RAM ,, ") == ["Ram", "sita"])
check("an unknown kind is refused", raises(lambda: records.add("gossip", "x")))
check("empty text is refused", raises(lambda: records.add("note", "   ")))
check("an unknown priority is refused", raises(lambda: records.add("task", "x", priority="urgent!!")))
check("'action item' with a space is accepted", records.add("action item", "Marketing to revise the timeline").kind
      == "action_item")
check("all six kinds exist", set(KINDS) == {"commitment", "decision", "action_item", "task", "note", "person"})

check("search finds by words", [r.id for r in records.find("vendor report")] == [promise.id])
check("search finds by person, any case", [r.id for r in records.find(person="ram")] == [promise.id])
check("search finds a name through the full-text index too", promise.id in [r.id for r in records.find("Ram")])
check("filters combine", records.find(kind="decision", person="marketing")[0].id == decision.id)
check("nothing matching returns nothing, not everything", records.find("budget") == [])
check("punctuation-only queries return nothing rather than erroring", records.find("?!") == [])

soon = records.add("task", "Renew the domain", due_at=time.time() + 3600)
later = records.add("task", "File the quarterly taxes", due_at=time.time() + 30 * 86400)
undated = records.add("task", "Tidy the desktop")
open_ids = [r.id for r in records.find(kind="task", status="open")]
check("open work lists soonest due first, undated last", open_ids == [soon.id, later.id, undated.id], str(open_ids))

memory.db.execute("UPDATE records SET created_at = created_at - ? WHERE id = ?", (40 * 86400, later.id))
memory.db.commit()
recent = [r.id for r in records.find(kind="task", since=time.time() - 7 * 86400)]
check("since filters by when a record was noted", later.id not in recent and soon.id in recent, str(recent))
check("until filters by when a record was noted",
      [r.id for r in records.find(kind="task", until=time.time() - 30 * 86400)] == [later.id])

done = records.set_status(soon.id, "done")
check("marking done records when", done.status == "done" and done.done_at is not None)
check("reopening clears the done time", records.set_status(soon.id, "open").done_at is None)
check("a decision cannot be marked done", raises(lambda: records.set_status(decision.id, "done")))
check("an unknown record is a KeyError", raises(lambda: records.set_status(999, "done"), KeyError))
check("an unknown status is refused", raises(lambda: records.set_status(soon.id, "finished")))
check("delete removes a record", records.delete(undated.id) and records.get(undated.id) is None)
check("...and the search index forgets it too", records.find("desktop") == [])
check("deleting twice reports nothing deleted", not records.delete(undated.id))

line = promise.describe()
check("a description says when it was noted", "noted " in line and "due " in line and "Ram" in line, line)
past = records.add("commitment", "Call the bank", due_at=time.time() - 3600)
check("an overdue item says so", "OVERDUE" in past.describe(), past.describe())

hindi = records.add("commitment", "सीता को सोमवार तक बजट भेजना है", "सीता")
check("Hindi records are found by a Hindi word", [r.id for r in records.find("बजट")] == [hindi.id])
check("...and by a Hindi name", hindi.id in [r.id for r in records.find(person="सीता")])
nepali = records.add("task", "भोलि बिहान बैंकमा फोन गर्नुपर्छ")
check("Nepali records are found by a Nepali word", [r.id for r in records.find("बैंकमा")] == [nepali.id])


# --- follow-ups ---------------------------------------------------------------

print("\n[overdue follow-up]")
memory, records = fresh()
late = records.add("commitment", "Send Ram the vendor report", "Ram", due_at=time.time() - 60)
records.add("commitment", "Pay the electricity bill", due_at=time.time() + 86400)
finished = records.add("task", "Old finished task", due_at=time.time() - 3600)
records.set_status(finished.id, "done")
memory.add_reminder("Stand up and stretch", time.time() - 5)
said = []
scheduler = ReminderScheduler(memory, lambda title, body: said.append((title, body)), records=records)
check("a tick announces the due reminder and the overdue promise", scheduler.check_now() == 2, str(said))
check("the reminder still fires as before", ("Reminder", "Stand up and stretch") in said)
overdue = [b for t, b in said if t == "Overdue"]
check("the follow-up names the work, the person and when it was due",
      len(overdue) == 1 and "vendor report" in overdue[0] and "Ram" in overdue[0] and "was due" in overdue[0],
      str(overdue))
check("done work and future work are not followed up", len(said) == 2)
said.clear()
check("a follow-up is said once, not every tick", scheduler.check_now() == 0 and said == [])
check("...but the item stays open until the user closes it", records.get(late.id).status == "open")
silent = ReminderScheduler(memory, lambda t, b: None)
check("a scheduler without records still works for reminders", silent.check_now() == 0)


def broken(title, body):
    raise RuntimeError("toast failed")


memory, records = fresh()
item = records.add("task", "Renew passport", due_at=time.time() - 60)
ReminderScheduler(memory, broken, records=records).check_now()
check("a failed announcement still marks the follow-up, so it does not repeat forever",
      records.overdue_unannounced() == [])


# --- notes tools ----------------------------------------------------------------

print("\n[notes tools]")
memory, records = fresh()
reg = ToolRegistry()
notes.register(reg, records, memory)


def run(name, **args):
    return reg.execute(ToolCall("t", name, args), confirm=lambda t, a: True)


check("noting, finding and updating run without asking",
      all(reg.tools[n].tier is Tier.AUTO for n in ("note", "find_notes", "update_note")))
check("deleting a record asks first", reg.tools["delete_note"].tier is Tier.CONFIRM)
check("kind is an enum the provider validates", reg.tools["note"].parameters["properties"]["kind"].get("enum")
      == list(KINDS))

friday = day(2)
out = run("note", kind="commitment", text="Send Ram the vendor report", people="Ram", due=friday)
stored = records.find(person="Ram")[0]
due_dt = datetime.datetime.fromtimestamp(stored.due_at)
check("a date-only due date means the end of that day",
      due_dt.date().isoformat() == friday and (due_dt.hour, due_dt.minute) == (23, 59), str(due_dt))
check("...and reads as a day, not 23:59", out.startswith("Noted #") and "23:59" not in out, out)
check("the record remembers which conversation it came from", stored.conversation == memory.conversation)
timed = run("note", kind="task", text="Call the plumber", due=f"{day(1)}T17:30")
check("a due time is kept and shown", "17:30" in timed, timed)
check("a vague due date is refused with how to fix it",
      run("note", kind="task", text="x", due="next week sometime").startswith("Error") and "get_datetime" in
      run("note", kind="task", text="x", due="next week sometime"))
check("a bad kind comes back as an error the model can read",
      run("note", kind="rumour", text="x").startswith("Error"))

nothing = run("find_notes", query="office move")
check("finding nothing says there is no record, and not to guess",
      nothing.startswith("No records") and "rather than guessing" in nothing, nothing)
found = run("find_notes", person="ram", status="open")
check("finding something lists it with its date", "vendor report" in found and "noted" in found, found)
check("since excludes older records", "No records" in run("find_notes", person="Ram", since=day(1)))
check("until includes today's records", "vendor report" in run("find_notes", person="Ram", until=day(0)))
check("an unreadable since date is an error", run("find_notes", since="last spring").startswith("Error"))
check("an unknown status filter is an error", run("find_notes", status="pending").startswith("Error"))

rid = stored.id
check("update_note marks done", "done" in run("update_note", note_id=rid, status="done"))
check("update_note refuses an unknown status", run("update_note", note_id=rid, status="finished").startswith("Error"))
check("update_note with nothing to change is an error", run("update_note", note_id=rid).startswith("Error"))

# Found live: asked for "this Friday" on a Thursday, gpt-oss-20b noted the day before.
wrong = run("note", kind="commitment", text="Send Sita the budget", people="Sita", due=day(-1))
check("a due date already past is noted, with a warning to check it",
      "Warning: that due date is already past" in wrong and "update_note" in wrong, wrong)
check("a due date today or later carries no warning", "Warning" not in run("note", kind="task", text="t", due=day(0)))
sita = records.find(person="Sita")[0]
records.mark_followed_up(sita.id)
fixed = run("update_note", note_id=sita.id, due=day(1))
check("update_note corrects a due date", "Warning" not in fixed and records.find(person="Sita")[0].due_at > time.time(),
      fixed)
check("...and a moved date can be followed up again when it passes",
      memory.db.execute("SELECT followed_up_at FROM records WHERE id=?", (sita.id,)).fetchone()[0] is None)
now_text = ToolRegistry()
builtin.register(now_text, memory)
said_now = now_text.execute(ToolCall("d", "get_datetime", {}))
check("get_datetime lists the coming seven days, so no model does weekday arithmetic",
      "Next 7 days:" in said_now and day(1) in said_now and day(7) in said_now and day(8) not in said_now, said_now)
check("update_note on a missing id is an error", run("update_note", note_id=404, status="done").startswith("Error"))
check("delete_note removes it", run("delete_note", note_id=rid).startswith("Deleted") and records.get(rid) is None)
declined = reg.execute(ToolCall("t", "delete_note", {"note_id": 2}), confirm=lambda t, a: False)
check("a declined delete keeps the record", records.get(2) is not None and "said no" in declined, declined)

lazy = ToolRegistry()
builtin.register(lazy, memory)
notes.register(lazy, records, memory)
lazy.enable_lazy_loading(base={"core"})
up_front = [s.name for s in lazy.specs()]
catalog = next(s for s in lazy.specs() if s.name == "load_tools").description
check("notes tools are not sent up front", "note" not in up_front and "find_notes" not in up_front, str(up_front))
check("the catalog tells the model what the notes group is for",
      "notes:" in catalog and "promises" in catalog and "deadlines" in catalog, catalog)


# --- honest recall --------------------------------------------------------------

print("\n[honest recall]")
flat = " ".join(SYSTEM_TEMPLATE.split())
check("the prompt says to note promises, decisions, tasks and deadlines",
      "makes a promise, reaches a decision, takes on a task or mentions a deadline, note it" in flat)
check("the prompt says to answer about the past only from what is on record, with when",
      "only from notes, past conversations or remembered facts" in flat and "say when it happened" in flat)
check("...and to say there is no record rather than guess", "say there is no record of it rather than guessing" in flat)

memory, records = fresh()
reg = ToolRegistry()
builtin.register(reg, memory)
memory.append(Message.user("We agreed the vendor contract waits until October."))
memory.append(Message.assistant("Got it, the contract waits until October."))
call = ToolCall("c", "peek", {})
memory.append(Message.assistant(None, [call]))
memory.append(Message.tool_result(call, "vendor contract data from a tool"))
out = reg.execute(ToolCall("s", "search_history", {"query": "vendor contract"}))
check("history search says when each thing was said", datetime.date.today().strftime("%d %b") in out, out)
check("...and who said it", "user said:" in out and "you replied:" in out, out)
check("...and leaves out tool output, which nobody said", "from a tool" not in out, out)
none = reg.execute(ToolCall("s", "search_history", {"query": "office move"}))
check("history search finding nothing says there is no record", "no record" in none, none)


print("\n[replies never show leaked tool-call markup]")
from core.agent import clean_reply

# Found live: qwen, offered no tools for a closing reply, wrote its native call format as text.
leak = "<tool_call>\n<function=save_note>\n<parameter=content>\nI sent Ram the report\n</parameter>\n</function>\n</tool_call>"
check("a reply that is only leaked markup becomes empty, so the fallback is used", clean_reply(leak) == "")
check("markup inside a reply is removed and the words kept",
      clean_reply("Done. <function=x><parameter=a>1</parameter></function> Anything else?") == "Done. Anything else?")
check("ordinary angle brackets and spacing are left alone",
      clean_reply("Use x < y  here") == "Use x < y  here")
tool_descriptions = " ".join(t.description + str(t.parameters) for t in reg.tools.values())
check("tool descriptions carry no Devanagari that could pull an English reply into Hindi",
      not any("\u0900" <= c <= "\u097f" for c in tool_descriptions))


# --- Devanagari search (a bug that shipped) ---------------------------------------

print("\n[Hindi and Nepali search]")
# Python's isalnum() rejects Devanagari vowel signs, so the query builder cut
# "रिपोर्ट" into fragments and dropped them: every Hindi or Nepali recall was empty.
check("the query keeps whole Devanagari words", _fts_query("रिपोर्ट भेजनी है") == '"रिपोर्ट" OR "भेजनी" OR "है"',
      _fts_query("रिपोर्ट भेजनी है"))
check("English queries are unchanged", _fts_query("vendor report?") == '"vendor" OR "report"')
memory, records = fresh()
memory.remember("हर्ष को शुक्रवार तक रिपोर्ट भेजनी है")
memory.remember("मेरो बैंक खाता नबिल बैंकमा छ")
check("a Hindi fact is recalled by a Hindi word", memory.recall("रिपोर्ट") == ["हर्ष को शुक्रवार तक रिपोर्ट भेजनी है"])
check("a Nepali fact is recalled by a Nepali word", memory.recall("बैंकमा") == ["मेरो बैंक खाता नबिल बैंकमा छ"])
memory.append(Message.user("राम को रिपोर्ट भेजनी है"))
check("a Hindi message is found in history", memory.search_messages("रिपोर्ट") == ["राम को रिपोर्ट भेजनी है"])

# SQLite's default tokenizer splits Devanagari at every vowel sign: "बैंकमा" was
# indexed as ब / कम, so a search for कम ("less") matched "in the bank".
check("whole words only: कम does not match बैंकमा in facts", memory.recall("कम") == [])
records.add("note", "मेरो बैंक खाता नबिल बैंकमा छ")
check("...nor in records", records.find("कम") == [] and len(records.find("बैंकमा")) == 1)
check("Latin accents still fold: cafe finds café", memory.remember("Harsh likes café au lait")
      and memory.recall("cafe") == ["Harsh likes café au lait"])

import sqlite3

old_path = Path(tempfile.mkdtemp()) / "old.db"
old = sqlite3.connect(str(old_path))
old.executescript("""
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_calls TEXT, tool_call_id TEXT, name TEXT, created_at REAL NOT NULL);
CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL UNIQUE, created_at REAL NOT NULL);
CREATE VIRTUAL TABLE facts_fts USING fts5(text, content='facts', content_rowid='id');
CREATE TRIGGER facts_ai AFTER INSERT ON facts BEGIN INSERT INTO facts_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='id');
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content); END;
""")
old.execute("INSERT INTO facts (text, created_at) VALUES ('मेरो बैंक खाता नबिल बैंकमा छ', 1)")
old.execute("INSERT INTO messages (conversation, role, content, created_at) VALUES ('c1', 'user', 'राम को रिपोर्ट भेजनी है', 1)")
old.commit()
check("an old database really had the fragment bug", old.execute(
    "SELECT count(*) FROM facts_fts WHERE facts_fts MATCH '\"कम\"'").fetchone()[0] == 1)
old.close()
upgraded = Memory(old_path)
fts_sql = {r["name"]: r["sql"] for r in upgraded.db.execute(
    "SELECT name, sql FROM sqlite_master WHERE name IN ('facts_fts', 'messages_fts')")}
check("opening an old database rebuilds its search indexes with the new tokenizer",
      all("tokenchars" in sql for sql in fts_sql.values()) and len(fts_sql) == 2, str(fts_sql))
check("...keeping every fact and message", upgraded.all_facts() == ["मेरो बैंक खाता नबिल बैंकमा छ"]
      and upgraded.search_messages("रिपोर्ट") == ["राम को रिपोर्ट भेजनी है"])
check("...and the fragment match is gone", upgraded.recall("कम") == [] and upgraded.recall("बैंकमा"))
upgraded.remember("नयाँ तथ्य पनि खोजिन्छ")
check("...and new rows are indexed through the old triggers", upgraded.recall("खोजिन्छ") == ["नयाँ तथ्य पनि खोजिन्छ"])
upgraded.close()
again = Memory(old_path)
check("a database already upgraded is left alone", again.recall("बैंकमा") and again.recall("खोजिन्छ"))

main_src = (ROOT / "main.py").read_text(encoding="utf-8")
check("the app registers the notes tools and follows up overdue work",
      "notes.register(_REGISTRY, records, memory)" in main_src and "records=records" in main_src)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
for f in FAIL:
    print("  FAILED:", f)
sys.exit(1 if FAIL else 0)
