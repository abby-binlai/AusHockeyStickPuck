"""Austin Hockey Schedule — public Streamlit app."""
from __future__ import annotations

import html
import re
from datetime import date, datetime, timedelta
from typing import Iterable
from urllib.parse import urlencode

import pandas as pd
import streamlit as st
from playwright.sync_api import PlaywrightTimeoutError, sync_playwright

RINKS = [
    {
        "rink": "Crossover",
        "kind": "daysmart",
        "url": "https://apps.daysmartrecreation.com/dash/x/iceandfield/calendar",
        "wanted": ["private hockey coaches ice", "stick & puck", "stick and puck"],
    },
    {
        "rink": "Chaparral",
        "kind": "daysmart",
        "url": "https://apps.daysmartrecreation.com/dash/x/chaparralice/calendar",
        "wanted": ["hockey stick and puck", "hockey stick & puck"],
    },
    {
        "rink": "The Pond",
        "kind": "pond",
        "url": "https://www.pondhockeyclub.com/page/show/2723404-rink-schedules",
        "wanted": ["barn time", "pond time"],
    },
]


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def contains_wanted(text: str, wanted: Iterable[str]) -> bool:
    t = norm(text).lower()
    return any(w.lower() in t for w in wanted)


def extract_date(text: str, default_year: int) -> str:
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})/(\d{1,2})\b", text)
    if m:
        return f"{default_year:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    months = "Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
    m = re.search(rf"\b({months})\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?\b", text, re.I)
    if m:
        month = datetime.strptime(m.group(1)[:3], "%b").month
        year = int(m.group(3)) if m.group(3) else default_year
        return f"{year:04d}-{month:02d}-{int(m.group(2)):02d}"
    return ""


def extract_times(text: str) -> tuple[str, str]:
    matches = re.findall(r"\b(\d{1,2}(?::\d{2})?\s*(?:AM|PM|am|pm))\b", text)
    matches = [norm(x).upper() for x in matches]
    return (matches[0] if matches else "", matches[1] if len(matches) > 1 else "")


def session_name(text: str, wanted: Iterable[str]) -> str:
    lower = text.lower()
    for w in wanted:
        pos = lower.find(w.lower())
        if pos >= 0:
            found = text[pos : pos + len(w)]
            return re.sub(r"\bAnd\b", "and", found.title())
    return norm(text)[:100]


def scrape_rendered_events(page, rink: dict, start: date, end: date) -> list[dict]:
    selectors = [
        ".fc-event", ".fc-event-main", ".fc-list-event", "[data-event-id]",
        "[class*='event']", "[class*='calendar'] a", "a",
    ]
    seen, rows = set(), []

    for selector in selectors:
        try:
            nodes = page.locator(selector)
            count = min(nodes.count(), 1800)
        except Exception:
            continue

        for i in range(count):
            try:
                node = nodes.nth(i)
                text = norm(node.inner_text(timeout=400))
                if not text or not contains_wanted(text, rink["wanted"]):
                    continue
                context = text
                try:
                    parent = norm(node.locator("xpath=..").inner_text(timeout=400))
                    if len(parent) <= 800:
                        context = parent
                except Exception:
                    pass
                href = ""
                try:
                    href = node.get_attribute("href") or ""
                except Exception:
                    pass
                event_date = extract_date(context, start.year)
                start_time, end_time = extract_times(context)
                name = session_name(text, rink["wanted"])
                key = (event_date, start_time, end_time, rink["rink"], name.lower(), context.lower())
                if key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "date": event_date,
                    "start": start_time,
                    "end": end_time,
                    "rink": rink["rink"],
                    "session": name,
                    "details": context,
                    "source": href or page.url,
                })
            except Exception:
                continue

    if not rows:
        try:
            body = page.locator("body").inner_text(timeout=5000)
            for chunk in re.split(r"[\n\r]+", body):
                chunk = norm(chunk)
                if not (3 <= len(chunk) <= 600 and contains_wanted(chunk, rink["wanted"])):
                    continue
                event_date = extract_date(chunk, start.year)
                start_time, end_time = extract_times(chunk)
                name = session_name(chunk, rink["wanted"])
                key = (event_date, start_time, end_time, rink["rink"], name.lower(), chunk.lower())
                if key not in seen:
                    seen.add(key)
                    rows.append({"date": event_date, "start": start_time, "end": end_time,
                                 "rink": rink["rink"], "session": name, "details": chunk, "source": page.url})
        except Exception:
            pass
    return rows


def scrape_rink(page, rink: dict, start: date, end: date) -> list[dict]:
    if rink["kind"] == "daysmart":
        params = urlencode({"start": start.isoformat(), "end": end.isoformat()})
        url = f'{rink["url"]}?{params}'
    else:
        url = rink["url"]
    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    try:
        page.wait_for_load_state("networkidle", timeout=10_000)
    except PlaywrightTimeoutError:
        pass
    page.wait_for_timeout(2200)
    rows = scrape_rendered_events(page, rink, start, end)
    filtered = []
    for row in rows:
        if row["date"]:
            try:
                if not start <= date.fromisoformat(row["date"]) <= end:
                    continue
            except ValueError:
                pass
        filtered.append(row)
    return filtered


