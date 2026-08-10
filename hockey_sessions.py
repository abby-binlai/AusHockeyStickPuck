"""
Austin Hockey Ice Finder
Scrapes selected public sessions from:
1. Crossover / Ice & Field (DaySmart)
2. Chaparral Ice (DaySmart)
3. The Pond Hockey Club

Run:
    pip install -r requirements.txt
    playwright install chromium
    streamlit run hockey_sessions.py
"""

from __future__ import annotations

import re
import base64
import html as htmlmod
import json
from datetime import date, datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlencode, urlparse, parse_qs, quote, urlsplit, urlunsplit, parse_qsl

import pandas as pd
from zoneinfo import ZoneInfo
from dateutil.rrule import rrulestr
import streamlit as st
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


RINKS = [
    {
        "rink": "Crossover",
        "kind": "daysmart",
        "url": "https://apps.daysmartrecreation.com/dash/x/iceandfield/calendar",
        "wanted": ["private hockey coaches ice", "stick & puck"],
    },
    {
        "rink": "Chaparral",
        "kind": "daysmart",
        "url": "https://apps.daysmartrecreation.com/dash/x/chaparralice/calendar",
        "wanted": ["hockey stick and puck"],
    },
    {
        "rink": "The Pond",
        "kind": "pond",
        "url": "https://www.pondhockeyclub.com/page/show/2723404-rink-schedules",
        "wanted": ["barn time", "pond time"],
    },
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def contains_wanted(text: str, wanted: Iterable[str]) -> bool:
    t = norm(text).lower()
    return any(w.lower() in t for w in wanted)


def date_from_text(text: str, default_year: int) -> str | None:
    """Best-effort date extraction from rendered event text."""
    patterns = [
        r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b",
        r"\b(\d{1,2})/(\d{1,2})\b",
        r"\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+(\d{1,2})(?:,\s*(\d{4}))?\b",
    ]
    m = re.search(patterns[0], text, re.I)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    m = re.search(patterns[1], text, re.I)
    if m:
        return f"{default_year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    m = re.search(patterns[2], text, re.I)
    if m:
        month = datetime.strptime(m.group(1)[:3], "%b").month
        year = int(m.group(3)) if m.group(3) else default_year
        return f"{year:04d}-{month:02d}-{int(m.group(2)):02d}"
    return None


def times_from_text(text: str) -> tuple[str, str]:
    """Extract one or two clock times such as 10:30 AM - 11:45 AM."""
    matches = re.findall(
        r"\b(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm))\b",
        text,
    )
    if not matches:
        return "", ""
    if len(matches) == 1:
        return norm(matches[0]).upper(), ""
    return norm(matches[0]).upper(), norm(matches[1]).upper()


def clean_title(text: str, wanted: Iterable[str]) -> str:
    lower = text.lower()
    for w in wanted:
        pos = lower.find(w.lower())
        if pos >= 0:
            return text[pos:pos + len(w)]
    return norm(text)[:120]


def scrape_rendered_events(scope, rink: dict, start: date, end: date) -> list[dict]:
    """
    Scrape likely event nodes, then fall back to rendered text lines.

    `scope` may be a Playwright Page or Frame. This matters for sites such as
    The Pond, where the actual schedule can be embedded in an iframe.
    """
    selectors = [
        ".fc-event",
        ".fc-event-main",
        ".fc-list-event",
        "[data-event-id]",
        "[class*='calendar'] a",
        "[class*='event']",
        "[role='button']",
        "a",
    ]

    seen = set()
    rows = []

    def add_row(text: str, context: str, href: str = ""):
        text = norm(text)
        context = norm(context)
        if not text or not contains_wanted(text, rink["wanted"]):
            return

        event_date = date_from_text(context, start.year)
        start_time, end_time = times_from_text(context)
        key = (
            rink["rink"],
            event_date or "",
            start_time,
            end_time,
            clean_title(text, rink["wanted"]).lower(),
            context.lower(),
        )
        if key in seen:
            return
        seen.add(key)

        rows.append({
            "date": event_date or "",
            "start": start_time,
            "end": end_time,
            "rink": rink["rink"],
            "session": clean_title(text, rink["wanted"]).title(),
            "details": context,
            "source": href or getattr(scope, "url", rink["url"]),
        })

    # First try actual event-like DOM nodes.
    for selector in selectors:
        try:
            nodes = scope.locator(selector)
            count = min(nodes.count(), 1800)
        except Exception:
            continue

        for i in range(count):
            try:
                node = nodes.nth(i)
                node_text = norm(node.inner_text(timeout=600))
                if not node_text or not contains_wanted(node_text, rink["wanted"]):
                    continue

                href = ""
                try:
                    href = node.get_attribute("href") or ""
                except Exception:
                    pass

                # Calendar libraries often put date/time one or two levels up.
                contexts = [node_text]
                for xpath in ("..", "../.."):
                    try:
                        parent_text = norm(
                            node.locator(f"xpath={xpath}").inner_text(timeout=600)
                        )
                        if parent_text and len(parent_text) <= 1200:
                            contexts.append(parent_text)
                    except Exception:
                        pass

                # Prefer the richest nearby context.
                context = max(contexts, key=len)
                add_row(node_text, context, href)
            except Exception:
                continue

    # Robust fallback. IMPORTANT: split the raw body into lines BEFORE
    # whitespace normalization. The original scraper normalized first, which
    # removed the line breaks and could hide valid Chaparral sessions.
    try:
        raw_body = scope.locator("body").inner_text(timeout=7000)
    except Exception:
        raw_body = ""

    if raw_body:
        raw_lines = [norm(x) for x in re.split(r"[\r\n]+", raw_body)]
        lines = [x for x in raw_lines if x]

        for i, line in enumerate(lines):
            if not contains_wanted(line, rink["wanted"]):
                continue

            # Include nearby lines so headings/date/time around an event title
            # become part of the same candidate record.
            lo = max(0, i - 4)
            hi = min(len(lines), i + 5)
            context = " | ".join(lines[lo:hi])
            if len(context) > 1600:
                context = context[:1600]

            add_row(line, context)

    return rows


def scrape_all_scopes(page, rink: dict, start: date, end: date) -> list[dict]:
    """Search the main document plus every accessible iframe."""
    rows = []
    scopes = [page]

    # Playwright exposes cross-origin iframe contents through Frame objects.
    for frame in page.frames:
        if frame != page.main_frame:
            scopes.append(frame)

    for scope in scopes:
        try:
            rows.extend(scrape_rendered_events(scope, rink, start, end))
        except Exception:
            continue

    # De-duplicate rows collected from overlapping page/frame scopes.
    unique = []
    seen = set()
    for row in rows:
        key = (
            row["date"],
            row["start"],
            row["end"],
            row["rink"],
            row["session"].lower(),
            row["details"].lower(),
        )
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique



def _flatten_json_objects(value):
    """Yield every dict contained anywhere in a JSON response."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _flatten_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _flatten_json_objects(child)


def _first_value(obj: dict, keys: list[str]):
    lower = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        if key.lower() in lower and lower[key.lower()] not in (None, ""):
            return lower[key.lower()]
    return None


def _parse_dt(value):
    """Best-effort parser for ISO strings and Unix timestamps."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            # Handle milliseconds as well as seconds.
            if value > 10_000_000_000:
                value = value / 1000
            return datetime.fromtimestamp(value)
        except Exception:
            return None
    if isinstance(value, str):
        s = value.strip().replace('Z', '+00:00')
        for candidate in (s, s.replace(' ', 'T')):
            try:
                return datetime.fromisoformat(candidate)
            except Exception:
                pass
        # Common US date/time formats used by calendar APIs.
        for fmt in (
            '%m/%d/%Y %I:%M %p', '%m/%d/%Y %I:%M:%S %p',
            '%Y-%m-%d %I:%M %p', '%Y-%m-%d %H:%M:%S',
        ):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                pass
    return None


def _format_clock(dt):
    if dt is None:
        return ''
    # %-I is unavailable on Windows, but Railway runs Linux. Keep portable.
    return dt.strftime('%I:%M %p').lstrip('0')


def rows_from_daysmart_json(payload, selected: date, rink_name: str,
                             session_name: str, source_url: str) -> list[dict]:
    """
    Recursively inspect a DaySmart JSON payload for event-like dictionaries.
    We intentionally support several possible field names because the public
    calendar endpoint is undocumented and may vary between facilities.
    """
    rows = []
    seen = set()
    session_words = [w for w in re.findall(r'[a-z0-9]+', session_name.lower()) if len(w) > 2]

    title_keys = [
        'title', 'name', 'event_name', 'eventName', 'description',
        'display_name', 'displayName', 'event_type_name', 'eventTypeName',
        'program_name', 'programName', 'type_name', 'typeName'
    ]
    start_keys = [
        'start', 'start_date', 'startDate', 'start_datetime', 'startDateTime',
        'start_time', 'startTime', 'begin', 'begin_date', 'beginDate',
        'begin_datetime', 'beginDateTime', 'date_start', 'dateStart'
    ]
    end_keys = [
        'end', 'end_date', 'endDate', 'end_datetime', 'endDateTime',
        'end_time', 'endTime', 'finish', 'finish_date', 'finishDate',
        'date_end', 'dateEnd'
    ]

    for obj in _flatten_json_objects(payload):
        try:
            compact = json.dumps(obj, default=str, ensure_ascii=False)
        except Exception:
            compact = str(obj)
        low = compact.lower()

        # A candidate needs to mention the requested session. Requiring all
        # meaningful words avoids matching unrelated navigation/config data.
        if session_words and not all(word in low for word in session_words):
            continue

        title = _first_value(obj, title_keys)
        title_text = norm(str(title)) if title is not None else session_name

        start_raw = _first_value(obj, start_keys)
        end_raw = _first_value(obj, end_keys)
        start_dt = _parse_dt(start_raw)
        end_dt = _parse_dt(end_raw)

        # Sometimes date and time are separate fields. Try joining them.
        if start_dt is None:
            date_part = _first_value(obj, ['date', 'event_date', 'eventDate', 'start_date', 'startDate'])
            time_part = _first_value(obj, ['time', 'start_time', 'startTime'])
            if date_part and time_part:
                start_dt = _parse_dt(f'{date_part} {time_part}')
        if end_dt is None:
            date_part = _first_value(obj, ['date', 'event_date', 'eventDate', 'end_date', 'endDate'])
            time_part = _first_value(obj, ['end_time', 'endTime', 'finish_time', 'finishTime'])
            if date_part and time_part:
                end_dt = _parse_dt(f'{date_part} {time_part}')

        # If an actual timestamp exists, require the selected date. If the API
        # returns only clock values, keep the row because the request itself is
        # already scoped to one selected day.
        if start_dt is not None and start_dt.date() != selected:
            continue

        start_clock = _format_clock(start_dt)
        end_clock = _format_clock(end_dt)

        # Reject configuration objects that mention the title but contain no
        # usable event timing at all.
        if not start_clock and not end_clock:
            continue

        key = (selected.isoformat(), start_clock, end_clock, session_name.lower())
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            'date': selected.isoformat(),
            'start': start_clock,
            'end': end_clock,
            'rink': rink_name,
            'session': session_name,
            'details': f'NETWORK JSON | {title_text} | {compact[:900]}',
            'source': source_url,
        })

    return rows


