"""Speak money with cents, number ranges and slash dates the way a reader would.

WHY (#1546). Two blind listening rounds on the narrator voice found three
written conventions the model reads wrongly most of the time: slash dates 6 of
6 ("seven four slash 1776"), dash ranges 5 of 7 ("three dash five"), dollars
with cents 4 of 6 ("zero point 75 dollars"). Plain integers, years, ordinals,
Roman numerals, pounds and percentages read correctly, so this only reshapes
the three conventions and leaves digits as digits.

WHAT IT MUST NOT TOUCH. In 29 saved books the only digit-dash-digit text was
ISBNs and product codes (978-0-316-31530-2, E3-20180216-JV-PC). Every pattern
here refuses a match that is part of a longer chain or code, so a range rule
cannot turn an ISBN into "316 to 31530".

DATE ORDER is a property of the book, not of one line: 7/4/1776 is the 4th of
July in an American book and the 7th of April in a British one. A line that
names a day over 12 settles itself; an ambiguous one needs the book's order,
which comes from the reader's choice, else from the book's own unambiguous
dates, else from its spelling (colour/color). With none of those, the date is
left as written and recorded, never guessed.
"""
import calendar
import json
import os
import re
import threading

MONTHS = [None, "January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
DATE_ORDERS = ("mdy", "dmy")
DATE_ORDER_CHOICES = ("auto", "mdy", "dmy")
STATE_KEY = "speech_date_order"

# A neighbour that would make the match part of a code, chain or decimal. A
# following period or comma only counts when a digit follows it: "1890-1902."
# ends a sentence, "1.2-3.4" does not.
_BEFORE = r"(?<![\w./\-–])"
_AFTER = r"(?![\w/\-–]|[.,]\d)"
_MONEY_RE = re.compile(r"(?<![\w$])\$(\d{1,3}(?:,\d{3})+|\d+)\.(\d{2})" + _AFTER)
_RANGE_RE = re.compile(_BEFORE + r"(\d+)[-–](\d+)" + _AFTER)
_DATE_RE = re.compile(_BEFORE + r"(\d{1,2})/(\d{1,2})/(\d{4})" + _AFTER)

# British and American spellings of the same words, for where the book was written.
_SPELLING_PAIRS = [("colour", "color"), ("honour", "honor"), ("favour", "favor"),
                   ("neighbour", "neighbor"), ("labour", "labor"), ("harbour", "harbor"),
                   ("centre", "center"), ("theatre", "theater"), ("grey", "gray"),
                   ("defence", "defense"), ("travelled", "traveled")]
_UK_RE = re.compile(r"\b(?:" + "|".join(uk for uk, _ in _SPELLING_PAIRS) + r")s?\b", re.IGNORECASE)
_US_RE = re.compile(r"\b(?:" + "|".join(us for _, us in _SPELLING_PAIRS) + r")s?\b", re.IGNORECASE)
MIN_SPELLING_HITS = 8
MIN_SPELLING_SHARE = 0.8


def _ordinal(day):
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _is_date(month, day, year):
    return 1 <= month <= 12 and 1 <= day <= calendar.monthrange(year, month)[1]


def get_date_reading(first, second, year, date_order):
    """-> (month, day) for a slash date, or None when it is not a date or needs an order."""
    as_mdy = _is_date(first, second, year)
    as_dmy = _is_date(second, first, year)
    if as_mdy and as_dmy and first != second:
        if date_order == "mdy":
            return first, second
        if date_order == "dmy":
            return second, first
        return None
    if as_mdy:
        return first, second
    if as_dmy:
        return second, first
    return None


def _spoken_money(dollars, cents):
    whole = int(dollars.replace(",", ""))
    cents = int(cents)
    cent_words = f"{cents} cent" + ("" if cents == 1 else "s")
    if whole == 0:
        return cent_words
    dollar_words = f"{dollars} dollar" + ("" if whole == 1 else "s")
    return dollar_words if cents == 0 else f"{dollar_words} and {cent_words}"


# A hyphen between two rising numbers is a range only when a word beside it says
# so: "pages 3-5", "from 1890-1902", "3-5 pm". Without one it is as likely a code
# or an operation ("the 737-800", "a 1-2 pitch", "subtract 3-5"), so it is left as
# written. An en dash is the typographic range mark and needs no such word.
_RANGE_WORDS_BEFORE = {
    "from", "between", "pages", "page", "pp", "chapters", "chapter", "verses", "verse",
    "lines", "questions", "items", "steps", "sections", "parts", "volumes", "vols",
    "episodes", "rounds", "levels", "rooms", "nos", "numbers", "ages", "aged",
    "years", "grades", "weeks", "days", "runs", "ran", "about", "roughly", "some"}
_RANGE_WORDS_AFTER = {
    "am", "pm", "a.m.", "p.m.", "o'clock", "seconds", "minutes", "hours", "days",
    "weeks", "months", "years", "miles", "feet", "inches", "yards", "metres",
    "meters", "kilometres", "kilometers", "pounds", "kilos", "percent", "%",
    "people", "men", "women", "times", "players", "points"}
_WORD_BEFORE_RE = re.compile(r"([A-Za-z.']+)\s*$")
_WORD_AFTER_RE = re.compile(r"^\s*(%|[A-Za-z.']+)")


def _has_range_context(match):
    if "–" in match.group(0):
        return True
    before = _WORD_BEFORE_RE.search(match.string[:match.start()])
    after = _WORD_AFTER_RE.search(match.string[match.end():])
    return bool(before and before.group(1).lower().strip(".") in _RANGE_WORDS_BEFORE
                or after and (after.group(1).lower() in _RANGE_WORDS_AFTER
                              or after.group(1).lower().rstrip(".,;:") in _RANGE_WORDS_AFTER))


def _is_range(first, second):
    if (first.startswith("0") and first != "0") or second.startswith("0"):
        return False
    if int(second) <= int(first) or len(second) > len(first) + 1:
        return False
    return not (len(first) == 3 and len(second) == 4)        # 555-1234


def verbalize_numbers(text, date_order=None):
    """-> (text, transformations). Only the three conventions; everything else unchanged."""
    changes, ambiguous = [], 0

    def money(match):
        spoken = _spoken_money(match.group(1), match.group(2))
        changes.append({"kind": "money", "source": match.group(0), "spoken": spoken})
        return spoken

    def date(match):
        nonlocal ambiguous
        first, second, year = (int(value) for value in match.groups())
        reading = get_date_reading(first, second, year, date_order)
        if reading is None:
            if _is_date(first, second, year) or _is_date(second, first, year):
                ambiguous += 1
            return match.group(0)
        month, day = reading
        spoken = f"the {_ordinal(day)} of {MONTHS[month]}, {year}"
        changes.append({"kind": "date", "source": match.group(0), "spoken": spoken})
        return spoken

    def number_range(match):
        first, second = match.groups()
        if not _is_range(first, second) or not _has_range_context(match):
            return match.group(0)
        spoken = f"{first} to {second}"
        changes.append({"kind": "range", "source": match.group(0), "spoken": spoken})
        return spoken

    value = _MONEY_RE.sub(money, text)
    value = _DATE_RE.sub(date, value)
    value = _RANGE_RE.sub(number_range, value)
    transformations = []
    if changes:
        transformations.append({"type": "verbalized_numbers", "changes": changes})
    if ambiguous:
        transformations.append({"type": "ambiguous_date", "count": ambiguous,
                                "date_order": date_order})
    return value, transformations


def get_date_order_evidence(text):
    """-> {order, source, detail}: the book's own dates first, then its spelling."""
    votes = {"mdy": 0, "dmy": 0}
    ambiguous = 0
    for match in _DATE_RE.finditer(text or ""):
        first, second, year = (int(value) for value in match.groups())
        as_mdy, as_dmy = _is_date(first, second, year), _is_date(second, first, year)
        if as_mdy and not as_dmy:
            votes["mdy"] += 1
        elif as_dmy and not as_mdy:
            votes["dmy"] += 1
        elif as_mdy and first != second:
            ambiguous += 1
    detail = {"date_votes": votes, "ambiguous_dates": ambiguous}
    if votes["mdy"] and not votes["dmy"]:
        return {"order": "mdy", "source": "book_dates", "detail": detail}
    if votes["dmy"] and not votes["mdy"]:
        return {"order": "dmy", "source": "book_dates", "detail": detail}
    uk, us = len(_UK_RE.findall(text or "")), len(_US_RE.findall(text or ""))
    detail["spelling"] = {"uk": uk, "us": us}
    if not (votes["mdy"] and votes["dmy"]) and uk + us >= MIN_SPELLING_HITS:
        if uk / (uk + us) >= MIN_SPELLING_SHARE:
            return {"order": "dmy", "source": "spelling", "detail": detail}
        if us / (uk + us) >= MIN_SPELLING_SHARE:
            return {"order": "mdy", "source": "spelling", "detail": detail}
    return {"order": None, "source": "undetermined", "detail": detail}


def get_script_text(entries):
    return "\n".join(str(entry.get("text", "")) for entry in entries
                     if isinstance(entry, dict)) if isinstance(entries, list) else ""


def get_data_dir():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.abspath(os.environ.get("ALEXANDRIA_DATA_DIR", "").strip() or root)


def _fingerprint(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


_cache = {"key": None, "value": None}
_cache_lock = threading.Lock()


def get_book_date_order_status(data_dir=None):
    """-> {choice, order, evidence} for the active book; read-only, cached on file identity.

    Every speech preparation reads this one answer, so render, publication and
    review cannot disagree about how a date was prepared.
    """
    data_dir = data_dir or get_data_dir()
    state_path = os.path.join(data_dir, "state.json")
    script_path = os.path.join(data_dir, "annotated_script.json")
    key = (data_dir, _fingerprint(state_path), _fingerprint(script_path))
    with _cache_lock:
        if _cache["key"] == key:
            return dict(_cache["value"])
    state = _read_json(state_path)
    choice = state.get(STATE_KEY, "auto") if isinstance(state, dict) else "auto"
    if choice not in DATE_ORDER_CHOICES:
        choice = "auto"
    evidence = get_date_order_evidence(get_script_text(_read_json(script_path)))
    order = choice if choice in DATE_ORDERS else evidence["order"]
    value = {"choice": choice, "order": order, "evidence": evidence}
    with _cache_lock:
        _cache.update(key=key, value=value)
    return dict(value)


def get_book_date_order(data_dir=None):
    return get_book_date_order_status(data_dir)["order"]