@st.cache_data(ttl=900, show_spinner=False)
def scrape_all(start_iso: str, end_iso: str) -> pd.DataFrame:
    start, end = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    rows = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 1440, "height": 1100}, locale="en-US",
            timezone_id="America/Chicago",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136 Safari/537.36",
        )
        for rink in RINKS:
            page = context.new_page()
            try:
                rows.extend(scrape_rink(page, rink, start, end))
            except Exception as exc:
                rows.append({"date": "", "start": "", "end": "", "rink": rink["rink"],
                             "session": "SCRAPE ERROR", "details": str(exc), "source": rink["url"]})
            finally:
                page.close()
        browser.close()

    df = pd.DataFrame(rows, columns=["date", "start", "end", "rink", "session", "details", "source"])
    if not df.empty:
        df = df.drop_duplicates(subset=["date", "start", "end", "rink", "session", "details"])
        df["_d"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.sort_values(["_d", "start", "rink"], na_position="last").drop(columns="_d").reset_index(drop=True)
    return df


def pretty_time(start: str, end: str) -> str:
    if start and end:
        return f"{start} – {end}"
    return start or end or "Time on rink calendar"


def render_card(row: pd.Series) -> None:
    rink = html.escape(str(row["rink"]))
    session = html.escape(str(row["session"]))
    time_text = html.escape(pretty_time(str(row["start"]), str(row["end"])))
    url = html.escape(str(row["source"]), quote=True)
    st.markdown(
        f'''<div class="session-card">
          <div class="session-time">{time_text}</div>
          <div class="session-title">{session}</div>
          <div class="session-rink">{rink}</div>
          <a class="source-link" href="{url}" target="_blank">Official calendar ↗</a>
        </div>''',
        unsafe_allow_html=True,
    )


st.set_page_config(page_title="Austin Hockey Schedule", page_icon="🏒", layout="centered")
st.markdown("""
<style>
.block-container {max-width: 880px; padding-top: 1.6rem; padding-bottom: 4rem;}
.hero {padding: .35rem 0 .6rem;}
.hero h1 {font-size: 2.05rem; margin-bottom: .15rem;}
.hero p {opacity:.72; margin-top:0;}
.day-heading {font-size: 1.05rem; font-weight: 800; margin: 1.6rem 0 .6rem; letter-spacing:.02em;}
.session-card {border:1px solid rgba(128,128,128,.25); border-radius:14px; padding:15px 17px; margin:9px 0;}
.session-time {font-size:.92rem; font-weight:700; opacity:.78;}
.session-title {font-size:1.13rem; font-weight:800; margin-top:3px;}
.session-rink {font-size:.96rem; margin-top:2px;}
.source-link {display:inline-block; font-size:.82rem; margin-top:8px; text-decoration:none; opacity:.72;}
.small-note {opacity:.66; font-size:.85rem;}
@media (max-width: 600px) {.block-container {padding-left:1rem; padding-right:1rem;} .hero h1 {font-size:1.65rem;}}
</style>
<div class="hero"><h1>🏒 Austin Hockey Schedule</h1><p>Stick & puck and open hockey ice from three Austin-area rinks, in one place.</p></div>
""", unsafe_allow_html=True)

today = date.today()
with st.expander("Filters", expanded=False):
    c1, c2 = st.columns(2)
    start_date = c1.date_input("From", value=today)
    end_date = c2.date_input("Through", value=today + timedelta(days=6))
    rink_filter = st.multiselect("Rinks", [r["rink"] for r in RINKS], default=[r["rink"] for r in RINKS])
    refresh = st.button("Refresh schedule", type="primary", use_container_width=True)

if end_date < start_date:
    st.error("The end date must be on or after the start date.")
    st.stop()
if (end_date - start_date).days > 31:
    st.warning("Please choose a range of 31 days or less.")
    st.stop()
if refresh:
    scrape_all.clear()

with st.spinner("Checking rink calendars…"):
    df = scrape_all(start_date.isoformat(), end_date.isoformat())

errors = df[df["session"] == "SCRAPE ERROR"] if not df.empty else pd.DataFrame()
sessions = df[df["session"] != "SCRAPE ERROR"].copy() if not df.empty else df
sessions = sessions[sessions["rink"].isin(rink_filter)] if not sessions.empty else sessions

if not errors.empty:
    st.info("One or more rink calendars could not be read right now. The other results are shown below.")

if sessions.empty:
    st.info("No matching sessions found for this date range.")
else:
    dated = sessions[sessions["date"] != ""].copy()
    undated = sessions[sessions["date"] == ""].copy()
    for day_iso, group in dated.groupby("date", sort=True):
        try:
            d = date.fromisoformat(day_iso)
            heading = d.strftime("%A, %B %d").replace(" 0", " ")
        except ValueError:
            heading = day_iso
        st.markdown(f'<div class="day-heading">{html.escape(heading)}</div>', unsafe_allow_html=True)
        for _, row in group.iterrows():
            render_card(row)
    if not undated.empty:
        st.markdown('<div class="day-heading">Date not detected</div>', unsafe_allow_html=True)
        st.caption("These matched a requested session name, but the source page did not expose a date beside the event.")
        for _, row in undated.iterrows():
            render_card(row)

    st.download_button("Download CSV", sessions.to_csv(index=False).encode("utf-8"),
                       file_name=f"austin_hockey_{start_date}_{end_date}.csv", mime="text/csv")

st.markdown('<p class="small-note">Schedules can change. Confirm on the rink’s official calendar before driving over.</p>', unsafe_allow_html=True)
