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
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.parse import urlencode

import pandas as pd
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


def scrape_pond(page, rink: dict, start: date, end: date) -> list[dict]:
    page.goto(rink["url"], wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(4500)

    rows = scrape_all_scopes(page, rink, start, end)

    # Keep only rows that appear to belong to the requested range when a date
    # could be extracted. Undated rows remain visible so site markup changes
    # don't silently hide valid sessions.
    filtered = []
    for row in rows:
        if not row["date"]:
            filtered.append(row)
            continue
        try:
            d = date.fromisoformat(row["date"])
            if start <= d <= end:
                filtered.append(row)
        except ValueError:
            filtered.append(row)
    return filtered


@st.cache_data(ttl=900, show_spinner=False)
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
                if rink["kind"] == "daysmart":
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
            subset=["date", "start", "end", "rink", "session", "details"]
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

        st.subheader(rink_name)

        if rink_rows.empty:
            st.caption("No matching sessions found.")
            continue

        # Only show schedule fields useful for verification.
        display_cols = ["start", "end", "session", "source"]
        st.dataframe(
            rink_rows[display_cols],
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

with st.expander("Source diagnostics"):
    st.caption("Use this only while validating scraper accuracy.")
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

The scraper deliberately searches several common calendar/event selectors and
falls back to matching rendered text, so minor site changes should usually not
require edits.
"""
    )
