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


def scrape_rendered_events(page, rink: dict, start: date, end: date) -> list[dict]:
    """
    Scrape likely calendar event nodes first, then fall back to rendered text blocks.
    This intentionally uses several common calendar selectors so small front-end
    changes are less likely to break the scraper.
    """
    selectors = [
        ".fc-event",
        ".fc-event-main",
        ".fc-list-event",
        "[data-event-id]",
        "[class*='calendar'] a",
        "[class*='event']",
        "a",
    ]

    seen = set()
    rows = []

    for selector in selectors:
        try:
            nodes = page.locator(selector)
            count = min(nodes.count(), 1500)
        except Exception:
            continue

        for i in range(count):
            try:
                node = nodes.nth(i)
                text = norm(node.inner_text(timeout=500))
                if not text or not contains_wanted(text, rink["wanted"]):
                    continue

                href = ""
                try:
                    href = node.get_attribute("href") or ""
                except Exception:
                    pass

                # Include parent context because calendar tiles often put the
                # date/time in a parent/sibling instead of the title element.
                context = text
                try:
                    parent_text = norm(node.locator("xpath=..").inner_text(timeout=500))
                    if len(parent_text) <= 700:
                        context = parent_text
                except Exception:
                    pass

                event_date = date_from_text(context, start.year)
                start_time, end_time = times_from_text(context)

                key = (rink["rink"], event_date, start_time, end_time, text.lower())
                if key in seen:
                    continue
                seen.add(key)

                rows.append({
                    "date": event_date or "",
                    "start": start_time,
                    "end": end_time,
                    "rink": rink["rink"],
                    "session": clean_title(text, rink["wanted"]).title(),
                    "details": context,
                    "source": href or page.url,
                })
            except Exception:
                continue

    # Last fallback: scan short rendered lines containing target names.
    if not rows:
        body = norm(page.locator("body").inner_text(timeout=5000))
        chunks = re.split(r"[\n\r]+", body)
        for chunk in chunks:
            chunk = norm(chunk)
            if 3 <= len(chunk) <= 500 and contains_wanted(chunk, rink["wanted"]):
                event_date = date_from_text(chunk, start.year)
                start_time, end_time = times_from_text(chunk)
                key = (rink["rink"], event_date, start_time, end_time, chunk.lower())
                if key not in seen:
                    seen.add(key)
                    rows.append({
                        "date": event_date or "",
                        "start": start_time,
                        "end": end_time,
                        "rink": rink["rink"],
                        "session": clean_title(chunk, rink["wanted"]).title(),
                        "details": chunk,
                        "source": page.url,
                    })

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
    page.wait_for_timeout(2500)

    return scrape_rendered_events(page, rink, start, end)


def scrape_pond(page, rink: dict, start: date, end: date) -> list[dict]:
    page.goto(rink["url"], wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_load_state("networkidle", timeout=12_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(3000)

    rows = scrape_rendered_events(page, rink, start, end)

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
    "Crossover: Private Hockey Coaches Ice + Stick & Puck · "
    "Chaparral: Hockey Stick and Puck · "
    "The Pond: Barn Time + Pond Time"
)

today = date.today()
c1, c2, c3 = st.columns([1, 1, 1])
with c1:
    start_date = st.date_input("From", value=today)
with c2:
    end_date = st.date_input("Through", value=today + timedelta(days=7))
with c3:
    st.write("")
    st.write("")
    refresh = st.button("Refresh schedules", type="primary", use_container_width=True)

if end_date < start_date:
    st.error("'Through' must be on or after 'From'.")
    st.stop()

if refresh:
    scrape_all.clear()

with st.spinner("Loading rink calendars…"):
    df = scrape_all(start_date.isoformat(), end_date.isoformat())

errors = df[df["session"] == "SCRAPE ERROR"] if not df.empty else pd.DataFrame()
sessions = df[df["session"] != "SCRAPE ERROR"].copy() if not df.empty else df

if not errors.empty:
    for _, row in errors.iterrows():
        st.warning(f'{row["rink"]}: {row["details"]}')

if sessions.empty:
    st.info("No matching sessions found for this date range.")
else:
    rink_filter = st.multiselect(
        "Rinks",
        options=list(dict.fromkeys(sessions["rink"].tolist())),
        default=list(dict.fromkeys(sessions["rink"].tolist())),
    )
    shown = sessions[sessions["rink"].isin(rink_filter)].copy()

    # Main one-page view.
    st.dataframe(
        shown[["date", "start", "end", "rink", "session", "details", "source"]],
        hide_index=True,
        use_container_width=True,
        column_config={
            "date": "Date",
            "start": "Start",
            "end": "End",
            "rink": "Rink",
            "session": "Session",
            "details": st.column_config.TextColumn("Details", width="large"),
            "source": st.column_config.LinkColumn("Open calendar", display_text="Open"),
        },
    )

    st.download_button(
        "Download CSV",
        shown.to_csv(index=False).encode("utf-8"),
        file_name=f"hockey_sessions_{start_date}_{end_date}.csv",
        mime="text/csv",
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