def capture_daysmart_json_events(page, url: str, selected: date,
                                  rink_name: str, session_name: str):
    """
    Capture XHR/fetch/JSON responses generated while DaySmart renders its
    calendar, then parse event records from those responses.

    Returns (rows, diagnostic_urls).
    """
    captured = []

    def on_response(response):
        try:
            resource_type = response.request.resource_type
            content_type = (response.headers.get('content-type') or '').lower()
            if resource_type in ('xhr', 'fetch') or 'json' in content_type:
                captured.append(response)
        except Exception:
            pass

    page.on('response', on_response)
    page.goto(url, wait_until='domcontentloaded', timeout=45_000)
    try:
        page.wait_for_load_state('networkidle', timeout=15_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(4000)

    rows = []
    diagnostics = []
    for response in captured:
        try:
            diagnostics.append(response.url)
            payload = response.json()
        except Exception:
            continue
        try:
            rows.extend(rows_from_daysmart_json(
                payload, selected, rink_name, session_name, response.url
            ))
        except Exception:
            continue

    # De-dupe across multiple API responses.
    unique = []
    seen = set()
    for row in rows:
        key = (row['date'], row['start'], row['end'], row['rink'], row['session'])
        if key not in seen:
            seen.add(key)
            unique.append(row)

    return unique, list(dict.fromkeys(diagnostics))


def scrape_daysmart_filtered(page, rink: dict, selected: date,
                              event_type: int, session_name: str) -> list[dict]:
    """
    Network-first Crossover scraper.

    1. Open the one-day, one-event-type DaySmart calendar.
    2. Capture XHR/fetch/JSON responses used to render it.
    3. Parse actual event timestamps from JSON.
    4. Fall back to rendered DOM only when no JSON events were found.
    """
    params = urlencode({
        'start': selected.isoformat(),
        'end': selected.isoformat(),
        'event_type': event_type,
    })
    url = f'{rink["url"]}?{params}'

    network_rows, network_urls = capture_daysmart_json_events(
        page, url, selected, rink["rink"], session_name
    )
    if network_rows:
        return network_rows

    # DOM fallback. This is intentionally secondary now.
    selectors = [
        '.fc-event', '.fc-timegrid-event', '.fc-daygrid-event',
        '.fc-list-event', '[class*="fc-event"]', '[data-event-id]'
    ]
    rows = []
    seen = set()
    expected_words = [w for w in re.findall(r'[a-z0-9]+', session_name.lower()) if len(w) > 2]

    for selector in selectors:
        try:
            nodes = page.locator(selector)
            count = min(nodes.count(), 500)
        except Exception:
            continue

        for i in range(count):
            try:
                node = nodes.nth(i)
                pieces = []
                for getter in (
                    lambda: node.inner_text(timeout=700),
                    lambda: node.get_attribute('aria-label'),
                    lambda: node.get_attribute('title'),
                    lambda: node.get_attribute('data-title'),
                ):
                    try:
                        value = getter()
                        if value:
                            value = norm(value)
                            if value and value not in pieces:
                                pieces.append(value)
                    except Exception:
                        pass

                blob = ' | '.join(pieces)
                if not blob:
                    continue
                low = blob.lower()
                if expected_words and not all(w in low for w in expected_words):
                    continue

                start_time, end_time = times_from_text(blob)
                if not start_time:
                    continue
                key = (selected.isoformat(), start_time, end_time, session_name.lower())
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    'date': selected.isoformat(),
                    'start': start_time,
                    'end': end_time,
                    'rink': rink["rink"],
                    'session': session_name,
                    'details': 'DOM FALLBACK | ' + blob + ' | CAPTURED URLS: ' + ' ; '.join(network_urls[:20]),
                    'source': url,
                })
            except Exception:
                continue

    return rows


