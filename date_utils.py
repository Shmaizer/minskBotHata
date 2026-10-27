"""Publication-date parsing and filtering in Minsk local time.

The bot deliberately treats a missing publication timestamp as unknown and does not
send the listing. This prevents old cards from being reported as fresh merely because
they appear on the current search page.
"""
from __future__ import annotations

import re
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

MINSK_TZ = ZoneInfo("Europe/Minsk")

RU_MONTHS = {
    "января": 1, "янв": 1,
    "февраля": 2, "фев": 2,
    "марта": 3, "мар": 3,
    "апреля": 4, "апр": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июн": 6,
    "июля": 7, "июл": 7,
    "августа": 8, "авг": 8,
    "сентября": 9, "сент": 9, "сен": 9,
    "октября": 10, "окт": 10,
    "ноября": 11, "нояб": 11, "ноя": 11,
    "декабря": 12, "дек": 12,
}


def now_minsk() -> datetime:
    return datetime.now(MINSK_TZ)


def normalize_datetime(value: datetime, now: Optional[datetime] = None) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=MINSK_TZ)
    return value.astimezone(MINSK_TZ)


def start_of_today(now: Optional[datetime] = None) -> datetime:
    now = normalize_datetime(now or now_minsk())
    return datetime.combine(now.date(), time.min, tzinfo=MINSK_TZ)


def parse_datetime_value(value: str, now: Optional[datetime] = None) -> Optional[datetime]:
    """Parse machine-readable timestamps, absolute Russian dates and clear relative labels."""
    if not value:
        return None
    now = normalize_datetime(now or now_minsk())
    raw = re.sub(r"\s+", " ", str(value)).strip()
    if not raw:
        return None

    # Unix timestamp (seconds or milliseconds), when exposed as an HTML attribute.
    if re.fullmatch(r"\d{10,13}(?:\.\d+)?", raw):
        try:
            n = float(raw)
            if n > 10**12:
                n /= 1000
            return datetime.fromtimestamp(n, MINSK_TZ)
        except (ValueError, OverflowError, OSError):
            pass

    # ISO 8601, including date-only values, UTC Z, and explicit offsets.
    iso = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    if re.match(r"^\d{4}-\d{2}-\d{2}(?:[T ]|$)", iso):
        try:
            return normalize_datetime(datetime.fromisoformat(iso), now)
        except ValueError:
            pass

    # Numeric dates: 27.10.2026, 27/10/2026, 27-10-26; time is optional.
    match = re.search(
        r"(?<!\d)(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})(?:\s*(?:г\.?|года?)?)?"
        r"(?:[, ]+в?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
        raw,
        re.IGNORECASE,
    )
    if match:
        day, month, year = map(int, match.group(1, 2, 3))
        if year < 100:
            year += 2000 if year < 70 else 1900
        hour = int(match.group(4) or 0)
        minute = int(match.group(5) or 0)
        second = int(match.group(6) or 0)
        try:
            return datetime(year, month, day, hour, minute, second, tzinfo=MINSK_TZ)
        except ValueError:
            pass

    # Russian date with month name: 27 октября 2026, 27 окт. в 12:40.
    month_pattern = "|".join(sorted((re.escape(x) for x in RU_MONTHS), key=len, reverse=True))
    match = re.search(
        rf"(?<!\d)(\d{{1,2}})\s+({month_pattern})\.?[,]?\s*(\d{{4}})?"
        rf"(?:\s*(?:г\.?|года?)?)?(?:\s*(?:в|,)\s*(\d{{1,2}}):(\d{{2}})(?::(\d{{2}}))?)?",
        raw.lower(),
        re.IGNORECASE,
    )
    if match:
        day = int(match.group(1))
        month = RU_MONTHS[match.group(2).lower()]
        year = int(match.group(3) or now.year)
        hour = int(match.group(4) or 0)
        minute = int(match.group(5) or 0)
        second = int(match.group(6) or 0)
        try:
            result = datetime(year, month, day, hour, minute, second, tzinfo=MINSK_TZ)
            # A yearless date far in the future is usually from the previous calendar year.
            if not match.group(3) and result.date() > now.date() + timedelta(days=1):
                result = result.replace(year=year - 1)
            return result
        except ValueError:
            pass

    # Relative labels are intended to be passed as the contents of a date/time tag,
    # not as an entire description paragraph.
    lowered = raw.lower().strip(" .,:;|—–-")
    if re.fullmatch(r"(?:только что|сейчас|just now)", lowered):
        return now
    if re.fullmatch(r"сегодня(?:\s+в\s+\d{1,2}:\d{2})?", lowered):
        tm = re.search(r"в\s+(\d{1,2}):(\d{2})", lowered)
        return now.replace(hour=int(tm.group(1)), minute=int(tm.group(2)), second=0, microsecond=0) if tm else now
    if re.fullmatch(r"вчера(?:\s+в\s+\d{1,2}:\d{2})?", lowered):
        tm = re.search(r"в\s+(\d{1,2}):(\d{2})", lowered)
        base = now - timedelta(days=1)
        return base.replace(hour=int(tm.group(1)), minute=int(tm.group(2)), second=0, microsecond=0) if tm else base
    ago = re.fullmatch(r"(\d+)\s*(?:минут(?:у|ы)?|мин\.?|minutes?)\s+назад", lowered)
    if ago:
        return now - timedelta(minutes=int(ago.group(1)))
    ago = re.fullmatch(r"(\d+)\s*(?:час(?:а|ов)?|ч\.?|hours?)\s+назад", lowered)
    if ago:
        return now - timedelta(hours=int(ago.group(1)))
    ago = re.fullmatch(r"(\d+)\s*(?:дн(?:я|ей|и)?\.?|день|days?)\s+назад", lowered)
    if ago:
        return now - timedelta(days=int(ago.group(1)))
    return None


