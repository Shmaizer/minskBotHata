"""Conservative adapters for apartment search sources.

Kufar and Domovita are read from public HTML search pages. Onliner HTML is a JS shell;
its public-facing search data has also been exposed through a separate legacy JSON host
in community-maintained code. We only try that endpoint after checking that the exact
path/query is allowed by that host's robots.txt. We never call r.onliner.by/sdapi/ and
never bypass auth, CAPTCHA, rate limits, or explicit robots exclusions.
"""
import asyncio
import logging
import os
import re
import time
from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from models import Listing
from date_utils import extract_published_at, parse_datetime_value

load_dotenv()
log = logging.getLogger("rent-bot.sources")

DEFAULTS = {
    "Kufar": "KUFAR_SEARCH_URLS",
    "Domovita": "DOMOVITA_SEARCH_URL",
}
USER_AGENT_TOKEN = "PersonalRentAlertBot"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PersonalRentAlertBot/1.0; respectful public-page polling)"
}
ONLINER_API_BASE = "https://ak.api.onliner.by"
ONLINER_API_PATH = "/search/apartments"
_ONLINER_ROBOTS = None
_ONLINER_ROBOTS_CHECKED_AT = 0.0
_ONLINER_ROBOTS_TTL = 24 * 60 * 60
ONLINER_API_PAGE_CURSOR = 2

# Monthly BYN amounts, for example: "1 532 р./мес.", "992 р. / мес.".
# USD/EUR values are intentionally not parsed or converted.
MONTHLY_PRICE_RE = re.compile(
    r"(?<!\d)((?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d{3,8}|\d{1,3}))\s*(?:р\.?|руб\.?|byn|бел\.?\s*руб\.?|ƃ|br)\s*(?:/\s*(?:мес\.?|месяц)|в\s*месяц|за\s*месяц)?",
    re.IGNORECASE,
)
ROOMS_RE = re.compile(
    r"(?<!\d)([1-4])\s*[-‐‑‒–— ]?\s*(?:комнатн\w*|комн\.?|квартира|к\b)",
    re.IGNORECASE,
)
AREA_RE = re.compile(r"(\d{2,3}(?:[.,]\d+)?)\s*(?:м²|м2|м\s*\^?\s*2|кв\.?\s*м)", re.IGNORECASE)
# Typical real-estate feature sequence: total / living / kitchen area.
AREA_TRIPLE_RE = re.compile(r"\b(\d{2,3}(?:[.,]\d+)?)\s*/\s*\d{1,3}(?:[.,]\d+)?\s*/\s*\d{1,2}(?:[.,]\d+)?")
ADDRESS_RE = re.compile(
    r"(?:г\.?\s*Минск\s*,?\s*)?((?:ул\.?|улица|пр-т|проспект|просп\.?|пер\.?|переулок|б-р|бульвар|пл\.?|площадь|наб\.?|набережная|проезд|шоссе)\s*[^,;|]{3,75}(?:,\s*(?:д\.?\s*)?\d+[А-Яа-яA-Za-z]?(?:\s*[/к]\s*\d+[А-Яа-я]?)?)?)",
    re.IGNORECASE,
)
METRO_STATIONS = [
    "Площадь Франтишка Богушевича", "Площадь Якуба Коласа", "Площадь Победы",
    "Институт культуры", "Каменная Горка", "Ковальская Слобода", "Тракторный завод",
    "Академия наук", "Борисовский тракт", "Барысаўскі тракт", "Петровщина",
    "Малиновка", "Могилевская", "Автозаводская", "Московская", "Купаловская",
    "Первомайская", "Партизанская", "Октябрьская", "Молодежная", "Молодёжная",
    "Фрунзенская", "Грушевка", "Михалово", "Уручье", "Восток", "Немига",
    "Пушкинская", "Спортивная", "Аэродромная", "Раковская", "Слуцкий гостинец",
    "Юбилейная площадь", "Площадь Ленина", "Пролетарская", "Кунцевщина", "Боровляны",
]
RENT_WORDS = re.compile(r"аренд|снять|сдам|сдается|сдаётся|квартир|комнат|комн\.", re.IGNORECASE)