def scrape_daysmart(page, rink: dict, start: date, end: date) -> list[dict]:
    # DaySmart accepts start/end query params on calendar URLs.
    params = urlencode({"start": start.isoformat(), "end": end.isoformat()})
    url = f'{rink["url"]}?{params}'
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)

    # Give the SPA/calendar time to hydrate.
    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3500)

    return scrape_all_scopes(page, rink, start, end)


def _unfold_ics(text: str) -> list[str]:
    """Unfold RFC 5545 continuation lines."""
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for line in raw:
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _parse_ics_dt(key: str, value: str) -> datetime | None:
    """
    Parse common Google Calendar ICS date/time formats.
    Returns an America/Chicago-aware datetime where possible.
    """
    tz = ZoneInfo("America/Chicago")
    value = value.strip()

    try:
        # All-day date.
        if "VALUE=DATE" in key.upper() or re.fullmatch(r"\d{8}", value):
            dt = datetime.strptime(value[:8], "%Y%m%d")
            return dt.replace(tzinfo=tz)

        # UTC timestamp.
        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return dt.astimezone(tz)

        # Explicit TZID in property key.
        m = re.search(r"TZID=([^;:]+)", key, re.I)
        if m:
            tzid = m.group(1)
            try:
                event_tz = ZoneInfo(tzid)
            except Exception:
                event_tz = tz
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=event_tz)
            return dt.astimezone(tz)

        # Floating time: treat as local Austin time.
        dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
        return dt.replace(tzinfo=tz)
    except Exception:
        return None