_ABSOLUTE_DATE_PATTERN = re.compile(
    r"(?<!\d)\d{1,2}[./-]\d{1,2}[./-]\d{2,4}(?:\s*(?:г\.?|года?)?)?"
    r"(?:[, ]+в?\s*\d{1,2}:\d{2}(?::\d{2})?)?"
    r"|(?<!\d)\d{1,2}\s+(?:" + "|".join(sorted((re.escape(x) for x in RU_MONTHS), key=len, reverse=True)) + r")\.?[,]?\s*\d{4}(?:\s*(?:г\.?|года?)?)?"
    r"(?:\s*(?:в|,)\s*\d{1,2}:\d{2}(?::\d{2})?)?",
    re.IGNORECASE,
)


def extract_published_at(container, now: Optional[datetime] = None) -> Optional[datetime]:
    """Extract a publication date from one listing card, returning None if unknown."""
    if container is None:
        return None
    now = normalize_datetime(now or now_minsk())

    # 1) Machine-readable date values inside the card.
    for tag in [container, *container.find_all(True)]:
        attr_values = []
        name = (getattr(tag, "name", "") or "").lower()
        attrs = getattr(tag, "attrs", {}) or {}
        if name == "time" and attrs.get("datetime"):
            attr_values.append(attrs.get("datetime"))
        for key in (
            "data-published-at", "data-published", "data-created-at", "data-created",
            "data-posted-at", "data-date", "data-timestamp", "datetime"
        ):
            if attrs.get(key):
                attr_values.append(attrs[key])
        for val in attr_values:
            parsed = parse_datetime_value(str(val), now)
            if parsed:
                return parsed

    # 2) Explicit semantic labels such as "Опубликовано: ...".
    for tag in [container, *container.find_all(True)]:
        attrs = getattr(tag, "attrs", {}) or {}
        signals = " ".join(str(attrs.get(k, "")) for k in ("class", "id", "title", "aria-label", "data-testid"))
        signal = signals.lower()
        if any(word in signal for word in ("publish", "created", "posted", "date", "опублик", "размещ", "добавлен")):
            text = tag.get("content") or tag.get("title") or tag.get_text(" ", strip=True)
            parsed = parse_datetime_value(str(text), now)
            if parsed:
                return parsed

    # 3) Structured data may include datePublished/dateCreated for the object.
    import json
    for script in container.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        try:
            data = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                for key in ("datePublished", "dateCreated", "publishedAt", "createdAt"):
                    if obj.get(key):
                        parsed = parse_datetime_value(str(obj[key]), now)
                        if parsed:
                            return parsed
                stack.extend(v for v in obj.values() if isinstance(v, (dict, list)))
            elif isinstance(obj, list):
                stack.extend(obj)

    # 4) Fallback for cards like Domovita where the posting date is rendered as plain text
    # at the end of the card. Use the LAST absolute date in the card, usually the footer's
    # publication date rather than move-in dates mentioned earlier in the description.
    card_text = container.get_text(" ", strip=True)
    matches = list(_ABSOLUTE_DATE_PATTERN.finditer(card_text))
    for match in reversed(matches):
        parsed = parse_datetime_value(match.group(0), now)
        if parsed:
            return parsed

    # Relative labels only when they occur in an element that looks like a date field.
    for tag in [container, *container.find_all(True)]:
        attrs = getattr(tag, "attrs", {}) or {}
        signals = " ".join(str(attrs.get(k, "")) for k in ("class", "id", "title", "aria-label", "data-testid"))
        if any(word in signals.lower() for word in ("date", "time", "publish", "created", "posted", "опублик", "дата", "время")):
            parsed = parse_datetime_value(tag.get_text(" ", strip=True), now)
            if parsed:
                return parsed
    return None


def published_since_yesterday(item_datetime: Optional[datetime], now: Optional[datetime] = None) -> bool:
    """True when publication is yesterday or today in Europe/Minsk, from yesterday 00:00."""
    if item_datetime is None:
        return False
    now = normalize_datetime(now or now_minsk())
    published = normalize_datetime(item_datetime, now)
    # Guard against malformed clocks/future dates; a small tolerance handles minute skew.
    if published > now + timedelta(minutes=5):
        return False
    start_window = start_of_today(now) - timedelta(days=1)
    return start_window <= published <= now + timedelta(minutes=5)


def format_datetime_minsk(value: Optional[datetime]) -> str:
    if value is None:
        return "дата публикации не определена"
    # Search cards often expose only a calendar date; do not present synthetic 00:00 as a real time.
    local = normalize_datetime(value)
    if local.hour == 0 and local.minute == 0 and local.second == 0:
        return local.strftime("%d.%m.%Y") + " (время не указано; Минск)"
    return local.strftime("%d.%m.%Y %H:%M") + " (Минск)"