def compact(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_number(value: str):
    digits = re.sub(r"\D", "", value or "")
    return int(digits) if digits else None


def extract_price(text: str):
    match = MONTHLY_PRICE_RE.search(text or "")
    return parse_number(match.group(1)) if match else None


def extract_rooms(text: str):
    match = ROOMS_RE.search(text or "")
    return int(match.group(1)) if match else None


def extract_area(text: str):
    # In sequences like "54 / 31 / 7 м²", the first value is the total area.
    match = AREA_TRIPLE_RE.search(text or "") or AREA_RE.search(text or "")
    return (match.group(1).replace(",", ".") + " м²") if match else ""


def first_element_text(container, selectors):
    if not container:
        return ""
    for selector in selectors:
        try:
            element = container.select_one(selector)
        except Exception:
            element = None
        value = compact(element.get_text(" ", strip=True)) if element else ""
        if value and len(value) <= 180:
            return value
    return ""


def extract_address(container, title="", text=""):
    # Prefer explicit address/location nodes exposed by the page markup.
    value = first_element_text(container, [
        '[itemprop="streetAddress"]', '[class*="address"]', '[data-testid*="address"]',
        '[class*="location"]', '[data-testid*="location"]', '[class*="subtitle"]',
    ])
    if value and not re.fullmatch(r"(?:г\.?\s*)?Минск(?:,\s*Минск)?", value, re.IGNORECASE):
        value = re.sub(r"^(?:Адрес|Расположение|Местоположение)\s*[:—-]\s*", "", value, flags=re.IGNORECASE)
        return value[:180]
    combined = f"{title} {text}"
    match = ADDRESS_RE.search(combined)
    if match:
        value = compact(match.group(1))
        # Cut metadata that sometimes follows the address in a flattened card.
        value = re.split(r"\s+(?:метро|рядом с метро|\d+[- ]комнат|\d+ м²|этаж)\b", value, maxsplit=1, flags=re.IGNORECASE)[0]
        return value.strip(" ,;.")[:180]
    # A meaningful location label is still preferable to the city name alone.
    return ""


def extract_metro(container, text=""):
    value = first_element_text(container, [
        '[class*="metro"]', '[data-testid*="metro"]', '[itemprop*="metro"]',
    ])
    if value:
        value = re.sub(r"^(?:метро|ст\.?\s*м\.?|рядом с метро)\s*[:—-]?\s*", "", value, flags=re.IGNORECASE).strip()
        if value and len(value) <= 100 and "выберите" not in value.lower():
            return value
    searchable = f"{text} {container.get_text(' ', strip=True) if container else ''}"
    folded = searchable.casefold()
    # Use a whitelist to avoid mistaking ordinary words for a metro station.
    for station in sorted(METRO_STATIONS, key=len, reverse=True):
        pos = folded.find(station.casefold())
        if pos >= 0:
            snippet = searchable[max(0, pos - 35):pos + len(station) + 45]
            walk = re.search(r"(\d{1,2}\s*(?:мин\.?|минут(?:а|ы)?)(?:\s+пешком)?)", snippet, re.IGNORECASE)
            return station + (f" ({walk.group(1)})" if walk else "")
    return ""


def image_from(container, base_url):
    img = container.select_one("img") if container else None
    if not img:
        return ""
    candidate = img.get("src") or img.get("data-src") or img.get("data-original") or ""
    if not candidate and img.get("srcset"):
        candidate = img.get("srcset").split(",")[0].strip().split(" ")[0]
    if candidate.startswith("data:") or not candidate:
        return ""
    return urljoin(base_url, candidate)


def find_card_container(anchor, max_text=2200):
    """Find a listing-card ancestor and prefer one that also contains its publication date.

    Some pages (notably Domovita) render the price/room count in a nested block and the
    publication date in a sibling footer. Returning the first ancestor with price + rooms
    drops that footer, making a real date look missing. Keep the smallest price/rooms
    ancestor as fallback, but continue upwards until we find a reasonable-size ancestor
    that also exposes a parseable publication date.
    """
    node = anchor
    best = anchor
    smallest_with_price_rooms = None
    for _ in range(12):
        if not getattr(node, "parent", None):
            break
        node = node.parent
        text = compact(node.get_text(" ", strip=True))
        if len(text) > max_text:
            continue
        if len(text) > len(compact(best.get_text(" ", strip=True))):
            best = node
        if extract_rooms(text) and extract_price(text):
            if smallest_with_price_rooms is None:
                smallest_with_price_rooms = node
            if extract_published_at(node) is not None:
                return node
    return smallest_with_price_rooms or best


def make_listing(source, anchor, base_url, href_pattern):
    href = anchor.get("href", "").strip()
    if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    url = urljoin(base_url, href).split("#")[0]
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not href_pattern.search(parsed.path):
        return None
    # Detail-page query strings are tracking metadata, not part of listing identity.
    url = parsed._replace(query="", fragment="").geturl()

    container = find_card_container(anchor)
    text = compact(container.get_text(" ", strip=True))
    if len(text) > 2200:
        text = compact(anchor.get_text(" ", strip=True))[:2200]

    raw_anchor_text = compact(anchor.get_text(" ", strip=True))
    title = compact(
        anchor.get("data-title") or anchor.get("title") or anchor.get("aria-label") or
        first_element_text(container, [
            '[class*="title"]', '[data-testid*="title"]', 'h1', 'h2', 'h3', 'h4',
        ]) or raw_anchor_text
    )
    if not title:
        return None
    # The card may have a neutral address link; accept it when nearby card text describes rent.
    if not RENT_WORDS.search(title) and not RENT_WORDS.search(text) and not extract_rooms(text):
        return None

    rooms = extract_rooms(title) or extract_rooms(text)
    price = extract_price(text) or extract_price(raw_anchor_text)
    if not rooms or not price:
        return None

    area = extract_area(text) or extract_area(raw_anchor_text)
    location = extract_address(container, title=title, text=text)
    metro = extract_metro(container, text=text)

    # If the anchor accidentally contains all card details, keep a useful concise title.
    is_card_dump = len(title) > 180 or bool(re.match(r"^\d[\d\s\u00a0]*\s*(?:р|byn|br|ƃ)", title, re.IGNORECASE))
    if is_card_dump:
        display_title = f"{rooms}-комнатная квартира в Минске"
    else:
        display_title = title

    return Listing(
        source=source,
        url=url,
        title=display_title[:200],
        price=price,
        rooms=rooms,
        location=location[:180],
        metro=metro[:100],
        area=area,
        description=text[:600],
        image_url=image_from(container, base_url),
        published_at=extract_published_at(container),
    )


def parse_domovita(soup, base_url):
    # The public long-term rental page is server-rendered and exposes listing links.
    pattern = re.compile(r"/minsk/flats/rent/[^/?]+", re.IGNORECASE)
    found = {}
    for anchor in soup.select('a[href*="/minsk/flats/rent/"]'):
        item = make_listing("Domovita", anchor, base_url, pattern)
        if item:
            found[item.url] = item
    return list(found.values())


def parse_kufar(soup, base_url):
    # Listing detail pages use /vi/minsk/snyat/kvartiru/<numeric-id>.
    pattern = re.compile(r"/vi/minsk/snyat/kvartiru/(?:[^/]+/)*\d+/?$", re.IGNORECASE)
    found = {}
    for anchor in soup.select('a[href*="/vi/minsk/snyat/kvartiru/"]'):
        item = make_listing("Kufar", anchor, base_url, pattern)
        if item:
            found[item.url] = item
    return list(found.values())


def parse_onliner(soup, base_url):
    # Only parse flat links present in the public HTML. The public catalogue currently
    # renders a JS shell and loads results via /sdapi/, which robots.txt disallows.
    # Do not emulate that private data request; if Onliner ever server-renders cards,
    # this parser will pick them up without changing the rest of the bot.
    pattern = re.compile(r"/ak/flat/\d+/?$", re.IGNORECASE)
    found = {}
    anchors = soup.select('a[href*="/ak/flat/"]')
    for anchor in anchors:
        item = make_listing("Onliner", anchor, base_url, pattern)
        if item:
            found[item.url] = item
    return list(found.values())


def _api_find(obj, names):
    """Recursively return the first non-empty value whose key is in names."""
    if isinstance(obj, dict):
        for key in names:
            if key in obj and obj[key] not in (None, "", [], {}):
                return obj[key]
        for value in obj.values():
            result = _api_find(value, names)
            if result not in (None, "", [], {}):
                return result
    elif isinstance(obj, list):
        for value in obj:
            result = _api_find(value, names)
            if result not in (None, "", [], {}):
                return result
    return None


def _api_area(apartment):
    area_obj = apartment.get("area") or apartment.get("areas") or {}
    if isinstance(area_obj, dict):
        value = (area_obj.get("total") or area_obj.get("total_area") or
                 area_obj.get("common") or area_obj.get("value"))
    else:
        value = area_obj
    if value is None:
        value = apartment.get("total_area") or apartment.get("area_total") or apartment.get("square")
    if isinstance(value, (int, float)):
        return f"{value:g} м²" if value > 0 else ""
    if isinstance(value, str):
        parsed = extract_area(value)
        if parsed:
            return parsed
        m = re.search(r"\d{2,3}(?:[.,]\d+)?", value)
        if m:
            return m.group(0).replace(",", ".") + " м²"
    # Some response shapes use total/living/kitchen values under `characteristics`.
    value = _api_find(apartment, ("total_area", "area_total", "square_total", "totalSquare"))
    if isinstance(value, (int, float)):
        return f"{value:g} м²" if value > 0 else ""
    return ""


def _api_metro(apartment):
    value = _api_find(apartment, ("metro", "nearest_metro", "metro_station", "underground"))
    if isinstance(value, dict):
        value = value.get("name") or value.get("title") or value.get("label")
    if isinstance(value, list):
        parts = []
        for entry in value:
            if isinstance(entry, dict):
                entry = entry.get("name") or entry.get("title") or entry.get("label")
            if entry:
                parts.append(str(entry))
        value = ", ".join(parts)
    return compact(str(value))[:100] if value else ""


def parse_onliner_api_payload(payload, now=None):
    """Turn the public legacy apartments JSON payload into normalized listing objects."""
    apartments = payload.get("apartments", []) if isinstance(payload, dict) else []
    if not isinstance(apartments, list):
        return []
    result = []
    for apartment in apartments:
        if not isinstance(apartment, dict):
            continue
        rent_type = str(apartment.get("rent_type") or apartment.get("type") or "")
        room_match = re.search(r"([1-4])_?rooms?", rent_type, re.IGNORECASE)
        rooms = int(room_match.group(1)) if room_match else apartment.get("rooms")
        try:
            rooms = int(rooms) if rooms is not None else None
        except (TypeError, ValueError):
            rooms = None

        price_obj = apartment.get("price") or {}
        price = None
        if isinstance(price_obj, dict):
            converted = price_obj.get("converted") or {}
            byn = converted.get("BYN") or converted.get("byn") or {}
            amount = byn.get("amount") if isinstance(byn, dict) else None
            currency = str(price_obj.get("currency") or "").upper()
            if amount is None and currency in {"BYN", "BR", "ƃ"}:
                amount = price_obj.get("amount")
            try:
                price_value = float(str(amount).replace(" ", "").replace(",", ".")) if amount is not None else None
                price = int(round(price_value)) if price_value is not None else None
            except (TypeError, ValueError):
                price = None
        else:
            try:
                price = int(round(float(price_obj)))
            except (TypeError, ValueError):
                price = None

        location_obj = apartment.get("location") or {}
        if isinstance(location_obj, dict):
            full_address = compact(str(location_obj.get("address") or ""))
            address = compact(str(location_obj.get("user_address") or full_address))
            lat = location_obj.get("latitude")
            lon = location_obj.get("longitude")
        else:
            full_address = compact(str(location_obj))
            address, lat, lon = full_address, None, None

        listing_url = str(apartment.get("url") or "").strip()
        if not listing_url and apartment.get("id") is not None:
            listing_url = f"https://r.onliner.by/ak/flat/{apartment['id']}"
        listing_url = urljoin("https://ak.onliner.by/", listing_url)
        if not listing_url.startswith(("https://", "http://")):
            continue

        # Enforce Minsk map bounds locally if coordinates are supplied. If address is
        # known and clearly outside Minsk, discard it. Server-side bounds may be omitted
        # below when the API's own robots.txt disallows that query parameter.
        min_lat = float(os.getenv("ONLINER_BOUNDS_MIN_LAT", "53.71296473440685"))
        min_lon = float(os.getenv("ONLINER_BOUNDS_MIN_LON", "27.377929687500004"))
        max_lat = float(os.getenv("ONLINER_BOUNDS_MAX_LAT", "54.08235390806399"))
        max_lon = float(os.getenv("ONLINER_BOUNDS_MAX_LON", "27.746658325195316"))
        if lat is not None and lon is not None:
            try:
                if not (min_lat <= float(lat) <= max_lat and min_lon <= float(lon) <= max_lon):
                    continue
            except (TypeError, ValueError):
                if "минск" not in full_address.casefold():
                    continue
        elif "минск" not in full_address.casefold():
            # If the API rules force us to omit the map-bounds query, be strict about
            # locality: no coordinates and no city in the full address means skip it.
            continue

        raw_photo = apartment.get("photo") or apartment.get("photo_url") or ""
        if isinstance(raw_photo, dict):
            raw_photo = raw_photo.get("url") or raw_photo.get("src") or ""
        if isinstance(raw_photo, list):
            raw_photo = raw_photo[0] if raw_photo else ""
            if isinstance(raw_photo, dict):
                raw_photo = raw_photo.get("url") or raw_photo.get("src") or ""
        image_url = urljoin("https://content.onliner.by/", str(raw_photo)) if raw_photo else ""
        created = apartment.get("created_at") or apartment.get("published_at") or apartment.get("date_created")
        published_at = parse_datetime_value(str(created), now) if created else None
        area = _api_area(apartment)
        metro = _api_metro(apartment)
        title = compact(str(apartment.get("title") or apartment.get("name") or ""))
        if not title:
            title = f"{rooms}-комнатная квартира" if rooms else "Квартира в аренду"
        if address and "минск" not in title.casefold() and "минск" not in address.casefold():
            title = f"{title}, Минск"
        result.append(Listing(
            source="Onliner", url=listing_url, title=title[:200], price=price,
            rooms=rooms, location=address[:180], metro=metro, area=area,
            description=compact(str(apartment.get("description") or ""))[:600],
            image_url=image_url, published_at=published_at,
        ))
    # Deduplicate by URL without changing the API's order.
    unique = {}
    for item in result:
        unique.setdefault(item.url, item)
    return list(unique.values())


async def _onliner_robots_parser(client):
    """Read/cache the robots.txt for the API host; fail closed on access/server errors."""
    global _ONLINER_ROBOTS, _ONLINER_ROBOTS_CHECKED_AT
    if _ONLINER_ROBOTS is not None and time.monotonic() - _ONLINER_ROBOTS_CHECKED_AT < _ONLINER_ROBOTS_TTL:
        return _ONLINER_ROBOTS
    url = ONLINER_API_BASE + "/robots.txt"
    try:
        response = await client.get(url, timeout=10, follow_redirects=True)
        if response.status_code == 200:
            parser = RobotFileParser()
            parser.set_url(url)
            parser.parse(response.text.splitlines())
        elif response.status_code in (404, 410):
            # robots.txt is unavailable; RFC 9309 treats 4xx (except auth/rate controls)
            # as unavailable rather than a policy blocking all resources.
            parser = RobotFileParser()
            parser.set_url(url)
            parser.parse([])
        else:
            log.warning("Onliner API: robots.txt вернул HTTP %s; запросы к API отключены до успешной проверки", response.status_code)
            return None
        _ONLINER_ROBOTS = parser
        _ONLINER_ROBOTS_CHECKED_AT = time.monotonic()
        log.info("Onliner API: robots.txt проверен, правила обновлены (кеш 24 часа)")
        return parser
    except Exception as exc:
        log.warning("Onliner API: не удалось проверить robots.txt (%s); запрос к API пропущен", exc)
        return None


def _onliner_api_params_allowed(robots, params):
    """Check each query parameter independently and omit any query keys disallowed by robots."""
    base_url = ONLINER_API_BASE + ONLINER_API_PATH
    if not robots.can_fetch(USER_AGENT_TOKEN, base_url):
        log.warning("Onliner API: путь %s запрещён robots.txt; источник отключён", ONLINER_API_PATH)
        return None
    permitted = []
    blocked = []
    for key, value in params:
        single_url = base_url + "?" + urlencode([(key, value)])
        if robots.can_fetch(USER_AGENT_TOKEN, single_url):
            permitted.append((key, value))
        else:
            blocked.append(key)
    if blocked:
        log.warning("Onliner API: robots.txt запрещает параметры %s; они не будут отправлены", ", ".join(sorted(set(blocked))))
    return permitted


def _onliner_api_query(page):
    rooms = os.getenv("ROOMS", "1,2").split(",")
    params = [
        ("price[min]", os.getenv("ONLINER_PRICE_MIN", "50")),
        ("price[max]", os.getenv("MAX_PRICE_BYN", "1100")),
        ("currency", "BYN"),
        ("bounds[lb][lat]", os.getenv("ONLINER_BOUNDS_MIN_LAT", "53.71296473440685")),
        ("bounds[lb][long]", os.getenv("ONLINER_BOUNDS_MIN_LON", "27.377929687500004")),
        ("bounds[rt][lat]", os.getenv("ONLINER_BOUNDS_MAX_LAT", "54.08235390806399")),
        ("bounds[rt][long]", os.getenv("ONLINER_BOUNDS_MAX_LON", "27.746658325195316")),
    ]
    for room in rooms:
        room = room.strip()
        if room.isdigit() and int(room) in (1, 2):
            n = int(room)
            params.append(("rent_type[]", "1_room" if n == 1 else "2_rooms"))
    params.append(("page", str(page)))
    return params


async def fetch_onliner_api(client, backfill=False):
    """Fetch Onliner apartments only through its separately hosted API if robots permits it."""
    global ONLINER_API_PAGE_CURSOR
    if os.getenv("ONLINER_API_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        log.info("Onliner API выключен параметром ONLINER_API_ENABLED")
        return []
    robots = await _onliner_robots_parser(client)
    if robots is None:
        return []

    try:
        page_limit = max(1, min(int(os.getenv("ONLINER_API_BACKFILL_PAGES", "3")), 5))
    except ValueError:
        page_limit = 3
    if backfill:
        pages = list(range(1, page_limit + 1))
        ONLINER_API_PAGE_CURSOR = 2 if page_limit > 1 else 1
    else:
        pages = [1]
        if page_limit > 1:
            pages.append(ONLINER_API_PAGE_CURSOR)
            ONLINER_API_PAGE_CURSOR += 1
            if ONLINER_API_PAGE_CURSOR > page_limit:
                ONLINER_API_PAGE_CURSOR = 2

    found = []
    for page in pages:
        params = _onliner_api_params_allowed(robots, _onliner_api_query(page))
        if params is None:
            return found
        page_allowed = any(key == "page" for key, _ in params)
        if page > 1 and not page_allowed:
            break
        url = ONLINER_API_BASE + ONLINER_API_PATH + ("?" + urlencode(params, doseq=True) if params else "")
        if not robots.can_fetch(USER_AGENT_TOKEN, url):
            # Combinations of individually permitted parameters may still match a robots wildcard.
            log.warning("Onliner API: полный URL запрещён robots.txt; карточки не запрашиваются: %s", urlsplit(url).path)
            return found
        try:
            response = await client.get(url, headers={"Accept": "application/json"}, timeout=18, follow_redirects=True)
            if response.status_code in (401, 403, 429):
                log.warning("Onliner API: HTTP %s — источник пропущен, обход ограничений не выполняется", response.status_code)
                return found
            response.raise_for_status()
            try:
                payload = response.json()
            except ValueError:
                log.warning("Onliner API: ответ не является JSON; источник пропущен")
                return found
            items = parse_onliner_api_payload(payload)
            found.extend(items)
            info = payload.get("page", {}) if isinstance(payload, dict) else {}
            last_page = int(info.get("last", page) or page) if isinstance(info, dict) else page
            log.info("Onliner API: страница %s — JSON-кандидатов=%s, карточек после разбора=%s, всего=%s",
                     page, len(payload.get("apartments", [])) if isinstance(payload, dict) else 0,
                     len(items), payload.get("total", "не указано") if isinstance(payload, dict) else "не указано")
            if page >= last_page or page >= page_limit or not page_allowed:
                # If page was forbidden by robots, never issue repeated identical requests.
                if not page_allowed:
                    log.info("Onliner API: параметр page запрещён robots.txt; за один цикл проверяется только первая страница")
                break
        except httpx.HTTPStatusError as exc:
            log.warning("Onliner API: HTTP %s при запросе списка; источник пропущен", exc.response.status_code)
            return found
        except Exception as exc:
            log.warning("Onliner API: ошибка запроса (%s); источник пропущен", exc)
            return found
        await asyncio.sleep(1.5)

    unique = {}
    for item in found:
        unique.setdefault(item.url, item)
    log.info("Onliner API: всего распознано уникальных карточек=%s", len(unique))
    return list(unique.values())


def onliner_public_url(url: str) -> str:
    """Remove query keys that Onliner's current robots.txt disallows for crawlers.

    Price and map bounds are enforced by the bot after reading any public HTML result.
    This avoids fetching the user's original URL with `price[...]`/`bounds[...]` params.
    """
    parts = urlsplit(url)
    allowed = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        key_lower = key.lower()
        if any(blocked in key_lower for blocked in ("price", "bounds", "metro", "area", "page", "order")):
            continue
        if key_lower.startswith("rent_type"):
            allowed.append((key, value))
        # Other query keys are omitted unless explicitly known to be needed.
    if not any(key.lower().startswith("rent_type") for key, _ in allowed):
        allowed = [("rent_type[]", "1_room"), ("rent_type[]", "2_rooms")]
    return urlunsplit((parts.scheme or "https", parts.netloc or "r.onliner.by", parts.path or "/ak/", urlencode(allowed, doseq=True), ""))


PARSERS = {
    "Kufar": parse_kufar,
    "Onliner": parse_onliner,
    "Domovita": parse_domovita,
}


DOMOVITA_PAGE_CURSOR = 2


def domovita_page_url(base_url: str, page: int) -> str:
    """Build a public Domovita pagination URL (?page=N)."""
    if page <= 1:
        return base_url
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def configured_sources(backfill: bool = False):
    """Configure public HTML sources; Onliner is handled by its robots-checked API adapter."""
    global DOMOVITA_PAGE_CURSOR
    result = []
    kufar_urls = [x.strip() for x in os.getenv("KUFAR_SEARCH_URLS", "").split(",") if x.strip()]
    for url in kufar_urls:
        result.append(("Kufar", url))

    domovita_url = os.getenv("DOMOVITA_SEARCH_URL", "").strip()
    if domovita_url:
        try:
            max_pages = max(1, min(int(os.getenv("DOMOVITA_PAGES", "3")), 10))
        except ValueError:
            max_pages = 3
        if backfill:
            pages = list(range(1, max_pages + 1))
            DOMOVITA_PAGE_CURSOR = 2 if max_pages > 1 else 1
        else:
            pages = [1]
            if max_pages > 1:
                pages.append(DOMOVITA_PAGE_CURSOR)
                DOMOVITA_PAGE_CURSOR += 1
                if DOMOVITA_PAGE_CURSOR > max_pages:
                    DOMOVITA_PAGE_CURSOR = 2
        for page in pages:
            result.append(("Domovita", domovita_page_url(domovita_url, page)))
    return result


async def fetch_source(client, source, url):
    try:
        response = await client.get(url, timeout=18, follow_redirects=True)
        if response.status_code in (401, 403, 429):
            log.warning("%s: HTTP %s — источник пропущен; обход ограничений отключён: %s", source, response.status_code, url)
            return []
        response.raise_for_status()
        final_url = str(response.url)
        soup = BeautifulSoup(response.text, "html.parser")
        items = PARSERS[source](soup, final_url)
        if not items:
            if source == "Onliner":
                flat_links = len(soup.select('a[href*="/ak/flat/"]'))
                if flat_links == 0:
                    log.warning(
                        "Onliner: в HTML каталога нет ссылок /ak/flat/ (найдено %s ссылок-кандидатов). "
                        "Каталог загружает объявления через /sdapi/, этот путь запрещён в robots.txt; "
                        "бот не обращается к нему и не обходит ограничение. Источник не дал карточек: %s",
                        flat_links, final_url
                    )
                else:
                    log.warning(
                        "Onliner: ссылки карточек есть (%s), но не удалось распознать цену/комнаты/ссылку; "
                        "нужно обновить парсер HTML. URL: %s", flat_links, final_url
                    )
            else:
                log.warning("%s: публичная HTML-страница открылась, но подходящие карточки в HTML не найдены: %s", source, final_url)
        else:
            log.info("%s: распознано карточек: %d", source, len(items))
        return items
    except httpx.HTTPStatusError as exc:
        log.warning("Источник %s вернул HTTP %s: %s", source, exc.response.status_code, url)
    except Exception as exc:
        log.warning("Источник %s недоступен: %s", source, exc)
    return []


async def collect_all(backfill: bool = False):
    configured = configured_sources(backfill=backfill)
    if not configured and os.getenv("ONLINER_API_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        log.warning("Источники не настроены или отключены: проверьте KUFAR_SEARCH_URLS, DOMOVITA_SEARCH_URL и ONLINER_API_ENABLED")
        return []
    all_items = []
    async with httpx.AsyncClient(headers=HEADERS) as client:
        for source, url in configured:
            all_items.extend(await fetch_source(client, source, url))
            await asyncio.sleep(1.5)
        # Onliner HTML shell doesn't contain apartment cards. The alternative endpoint
        # is attempted only after checking its own robots.txt and each actual URL.
        all_items.extend(await fetch_onliner_api(client, backfill=backfill))
    unique = {}
    for item in all_items:
        unique.setdefault((item.source, item.url), item)
    return list(unique.values())