def _parse_google_ics(ics_text: str, selected: date, source_url: str) -> list[dict]:
    """Parse Barn Time / Pond Time events from a public Google Calendar ICS feed."""
    lines = _unfold_ics(ics_text)
    events = []
    current = None

    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current:
                summary = norm(current.get("SUMMARY", ""))
                lower = summary.lower()

                session = None
                if "barn time" in lower:
                    session = "Barn Time"
                elif "pond time" in lower:
                    session = "Pond Time"

                if session:
                    start_dt = current.get("_START_DT")
                    end_dt = current.get("_END_DT")
                    if start_dt and start_dt.date() == selected:
                        events.append({
                            "date": selected.isoformat(),
                            "start": start_dt.strftime("%-I:%M %p"),
                            "end": end_dt.strftime("%-I:%M %p") if end_dt else "",
                            "rink": "The Pond",
                            "session": session,
                            "details": f"GOOGLE CALENDAR ICS | {summary}",
                            "source": source_url,
                        })
            current = None
            continue

        if current is None or ":" not in line:
            continue

        key, value = line.split(":", 1)
        base_key = key.split(";", 1)[0].upper()

        if base_key == "SUMMARY":
            current["SUMMARY"] = value.replace("\\,", ",").replace("\\n", " ")
        elif base_key == "DTSTART":
            current["_START_DT"] = _parse_ics_dt(key, value)
        elif base_key == "DTEND":
            current["_END_DT"] = _parse_ics_dt(key, value)

    return events


def _google_embed_for_date(src: str, selected: date) -> str:
    """
    Force a Google Calendar embed into agenda mode for one selected date.
    This lets Google expand recurring events itself instead of us trying to
    interpret RRULE/EXDATE/RDATE recurrence logic from ICS.
    """
    parts = urlsplit(src)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    ymd = selected.strftime("%Y%m%d")

    params["mode"] = "AGENDA"
    params["dates"] = f"{ymd}/{ymd}"
    params["showTitle"] = "0"
    params["showNav"] = "0"
    params["showDate"] = "1"
    params["showPrint"] = "0"
    params["showTabs"] = "0"
    params["showCalendars"] = "0"
    params["showTz"] = "0"
    params["ctz"] = "America/Chicago"

    return urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(params, doseq=True),
        parts.fragment,
    ))


def _pond_event_rows_from_text(
    body_text: str,
    selected: date,
    source_url: str,
) -> list[dict]:
    """
    Parse only exact Barn Time / Pond Time occurrences from a one-day Google
    Calendar agenda view.

    The agenda text commonly places the event title and time on nearby lines,
    so inspect a small local window only. Never borrow context from the whole
    calendar.
    """
    raw_lines = re.split(r"[\r\n]+", body_text or "")
    lines = [norm(x) for x in raw_lines if norm(x)]
    rows = []
    seen = set()

    for i, line in enumerate(lines):
        lower = line.lower()

        if "barn time" in lower:
            session = "Barn Time"
        elif "pond time" in lower:
            session = "Pond Time"
        else:
            continue

        # Keep a very small context window around this exact event.
        lo = max(0, i - 3)
        hi = min(len(lines), i + 4)
        context_lines = lines[lo:hi]
        context = " | ".join(context_lines)

        start_time, end_time = times_from_text(context)

        # Google agenda sometimes formats a range with an en dash / hyphen
        # but our generic parser may see only one time. Try a direct range.
        if not end_time:
            m = re.search(
                r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))\s*[-–—]\s*"
                r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))",
                context,
                re.I,
            )
            if m:
                start_time = norm(m.group(1)).upper()
                end_time = norm(m.group(2)).upper()

        if not start_time:
            continue

        key = (session, start_time, end_time)
        if key in seen:
            continue
        seen.add(key)

        rows.append({
            "date": selected.isoformat(),
            "start": start_time,
            "end": end_time,
            "rink": "The Pond",
            "session": session,
            "details": f"GOOGLE CALENDAR AGENDA | {context}",
            "source": source_url,
        })

    return rows


POND_GOOGLE_CALENDARS = [
    {
        "calendar_id": "thebarnrinkcalendar@gmail.com",
        "session": "Barn Time",
    },
    {
        "calendar_id": "thepondcalendar@gmail.com",
        "session": "Pond Time",
    },
]


def _unfold_ics_lines(ics_text: str) -> list[str]:
    raw = ics_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for line in raw:
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def _ics_unescape(value: str) -> str:
    return (
        value.replace("\\n", " ")
             .replace("\\N", " ")
             .replace("\\,", ",")
             .replace("\\;", ";")
             .replace("\\\\", "\\")
    )


def _parse_ics_datetime(prop_name: str, value: str) -> datetime | None:
    """Parse common Google Calendar DTSTART/DTEND/EXDATE values."""
    local_tz = ZoneInfo("America/Chicago")
    value = value.strip()

    try:
        if "VALUE=DATE" in prop_name.upper() or re.fullmatch(r"\d{8}", value):
            return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=local_tz)

        if value.endswith("Z"):
            dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
            return dt.replace(tzinfo=timezone.utc).astimezone(local_tz)

        tz_match = re.search(r"TZID=([^;:]+)", prop_name, re.I)
        if tz_match:
            tzid = tz_match.group(1)
            try:
                tz = ZoneInfo(tzid)
            except Exception:
                tz = local_tz
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=tz)
            return dt.astimezone(local_tz)

        dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
        return dt.replace(tzinfo=local_tz)
    except Exception:
        return None


def _parse_ics_events(ics_text: str) -> list[dict]:
    """Parse VEVENT blocks while preserving recurrence information."""
    lines = _unfold_ics_lines(ics_text)
    events = []
    current = None

    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {"EXDATE": []}
            continue

        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue

        if current is None or ":" not in line:
            continue

        key, value = line.split(":", 1)
        base = key.split(";", 1)[0].upper()

        if base == "SUMMARY":
            current["SUMMARY"] = _ics_unescape(value)
        elif base == "DTSTART":
            current["DTSTART"] = _parse_ics_datetime(key, value)
        elif base == "DTEND":
            current["DTEND"] = _parse_ics_datetime(key, value)
        elif base == "RRULE":
            current["RRULE"] = value.strip()
        elif base == "RECURRENCE-ID":
            current["RECURRENCE-ID"] = _parse_ics_datetime(key, value)
        elif base == "EXDATE":
            for part in value.split(","):
                dt = _parse_ics_datetime(key, part)
                if dt:
                    current["EXDATE"].append(dt)
        elif base == "STATUS":
            current["STATUS"] = value.strip().upper()

    return events


def _expand_ics_event_for_date(event: dict, selected: date) -> list[tuple[datetime, datetime | None]]:
    """
    Expand one VEVENT for exactly the selected local date.

    Handles normal events, recurring RRULE events, EXDATE exclusions, and
    detached recurrence overrides (RECURRENCE-ID events).
    """
    local_tz = ZoneInfo("America/Chicago")
    start_dt = event.get("DTSTART")
    end_dt = event.get("DTEND")

    if not start_dt or event.get("STATUS") == "CANCELLED":
        return []

    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=local_tz)
    else:
        start_dt = start_dt.astimezone(local_tz)

    if end_dt:
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=local_tz)
        else:
            end_dt = end_dt.astimezone(local_tz)

    duration = (end_dt - start_dt) if end_dt else None
    day_start = datetime.combine(selected, datetime.min.time(), tzinfo=local_tz)
    day_end = day_start + timedelta(days=1)

    # Detached override / ordinary non-recurring event.
    if not event.get("RRULE"):
        if day_start <= start_dt < day_end:
            return [(start_dt, start_dt + duration if duration else end_dt)]
        return []

    try:
        rule = rrulestr(event["RRULE"], dtstart=start_dt)
        occurrences = rule.between(day_start, day_end, inc=True)
    except Exception:
        return []

    excluded = set()
    for ex in event.get("EXDATE", []):
        if ex.tzinfo is None:
            ex = ex.replace(tzinfo=local_tz)
        else:
            ex = ex.astimezone(local_tz)
        excluded.add(ex.replace(microsecond=0))

    rows = []
    for occ in occurrences:
        if occ.tzinfo is None:
            occ = occ.replace(tzinfo=local_tz)
        else:
            occ = occ.astimezone(local_tz)

        if occ.replace(microsecond=0) in excluded:
            continue

        rows.append((occ, occ + duration if duration else None))

    return rows


def _format_clock(dt: datetime | None) -> str:
    if not dt:
        return ""
    # Portable 12-hour formatting without leading zero.
    return dt.strftime("%I:%M %p").lstrip("0")


def scrape_pond(page, rink: dict, start: date, end: date) -> list[dict]:
    """
    Query The Pond's two public Google Calendar ICS feeds directly and expand
    recurring events for exactly the selected date.

    Barn Time:
        thebarnrinkcalendar@gmail.com

    Pond Time:
        thepondcalendar@gmail.com
    """
    selected = start
    rows = []

    for cal in POND_GOOGLE_CALENDARS:
        calendar_id = cal["calendar_id"]
        session_name = cal["session"]

        encoded_id = quote(calendar_id, safe="")
        ics_url = (
            "https://calendar.google.com/calendar/ical/"
            f"{encoded_id}/public/basic.ics"
        )

        try:
            response = page.request.get(ics_url, timeout=20_000)
        except Exception as exc:
            rows.append({
                "date": selected.isoformat(),
                "start": "",
                "end": "",
                "rink": "The Pond",
                "session": "POND DEBUG",
                "details": f"ICS REQUEST ERROR | {calendar_id} | {exc}",
                "source": ics_url,
            })
            continue

        if not response.ok:
            rows.append({
                "date": selected.isoformat(),
                "start": "",
                "end": "",
                "rink": "The Pond",
                "session": "POND DEBUG",
                "details": (
                    f"ICS HTTP {response.status} | {calendar_id} | {ics_url}"
                ),
                "source": ics_url,
            })
            continue

        try:
            events = _parse_ics_events(response.text())
        except Exception as exc:
            rows.append({
                "date": selected.isoformat(),
                "start": "",
                "end": "",
                "rink": "The Pond",
                "session": "POND DEBUG",
                "details": f"ICS PARSE ERROR | {calendar_id} | {exc}",
                "source": ics_url,
            })
            continue

        matched = 0

        for event in events:
            summary = norm(event.get("SUMMARY", ""))
            if session_name.lower() not in summary.lower():
                continue

            for occurrence_start, occurrence_end in _expand_ics_event_for_date(
                event, selected
            ):
                rows.append({
                    "date": selected.isoformat(),
                    "start": _format_clock(occurrence_start),
                    "end": _format_clock(occurrence_end),
                    "rink": "The Pond",
                    "session": session_name,
                    "details": (
                        f"GOOGLE ICS | {calendar_id} | {summary} | "
                        f"RRULE={event.get('RRULE', '')}"
                    ),
                    "source": ics_url,
                })
                matched += 1

        if matched == 0:
            # Keep a diagnostic row hidden from the public schedule so we can
            # distinguish "feed accessible, no match" from "feed unavailable".
            summaries = sorted({
                norm(e.get("SUMMARY", ""))
                for e in events
                if e.get("SUMMARY")
            })
            interesting = [
                s for s in summaries
                if "barn" in s.lower() or "pond" in s.lower() or "time" in s.lower()
            ][:20]

            rows.append({
                "date": selected.isoformat(),
                "start": "",
                "end": "",
                "rink": "The Pond",
                "session": "POND DEBUG",
                "details": (
                    f"ICS OK, 0 {session_name} occurrences on {selected} | "
                    f"calendar={calendar_id} | "
                    f"candidate summaries={interesting}"
                ),
                "source": ics_url,
            })

    # Strict de-dupe; Barn Time and Pond Time remain separate.
    unique = []
    seen = set()
    for row in rows:
        key = (
            row["date"],
            row["start"],
            row["end"],
            row["session"],
            row["details"] if row["session"] == "POND DEBUG" else "",
        )
        if key not in seen:
            seen.add(key)
            unique.append(row)

    return unique


@st.cache_data(ttl=3600, show_spinner=False)
def scrape_all(start_iso: str, end_iso: str) -> pd.DataFrame:
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1100},
            locale="en-US",
            timezone_id="America/Chicago",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/136.0.0.0 Safari/537.36"
            ),
        )

        for rink in RINKS:
            page = context.new_page()
            try:
                if rink["rink"] == "Crossover":
                    # Use DaySmart's explicit event-type filters for both
                    # Crossover session categories.
                    rows.extend(
                        scrape_daysmart_filtered(
                            page, rink, start, 22, "Stick & Puck"
                        )
                    )
                    rows.extend(
                        scrape_daysmart_filtered(
                            page, rink, start, 13, "Private Hockey Coaches Ice"
                        )
                    )
                elif rink["rink"] == "Chaparral":
                    rows.extend(
                        scrape_daysmart_filtered(
                            page, rink, start, 12, "Hockey Stick and Puck"
                        )
                    )
                elif rink["kind"] == "daysmart":
                    rows.extend(scrape_daysmart(page, rink, start, end))
                else:
                    rows.extend(scrape_pond(page, rink, start, end))
            except Exception as exc:
                rows.append({
                    "date": "",
                    "start": "",
                    "end": "",
                    "rink": rink["rink"],
                    "session": "SCRAPE ERROR",
                    "details": str(exc),
                    "source": rink["url"],
                })
            finally:
                page.close()

        browser.close()

    df = pd.DataFrame(
        rows,
        columns=["date", "start", "end", "rink", "session", "details", "source"],
    )

    if not df.empty:
        # De-duplicate conservatively and sort dated sessions first.
        df = df.drop_duplicates(
            subset=["date", "start", "end", "rink", "session"]
        )
        df["_sortdate"] = pd.to_datetime(df["date"], errors="coerce")
        df = (
            df.sort_values(["_sortdate", "start", "rink"], na_position="last")
              .drop(columns="_sortdate")
              .reset_index(drop=True)
        )
    return df


st.set_page_config(page_title="Austin Hockey Ice Finder", page_icon="🏒", layout="wide")
st.title("🏒 Austin Hockey Ice Finder")
st.caption(
    "Single-day view · Crossover, Chaparral, and The Pond"
)

today = date.today()

selected_date = st.date_input(
    "Date",
    value=today,
    help="Choose one date to view sessions at all three rinks.",
)

start_date = selected_date
end_date = selected_date

refresh = st.button("Refresh schedules", type="primary")


if refresh:
    scrape_all.clear()

with st.spinner("Loading rink calendars…"):
    df = scrape_all(start_date.isoformat(), end_date.isoformat())

errors = df[df["session"] == "SCRAPE ERROR"] if not df.empty else pd.DataFrame()
sessions = df[df["session"] != "SCRAPE ERROR"].copy() if not df.empty else df

# Always show the three configured sources and how many matches were found.
status_cols = st.columns(3)
for col, rink in zip(status_cols, RINKS):
    count = 0 if sessions.empty else int((sessions["rink"] == rink["rink"]).sum())
    col.metric(rink["rink"], f"{count} session" + ("" if count == 1 else "s"))

if not errors.empty:
    for _, row in errors.iterrows():
        st.warning(f'{row["rink"]}: {row["details"]}')

if sessions.empty:
    st.info("No matching sessions found for the selected date.")
else:
    # Show each rink separately so source accuracy is easy to verify.
    for rink in RINKS:
        rink_name = rink["rink"]
        rink_rows = sessions[sessions["rink"] == rink_name].copy()
        public_rink_rows = rink_rows[rink_rows["session"] != "POND DEBUG"].copy()

        # Sort chronologically by start time (e.g. 9 AM before 12 PM before 4 PM).
        if not public_rink_rows.empty:
            public_rink_rows["_start_sort"] = pd.to_datetime(
                public_rink_rows["start"], format="%I:%M %p", errors="coerce"
            )
            # Also support times rendered without minutes, e.g. "9 AM".
            missing_sort = public_rink_rows["_start_sort"].isna()
            if missing_sort.any():
                public_rink_rows.loc[missing_sort, "_start_sort"] = pd.to_datetime(
                    public_rink_rows.loc[missing_sort, "start"],
                    format="%I %p",
                    errors="coerce",
                )
            public_rink_rows = (
                public_rink_rows.sort_values("_start_sort", na_position="last")
                                .drop(columns="_start_sort")
            )

        st.subheader(rink_name)

        if public_rink_rows.empty:
            st.caption("No matching sessions found.")
            continue

        # Only show schedule fields useful for verification.
        display_cols = ["start", "end", "session", "source"]
        st.dataframe(
            public_rink_rows[display_cols],
            hide_index=True,
            use_container_width=True,
            column_config={
                "start": "Start",
                "end": "End",
                "session": "Session",
                "source": st.column_config.LinkColumn(
                    "Official calendar",
                    display_text="Open",
                ),
            },
        )

with st.expander("Pond feed debug"):
    pond_debug = sessions[
        (sessions["rink"] == "The Pond") &
        (sessions["session"] == "POND DEBUG")
    ] if not sessions.empty else sessions

    if pond_debug.empty:
        st.caption("No Pond feed error recorded.")
    else:
        for _, row in pond_debug.iterrows():
            st.code(row["details"])

with st.expander("Source diagnostics"):
    st.caption("Validation mode: Crossover is NETWORK-FIRST. Stick & Puck uses event_type=22; Private Hockey Coaches Ice uses event_type=13. Details begin with NETWORK JSON when the real DaySmart payload was used, or DOM FALLBACK otherwise.")
    for rink in RINKS:
        rink_name = rink["rink"]
        rink_rows = sessions[sessions["rink"] == rink_name] if not sessions.empty else sessions
        st.markdown(f"**{rink_name}: {len(rink_rows)} match(es)**")
        if not rink_rows.empty:
            for _, row in rink_rows.iterrows():
                st.code(
                    f'{row["date"]} | {row["start"]} - {row["end"]} | '
                    f'{row["session"]}\n{row["details"]}'
                )

with st.expander("Troubleshooting"):
    st.markdown(
        """
If one rink suddenly stops showing sessions, the site probably changed its
calendar markup. Run the scraper with `headless=False` in `browser =
p.chromium.launch(...)` and inspect the rendered event elements.

For Crossover, the scraper first captures DaySmart XHR/fetch/JSON responses and
parses event timestamps directly. DOM scraping is only a fallback. In Source
diagnostics, a correct network-derived row starts with `NETWORK JSON`.
"""
    )
