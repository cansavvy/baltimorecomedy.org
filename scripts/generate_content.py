#!/usr/bin/env python3
"""
generate_content.py

Pulls data from published Google Sheets (Google Form responses + the open
mic listing) and regenerates the auto-generated Quarto content fragments
in /_generated. This is the engine behind "just approve it and the site
updates itself."

HOW THE APPROVAL WORKFLOW WORKS
--------------------------------
1. Someone fills out the "Add a Show" or "Add a Comedian" Google Form.
2. Their response lands as a new row in the linked Google Sheet.
3. Candace reviews it and types "Yes" in an "Approved" column she keeps
   on that sheet (the form doesn't create this column -- add it once by
   hand, it's just a normal spreadsheet column).
4. This script (run manually or on a schedule via GitHub Actions) pulls
   the sheet as CSV, keeps only Approved == "Yes" rows, and rewrites the
   matching file in /_generated. Quarto then renders those includes into
   shows.qmd / comedians.qmd / open-mics.qmd.
5. Nothing is ever deleted from the sheet -- rows just don't render until
   Approved is set to Yes.

SETTING THIS UP
---------------
For each Google Sheet (Shows responses, Comedians responses, Open Mic
listing), publish it to the web as CSV:
  File -> Share -> Publish to web -> select the sheet/tab -> CSV -> Publish
Paste the resulting URL into the CONFIG section below.

Each published CSV URL only exposes read-only data Google already serves
publicly once "Publish to web" is turned on -- no API key or auth needed.
"""

import calendar
import csv
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, date

# ============================== CONFIG ==============================
# Replace these with your own "Publish to web -> CSV" URLs once the forms
# and sheets exist. Leave a value as None to skip that section (it will
# be left untouched / shown as "pending" on the site).

SHOWS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTDB_QGh0L4oqe0jUFl-jvxoObctjaM2cwD4dsqtPvFJ2HBHEPggAIXCe297jxK0Dr7jvUMslWehRCL/pub?output=csv"    # "Add a Show" response sheet, published as CSV
COMEDIANS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTXiMDWyDOFeYXxdOX6KVpzMOu3yeszvBt0oQ7HlupDRuKJnWF8apg7wpYh-sPUjVBkeIcxUFBp2u4r/pub?output=csv"    # "Add a Comedian" response sheet, published as CSV
MEDIA_CSV_URL = "https://docs.google.com/spreadsheets/d/11UCtXbXNO0zXAQaZ3FW1IlPnCcGsJ0A-kVeP5P_slag/export?format=csv&gid=581388853"    # "Add a Podcast/Book/Media" response sheet
OPENMICS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSAxpZ6jerNNfwdMnqPX3rrTN6WQ-kKOmEplH2OGiUH384XWFLB9i6-WDMXM4GzMvSlIJkjBtknnZ1Q/pub?output=csv"     # Open mic listing sheet, published as CSV
ACCESSIBILITY_CSV_URL = "https://docs.google.com/spreadsheets/d/1YPzNTj1Qk50UyutI2YLxbLmHn7Pum6zO8nONxnHGt2s/export?format=csv&gid=1785766944"     # Your own venue-accessibility sheet (Venue / Rating / Notes columns)

# Eventbrite organizer pages to pull events from automatically, e.g.:
#   "https://www.eventbrite.com/o/119257059441"
# Add as many as you like. See the big warning in fetch_eventbrite_organizer_events()
# below about how reliable this is (short version: best-effort, not an
# official API, may need retuning if Eventbrite changes their site).
#
# The actual URLs live in scripts/eventbrite_urls.txt (one per line) so
# they can be edited without touching this file — see load_eventbrite_urls().
EVENTBRITE_URLS_FILE = os.path.join(os.path.dirname(__file__), "eventbrite_urls.txt")


def load_eventbrite_urls() -> list[str]:
    try:
        with open(EVENTBRITE_URLS_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    return [
        line.strip() for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]


EVENTBRITE_ORGANIZER_URLS: list[str] = load_eventbrite_urls()

OUTPUT_DIR = "_generated"

# ======================================================================


def fetch_csv_rows(url: str, header_hint: str = None) -> list[dict]:
    """Download a published-to-web Google Sheet CSV and return rows as dicts.

    Most sheets have their real column headers on row 1, and the default
    behavior (header_hint=None) assumes that. Some sheets — like the open
    mic listing, which has a few lines of banner/instruction text above
    the actual table — don't. Pass header_hint (e.g. "Name") to instead
    scan for the first row that contains that value among its cells and
    treat THAT as the real header row, ignoring anything above it.
    """
    with urllib.request.urlopen(url, timeout=30) as response:
        raw = response.read().decode("utf-8-sig")

    if header_hint:
        all_rows = list(csv.reader(io.StringIO(raw)))
        header_idx = next(
            (i for i, row in enumerate(all_rows)
             if any(cell.strip().lower() == header_hint.lower() for cell in row)),
            None,
        )
        if header_idx is not None:
            headers = [h.strip() for h in all_rows[header_idx]]
            return [
                {
                    (headers[i] if i < len(headers) and headers[i] else f"col_{i}"): (cell or "").strip()
                    for i, cell in enumerate(row)
                }
                for row in all_rows[header_idx + 1:]
            ]
        # header_hint not found anywhere — fall through to the plain
        # row-1-is-the-header behavior below rather than fail outright.

    reader = csv.DictReader(io.StringIO(raw))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]


def is_approved(row: dict) -> bool:
    val = (row.get("Approved") or "").strip().lower()
    return val in ("yes", "y", "true", "approved")


def is_open_mic(row: dict) -> bool:
    val = (row.get("Is this an Open Mic?") or "").strip().lower()
    return val in ("yes", "y", "true")


def escape_html(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def parse_date_safe(value: str):
    """Try a handful of common date formats; return a date object or None."""
    value = value.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


# --------------------------- SHOWS ---------------------------

def normalize_form_rows(rows: list[dict]) -> list[dict]:
    """Turn approved Shows-form rows into the common normalized shape
    used by build_shows_content(). Rows flagged "Is this an Open Mic?" =
    Yes are excluded here — they go to the Open Mics page instead (see
    build_submitted_openmics_content)."""
    out = []
    for r in rows:
        if not is_approved(r) or is_open_mic(r):
            continue
        recurring = r.get(
            "Is this a recurring show? If so, how frequently? Weekly? Monthly?", ""
        ).strip()
        tag = recurring if recurring and recurring.lower() not in ("no", "n/a", "none", "") else ""
        out.append({
            "name": r.get("Show name", "").strip() or "Untitled Show",
            "venue": r.get("Venue", "").strip(),
            "date": parse_date_safe(r.get("Date", "")),
            "link": resolve_link(r.get("Link to where folks can buy tickets", "")) or "#",
            "tag": tag,
            "source": "form",
        })
    return out


def extract_start_raw(node: dict):
    """Try a bunch of the field-name shapes Eventbrite-style embedded
    JSON commonly uses for an event's start date/time. Checked in
    order; first non-empty match wins."""
    candidates = [
        node.get("start_date"),
        node.get("startDate"),
        node.get("start_time"),
        node.get("startTime"),
        node.get("date"),
        node.get("eventDate"),
    ]
    start_node = node.get("start")
    if isinstance(start_node, dict):
        candidates.extend([
            start_node.get("utc"),
            start_node.get("local"),
            start_node.get("date"),
        ])
    elif isinstance(start_node, str):
        candidates.append(start_node)
    for c in candidates:
        if c:
            return c
    return None


def fetch_eventbrite_organizer_events(organizer_url: str) -> list[dict]:
    """
    Best-effort pull of upcoming events from a public Eventbrite organizer
    page, e.g. https://www.eventbrite.com/o/119257059441

    IMPORTANT — READ BEFORE RELYING ON THIS:
    Eventbrite's official API only exposes an organizer's OWN events to
    THAT organizer's own OAuth token — there is no supported public API
    for reading arbitrary other organizers' events. Eventbrite's public
    organizer profile pages are also JavaScript-rendered (a Next.js app),
    so the event list isn't necessarily present in the plain HTML the way
    it would be on a simple server-rendered page.

    This function works by requesting the page's HTML and searching for
    a large embedded JSON blob Next.js apps typically ship (commonly in a
    <script id="__NEXT_DATA__"> tag) that the page uses to hydrate itself
    client-side, then heuristically searching that JSON for anything that
    looks like an event (an object with a "url" containing "/e/" — every
    Eventbrite event page follows that pattern — alongside a name and a
    date). This is inherently fragile: if Eventbrite changes their page
    structure, this may return nothing, and needs re-tuning. It also may
    be against Eventbrite's Terms of Service depending on how it's used —
    worth checking before relying on this for anything beyond casual,
    low-volume personal use.

    If this stops finding events, the most reliable fallback is to ask
    each organizer directly for an RSS/iCal export link if they have one
    enabled, or their own private API token if they're willing to share
    it — both are officially supported and far more stable than scraping.
    """
    req = urllib.request.Request(
        organizer_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BaltimoreComedyBot/1.0)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            html = response.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  [eventbrite] failed to fetch {organizer_url}: {e}", file=sys.stderr)
        return []

    blob = None
    match = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL
    )
    if match:
        try:
            blob = json.loads(match.group(1))
        except json.JSONDecodeError:
            blob = None

    if blob is None:
        # Fall back to any schema.org JSON-LD blocks, in case the page
        # includes those instead of/alongside __NEXT_DATA__.
        ld_blocks = re.findall(
            r'<script type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL
        )
        parsed = []
        for block in ld_blocks:
            try:
                parsed.append(json.loads(block))
            except json.JSONDecodeError:
                continue
        blob = parsed if parsed else None

    if blob is None:
        print(
            f"  [eventbrite] no embedded event data found on {organizer_url} "
            "— page structure may have changed, or events are loaded via a "
            "call this script doesn't replicate.",
            file=sys.stderr,
        )
        return []

    found = {}  # keyed by url, to dedupe
    raw_samples = []  # first few raw matched nodes, for --debug-eventbrite

    def walk(node):
        if isinstance(node, dict):
            url = node.get("url") or node.get("eventUrl") or node.get("event_url")
            name = node.get("name") or node.get("title")
            if isinstance(url, str) and "/e/" in url and name:
                if url not in found:
                    start_raw = extract_start_raw(node)
                    venue = ""
                    v = node.get("venue") or node.get("primary_venue") or node.get("location")
                    if isinstance(v, dict):
                        venue = v.get("name", "") or ""
                    elif isinstance(v, str):
                        venue = v
                    found[url] = {
                        "name": str(name).strip(),
                        "venue": str(venue).strip(),
                        "date": parse_eventbrite_date(start_raw),
                        "link": url,
                        "tag": "",
                        "source": "eventbrite",
                    }
                    if len(raw_samples) < 3:
                        raw_samples.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(blob)

    events = list(found.values())
    print(f"  [eventbrite] {organizer_url} -> {len(events)} event(s) found")

    if os.environ.get("EVENTBRITE_DEBUG") and raw_samples:
        print("\n  [eventbrite debug] raw keys on first matched event node:")
        print(" ", sorted(raw_samples[0].keys()))
        date_like = {
            k: v for k, v in raw_samples[0].items()
            if any(w in k.lower() for w in ("date", "start", "time"))
        }
        print("  [eventbrite debug] date-ish fields on that node:", date_like)

    return events


def parse_eventbrite_date(value):
    if not value or not isinstance(value, str):
        return None
    v = value.strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(v[: len(fmt) + 8], fmt).date()
        except ValueError:
            continue
    return parse_date_safe(value)


def fetch_all_eventbrite_events(organizer_urls: list[str]) -> list[dict]:
    all_events = []
    for url in organizer_urls:
        all_events.extend(fetch_eventbrite_organizer_events(url))
    return all_events


def build_shows_content(events: list[dict], openmic_entries: list[dict] = None) -> str:
    """events: list of normalized dicts with name/venue/date/link/tag/source
    (see normalize_form_rows and fetch_eventbrite_organizer_events).
    openmic_entries: optional list from extract_openmic_entries() — these
    are recurring (weekly, not date-specific) and get overlaid onto every
    matching weekday in the rendered calendar, in a distinct color from
    one-off shows.
    Renders one calendar-style month grid per month that has shows or (if
    no dated shows exist yet) a rolling window of upcoming months so
    recurring mics still have somewhere to show up, plus a small list at
    the end for anything without a confirmed date."""
    openmic_entries = openmic_entries or []
    mics_by_weekday: dict = {}
    for m in openmic_entries:
        wd = m.get("weekday")
        if wd is not None and not is_mic_paused(m.get("notes", "")):
            mics_by_weekday.setdefault(wd, []).append(m)

    dated = [e for e in events if e.get("date")]
    undated = [e for e in events if not e.get("date")]
    dated.sort(key=lambda e: e["date"])

    if not dated and not undated and not mics_by_weekday:
        return (
            "::: {.aside-note}\n"
            "No approved shows yet. Once rows are marked Approved in the "
            "sheet, they'll appear here automatically.\n"
            ":::\n"
        )

    lines = [
        "<!--",
        "AUTO-GENERATED FILE — DO NOT HAND-EDIT",
        "Regenerated by scripts/generate_content.py from approved Shows-form",
        "rows, configured Eventbrite organizer pages, and (in a different",
        "color) recurring weekly open mics.",
        f"Last generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "-->",
        "",
    ]

    # Which (year, month) grids to render: every month a dated show falls
    # in, PLUS — if there are recurring mics to show — a rolling window
    # starting this month, so mics still have a calendar even in months
    # with zero one-off shows booked yet. Past months are always
    # excluded (below), so this doesn't need re-cleaning every month —
    # a stale past-dated show in the feed also won't resurrect an old
    # month's grid.
    today = date.today()
    current_ym = (today.year, today.month)
    months_needed = {
        (e["date"].year, e["date"].month) for e in dated
        if (e["date"].year, e["date"].month) >= current_ym
    }
    if mics_by_weekday:
        y, mo = today.year, today.month
        for _ in range(3):
            months_needed.add((y, mo))
            mo += 1
            if mo > 12:
                mo = 1
                y += 1

    for (year, month) in sorted(months_needed):
        by_day: dict = {}
        for e in dated:
            if e["date"].year == year and e["date"].month == month:
                by_day.setdefault(e["date"].day, []).append(e)

        month_label = date(year, month, 1).strftime("%B %Y")
        lines.append(f"### {month_label}\n")
        lines.append('<div class="cal-wrap">')
        lines.append('<div class="cal-grid">')

        for dow in ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]:
            lines.append(f'<div class="cal-dow">{dow}</div>')

        # calendar.monthrange gives weekday of the 1st with Monday=0..Sunday=6;
        # convert to a Sunday-first offset for a standard US calendar layout.
        first_weekday, days_in_month = calendar.monthrange(year, month)
        leading_blanks = (first_weekday + 1) % 7

        for _ in range(leading_blanks):
            lines.append('<div class="cal-day empty"></div>')

        for day_num in range(1, days_in_month + 1):
            day_events = by_day.get(day_num, [])
            this_weekday = date(year, month, day_num).weekday()  # Mon=0..Sun=6
            day_mics = mics_by_weekday.get(this_weekday, [])

            # Color-coding lives on the small event pills (.cal-show /
            # .cal-mic) only, not on the day cell itself.
            lines.append('<div class="cal-day">')
            lines.append(f'<span class="cal-daynum">{day_num}</span>')

            for e in day_events:
                name = escape_html(e.get("name", "Untitled Show"))
                venue = escape_html(e.get("venue", ""))
                link = e.get("link") or "#"
                tag = e.get("tag", "")
                full_title = f"{name} — {venue}" if venue else name
                if tag:
                    full_title += f" ({tag})"
                lines.append(
                    f'<a class="cal-show" href="{escape_html(link)}" target="_blank" title="{escape_html(full_title)}">{name}</a>'
                )

            for m in day_mics:
                if not mic_occurs_on_day(m, day_num, days_in_month):
                    continue
                name = escape_html(m.get("name", "Open Mic"))
                venue = escape_html(m.get("venue", ""))
                mic_time = m.get("time", "")
                full_title = f"{name} — {venue}" if venue else name
                if mic_time:
                    full_title += f" ({mic_time})"
                if m.get("notes"):
                    full_title += f" — {m['notes']}"
                row_slug = m.get("slug") or slugify(f"{m.get('name','')}-{m.get('venue','')}")
                lines.append(
                    f'<a class="cal-mic" href="open-mics.qmd#{row_slug}" title="{escape_html(full_title)}">{name}</a>'
                )

            lines.append("</div>")

        # Trailing blanks so the grid fills out to a full last row.
        trailing = (7 - (leading_blanks + days_in_month) % 7) % 7
        for _ in range(trailing):
            lines.append('<div class="cal-day empty"></div>')

        lines.append("</div>")  # .cal-grid
        lines.append("</div>\n")  # .cal-wrap

    if mics_by_weekday:
        lines.append(
            '<p class="cal-legend"><span class="cal-legend-swatch cal-legend-show"></span> One-off shows '
            '&nbsp;&nbsp;<span class="cal-legend-mic-item"><span class="cal-legend-swatch cal-legend-mic"></span> Recurring open mics</span></p>\n'
        )

    if undated:
        lines.append("### Date TBD\n")
        lines.append('<ul class="show-list">\n')
        for e in undated:
            name = escape_html(e.get("name", "Untitled Show"))
            venue = escape_html(e.get("venue", ""))
            link = e.get("link") or "#"
            label = f"{name} — {venue}" if venue else name
            tag = e.get("tag", "")
            tag_html = f" <em>({escape_html(tag)})</em>" if tag else ""
            lines.append('<li class="show-item">')
            lines.append(
                f'<span class="show-name"><a href="{escape_html(link)}" target="_blank">{label}</a>{tag_html}</span>'
            )
            lines.append('<span class="show-date">TBD</span>')
            lines.append("</li>\n")
        lines.append("</ul>")

    return "\n".join(lines)


# --------------------------- COMEDIANS ---------------------------

SOCIAL_FIELDS = [
    ("Instagram URL", "Instagram", "instagram"),
    ("TikTok URL", "TikTok", "tiktok"),
    ("Youtube URL", "YouTube", "youtube"),
    ("Facebook URL", "Facebook", "facebook"),
    ("Website URL", "Website", None),
]


def extract_drive_file_id(url: str):
    """Pull a Google Drive file ID out of the URL shapes a Form's file-
    upload question typically produces in the response sheet, e.g.:
      https://drive.google.com/open?id=FILE_ID
      https://drive.google.com/file/d/FILE_ID/view?usp=drivesdk
      https://drive.google.com/uc?id=FILE_ID
    Returns None if it doesn't look like a Drive link at all.
    """
    if not url:
        return None
    m = re.search(r"/d/([a-zA-Z0-9_-]{15,})", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]{15,})", url)
    if m:
        return m.group(1)
    return None


def drive_thumbnail_url(file_id: str, width: int = 500) -> str:
    """
    Google's (unofficial but widely used) endpoint for rendering a Drive
    file as an image directly, no download needed. Only works if the
    file — or, more usefully, the whole Drive folder Google Forms saves
    uploads into — is shared as "Anyone with the link can view". That's
    a ONE-TIME setting on the folder, not something to redo per photo:
      Google Drive -> find the form's upload folder (usually named after
      the form) -> right-click -> Share -> General access -> "Anyone
      with the link" -> Viewer.
    If that folder is still restricted to "Only me", this URL will come
    back broken/blank for site visitors even though it works fine when
    Candace is logged in and looks at it herself — that's the first
    thing to check if photos aren't showing.
    """
    return f"https://drive.google.com/thumbnail?id={file_id}&sz=w{width}"


def resolve_comedian_photo(row: dict) -> str:
    # Manual override always wins, for when Drive hotlinking isn't
    # working or a nicer/cropped photo has been dropped in by hand.
    manual = row.get("Photo Filename", "").strip()
    if manual:
        return f"images/{manual}" if "/" not in manual else manual

    headshot_url = row.get("Headshot", "").strip()
    file_id = extract_drive_file_id(headshot_url)
    if file_id:
        return drive_thumbnail_url(file_id)

    return "images/placeholder-headshot.svg"


def build_comedians_content(rows: list[dict]) -> str:
    approved = [r for r in rows if is_approved(r)]
    approved.sort(key=lambda r: r.get("Stage Name", "").strip().lower())

    if not approved:
        return (
            "::: {.aside-note}\n"
            "No approved comedian profiles yet. Once rows are marked "
            "Approved in the sheet, they'll appear here automatically.\n"
            ":::\n"
        )

    lines = [
        "<!--",
        "AUTO-GENERATED FILE — DO NOT HAND-EDIT",
        "Regenerated by scripts/generate_content.py. Edit the source sheet",
        "and mark rows Approved instead.",
        f"Last generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "-->",
        "",
        '<div class="comedian-grid">',
        "",
    ]

    for r in approved:
        name = escape_html(r.get("Stage Name", "").strip() or "Unnamed")
        tag = escape_html(r.get("Comedic Style (short sentence descriptor)", "").strip())
        bio = ""  # this form doesn't currently collect a separate bio field
        # Try Drive hotlinking first (see resolve_comedian_photo), then a
        # manual "Photo Filename" override, then the placeholder image.
        photo = resolve_comedian_photo(r)

        lines.append('<div class="comedian-card">')
        lines.append(f'<img class="comedian-photo" src="{escape_html(photo)}" alt="{name}" loading="lazy" onerror="this.onerror=null;this.src=\'images/placeholder-headshot.svg\';">')
        lines.append(f'<div class="comedian-name">{name}</div>')
        if tag:
            lines.append(f'<div class="comedian-tag">{tag}</div>')
        if bio:
            lines.append(f'<p class="comedian-bio">{bio}</p>')
        lines.append('<div class="social-links">')
        for field, label, platform in SOCIAL_FIELDS:
            url = resolve_link(r.get(field, ""), platform)
            if url:
                lines.append(f'<a href="{escape_html(url)}" target="_blank">{label}</a>')
        lines.append("</div>")
        lines.append("</div>\n")

    lines.append("</div>")
    return "\n".join(lines)


# --------------------------- MEDIA (PODCASTS, BOOKS, ETC.) ---------------------------

def resolve_media_cover(row: dict) -> str:
    """Same pattern as resolve_comedian_photo: a manual filename
    override always wins, otherwise try hotlinking a Google Form file
    upload from Drive, otherwise fall back to a placeholder."""
    manual = row.get("Cover Filename", "").strip()
    if manual:
        return f"images/{manual}" if "/" not in manual else manual

    cover_url = pick_field(row, "Image of podcast/media", "Cover Image")
    file_id = extract_drive_file_id(cover_url)
    if file_id:
        return drive_thumbnail_url(file_id)

    return "images/placeholder-cover.svg"


def build_media_content(rows: list[dict]) -> str:
    approved = [r for r in rows if is_approved(r)]
    approved.sort(key=lambda r: pick_field(r, "Podcast/Media name", "Title").strip().lower())

    if not approved:
        return (
            "::: {.aside-note}\n"
            "No approved podcasts, books, or media yet. Once rows are marked "
            "Approved in the sheet, they'll appear here automatically.\n"
            ":::\n"
        )

    lines = [
        "<!--",
        "AUTO-GENERATED FILE — DO NOT HAND-EDIT",
        "Regenerated by scripts/generate_content.py. Edit the source sheet",
        "and mark rows Approved instead.",
        f"Last generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "-->",
        "",
        '<div class="media-grid">',
        "",
    ]

    for r in approved:
        title = escape_html(pick_field(r, "Podcast/Media name", "Title") or "Untitled")
        # Note: the form only collects the creator's EMAIL (for internal
        # contact), not a public display name — same as "Email for
        # showrunner" on the Shows form, this stays private and isn't
        # shown on the card.
        description = escape_html(pick_field(r, "Brief description of the podcast/media", "Description"))
        link = resolve_link(pick_field(r, "Link to where it can be found", "Link"))
        cover = resolve_media_cover(r)

        lines.append('<div class="media-card">')
        lines.append(f'<img class="media-cover" src="{escape_html(cover)}" alt="{title}" loading="lazy" onerror="this.onerror=null;this.src=\'images/placeholder-cover.svg\';">')
        lines.append(f'<div class="media-title">{title}</div>')
        if description:
            lines.append(f'<p class="media-description">{description}</p>')
        if link:
            lines.append(f'<a class="cta-button" href="{escape_html(link)}" target="_blank">Listen / View →</a>')
        lines.append("</div>\n")

    lines.append("</div>")
    return "\n".join(lines)


# --------------------------- OPEN MICS ---------------------------

def build_submitted_openmics_content(shows_rows: list[dict]) -> str:
    """
    Rows from the SHOWS form where "Is this an Open Mic?" = Yes get their
    own small table on the Open Mics page, separate from the main
    hand-curated listing (which comes from a different source sheet).
    """
    submitted = [r for r in shows_rows if is_approved(r) and is_open_mic(r)]

    if not submitted:
        return (
            "<!-- no community-submitted open mics yet -->\n"
        )

    lines = [
        "<!--",
        "AUTO-GENERATED FILE — DO NOT HAND-EDIT",
        "Regenerated by scripts/generate_content.py from the Shows form",
        'responses where "Is this an Open Mic?" = Yes.',
        f"Last generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "-->",
        "",
        "### Recently Submitted\n",
        '<div class="openmic-table-wrap">',
        '<table class="openmic-table">',
        "<thead><tr><th>Open Mic</th><th>Venue</th><th>Day / Time</th>"
        "<th>Contact</th><th>Notes</th><th>Last Verified</th></tr></thead>",
        "<tbody>",
    ]

    for r in submitted:
        name = escape_html(r.get("Show name", "Untitled"))
        venue = escape_html(r.get("Venue", ""))
        d = parse_date_safe(r.get("Date", ""))
        when = d.strftime("%a, %b %-d") if d else ""
        recurring = r.get(
            "Is this a recurring show? If so, how frequently? Weekly? Monthly?", ""
        ).strip()
        when_label = f"{recurring} ({when})" if recurring and when else (recurring or when or "—")
        link = r.get("Link to where folks can buy tickets", "").strip()
        contact = f'<a href="{escape_html(link)}" target="_blank">link</a>' if link else "—"
        submitted_date = date.today().strftime("%Y-%m-%d")

        lines.append(
            "<tr>"
            f"<td>{name}</td>"
            f"<td>{venue}</td>"
            f"<td>{escape_html(when_label)}</td>"
            f"<td>{contact}</td>"
            f"<td>—</td>"
            f'<td class="last-verified">{submitted_date}</td>'
            "</tr>"
        )

    lines.append("</tbody></table></div>")
    return "\n".join(lines)


WEEKDAY_LOOKUP = {
    "MONDAY": 0, "MONDAYS": 0,
    "TUESDAY": 1, "TUESDAYS": 1,
    "WEDNESDAY": 2, "WEDNESDAYS": 2,
    "THURSDAY": 3, "THURSDAYS": 3,
    "FRIDAY": 4, "FRIDAYS": 4,
    "SATURDAY": 5, "SATURDAYS": 5,
    "SUNDAY": 6, "SUNDAYS": 6,
}
WEEKDAY_DISPLAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def pick_field(row: dict, *candidates):
    """Be tolerant of whatever column names a source sheet actually uses."""
    for c in candidates:
        if c in row and row[c]:
            return row[c]
    return ""


def weekday_from_free_text(text: str):
    """Fallback for sheets that DO have an explicit per-row day/schedule
    column (e.g. 'Tuesdays 7pm') instead of day-header marker rows."""
    if not text:
        return None
    upper = text.upper()
    for name, idx in WEEKDAY_LOOKUP.items():
        if name in upper:
            return idx
    return None


PLATFORM_DOMAINS = {
    "instagram": "instagram.com",
    "tiktok": "tiktok.com",
    "youtube": "youtube.com",
    "facebook": "facebook.com",
}


def resolve_link(raw: str, platform: str = None) -> str:
    """
    Normalize a submitted link/handle into a real, usable URL. People
    submitting through a form very often type just a bare handle
    ("cam_a_miller" or "@cam_a_miller") instead of a full link — this
    turns those into something that actually works as an href, rather
    than emitting a broken relative link.

    - Already a full http(s):// URL -> used as-is.
    - Already "platform.com/..." for the matching platform, just
      missing the protocol -> "https://" gets added, not re-wrapped.
    - A bare handle -> built into a real URL for the given `platform`
      ("instagram", "tiktok", "youtube", or "facebook"). Checked BEFORE
      the domain pattern below when a platform is known, since some
      real handles contain a dot (e.g. "AlexAndJillian.zip") and would
      otherwise be misread as a website domain.
    - Without a platform given, a bare handle can't be safely resolved
      — there's no way to know which site it's for — so it's dropped
      rather than guessed at wrong.
    - Something that looks like a bare domain with no protocol (e.g.
      "example.com" or "www.example.com") -> "https://" gets prepended.
      Only checked when there's no platform context, or the text didn't
      match as a handle for that platform.
    - Anything else that doesn't parse as any of the above (an email
      address, "check the FB page", etc.) -> dropped rather than
      emitting a broken link.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw

    if platform and platform in PLATFORM_DOMAINS:
        domain = PLATFORM_DOMAINS[platform]
        if raw.lower().startswith(domain) or raw.lower().startswith(f"www.{domain}"):
            return f"https://{raw}"

    looks_like_handle = re.fullmatch(r"@?[A-Za-z0-9._]+", raw)
    looks_like_domain = re.fullmatch(r"([A-Za-z0-9-]+\.)+[A-Za-z]{2,}(/\S*)?", raw)

    if platform and platform in PLATFORM_DOMAINS:
        if looks_like_handle or looks_like_domain:
            handle = raw.lstrip("@")
            if platform == "youtube":
                return f"https://youtube.com/@{handle}"
            return f"https://{PLATFORM_DOMAINS[platform]}/{handle}"
        return ""

    if looks_like_domain:
        return f"https://{raw}"

    return ""


def resolve_openmic_link(raw: str) -> str:
    """Thin wrapper kept for existing call sites — open mic sheet
    columns are Instagram-context specifically, see resolve_link()."""
    return resolve_link(raw, platform="instagram")


def slugify(text: str) -> str:
    """Stable identifier used as a table row's id AND the calendar
    pill's link fragment, so clicking a mic on the calendar can jump to
    and highlight its actual row on the Open Mics table."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return f"mic-{slug}" if slug else "mic-unknown"


def fetch_accessibility_lookup(url: str) -> dict:
    """
    Fetch a small, separately-maintained sheet (yours, not the main
    open mic list) rating each VENUE's wheelchair accessibility — keyed
    by venue alone, not by individual mic, since accessibility is a
    property of the physical location (one venue hosting several
    different mic nights shares a single rating). Built for the case
    where you don't have edit access to the main list, so this data
    lives entirely in something you control — no re-syncing needed,
    since it's fetched fresh alongside the main list every run.

    Expected columns: Venue, Rating (also recognizes Accessibility /
    Wheelchair Accessible / Accessible / ADA), and optionally Notes —
    extra detail beyond the short tier, shown as a tooltip on the
    site's badge.

    Returns {venue_slug: {"tier": normalized_tier, "note": note_text}}.
    """
    try:
        rows = fetch_csv_rows(url)
    except Exception as e:
        print(f"[accessibility] FAILED to fetch accessibility sheet: {e}", file=sys.stderr)
        return {}

    lookup = {}
    for row in rows:
        venue = pick_field(row, "Venue", "Place", "Location")
        rating_raw = pick_field(row, "Rating", "Accessibility", "Wheelchair Accessible", "Accessible", "ADA")
        note = pick_field(row, "Notes", "Note", "Details")
        if not venue or not rating_raw:
            continue
        key = slugify(venue)
        lookup[key] = {"tier": normalize_accessibility(rating_raw), "note": note}

    print(f"  [accessibility] loaded {len(lookup)} entr{'y' if len(lookup)==1 else 'ies'} from your accessibility sheet")
    return lookup


ORDINAL_WORDS = {
    "1st": 1, "first": 1,
    "2nd": 2, "second": 2,
    "3rd": 3, "third": 3,
    "4th": 4, "fourth": 4,
    "5th": 5, "fifth": 5,
    "last": -1,
}

BIWEEKLY_PATTERNS = [
    r"every other",
    r"bi-?weekly",
    r"\b2x a month\b",
    r"\btwice a month\b",
]

PAUSED_PATTERNS = [
    r"\bhiatus\b",
    r"\bpaused?\b",
    r"\bon hold\b",
    r"\btbd\b",
    r"\bcancell?ed\b",
    r"\bdiscontinued\b",
]


def is_mic_paused(text: str) -> bool:
    """Notes like '(on hiatus) (TBD)' mean this mic isn't actually
    running right now — it shouldn't show up as a recurring weekly
    event on the calendar at all until it's back."""
    if not text:
        return False
    lowered = text.lower()
    return any(re.search(p, lowered) for p in PAUSED_PATTERNS)


def parse_month_occurrences(text: str):
    """
    Figure out which week(s) of the month a recurring mic actually
    happens on, from its notes text. Three shapes of real-world
    phrasing, checked in order:

    1. Explicit ordinals — "2nd Friday of the Month", "1st and 3rd
       Saturday", "4th (4th) Sunday of Month" — returns exactly those
       occurrence numbers (1=first, 2=second, 3=third, 4=fourth,
       -1=last).
    2. "Every other [day]" / "bi-weekly" / "2x a month" / "twice a
       month" with no specific weeks named — there's no way to know
       from text alone whether that means odd or even weeks, so this
       assumes the 1st and 3rd occurrence, the most common convention
       for alternating-week schedules. Not guaranteed to match the
       real schedule exactly (a true biweekly cycle drifts across
       month boundaries in a way "1st and 3rd of the month" doesn't
       perfectly replicate), but far closer than showing it every
       single week.
    3. Nothing recognizable — returns None, meaning "every week" (the
       safe default for genuinely weekly mics, and the fallback for
       anything too vague to place more precisely).
    """
    if not text:
        return None
    lowered = text.lower()

    explicit = {num for word, num in ORDINAL_WORDS.items() if re.search(rf"\b{re.escape(word)}\b", lowered)}
    if explicit:
        return sorted(explicit)

    if any(re.search(p, lowered) for p in BIWEEKLY_PATTERNS):
        return [1, 3]

    return None


def mic_occurs_on_day(mic: dict, day_num: int, days_in_month: int) -> bool:
    """Does this recurring mic actually happen on this specific day
    number, given its monthly-occurrence pattern (if any)?"""
    occurrences = mic.get("occurrences")
    if not occurrences:
        return True  # no specific pattern found -> assume every week
    occurrence_of_this_day = (day_num - 1) // 7 + 1  # 1st, 2nd, 3rd... of that weekday this month
    is_last_occurrence = day_num + 7 > days_in_month
    return occurrence_of_this_day in occurrences or (-1 in occurrences and is_last_occurrence)


ACCESSIBILITY_TIERS = {
    "confirmed": "Confirmed",
    "yes": "Confirmed",
    "likely": "Likely",
    "probably": "Likely",
    "uncertain": "Uncertain",
    "unknown": "Uncertain",
    "unclear": "Uncertain",
    "not accessible": "Likely not accessible",
    "no": "Likely not accessible",
    "not likely": "Likely not accessible",
    "inaccessible": "Likely not accessible",
}


def normalize_accessibility(raw: str) -> str:
    """Sheet input for this column is free text someone's typing by
    hand — be forgiving about exact wording rather than requiring one
    precise phrase. Falls back to showing whatever was typed verbatim
    if it doesn't match a known tier, rather than silently dropping it."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    return ACCESSIBILITY_TIERS.get(raw.lower(), raw)


def extract_openmic_entries(rows: list[dict], accessibility_lookup: dict = None) -> list[dict]:
    """
    Parses the open mic listing sheet, handling the layout it's actually
    kept in: mics are grouped under day-header marker rows (a row whose
    first cell is literally just "TUESDAYS" etc.) rather than having an
    explicit day column repeated on every data row. Falls back to an
    explicit day/schedule column if one exists instead, for sheets built
    differently.

    accessibility_lookup: optional {venue_slug: {"tier", "note"}} dict
    from fetch_accessibility_lookup() — used when you don't have edit
    access to this sheet yourself, so accessibility data lives in a
    separate sheet you do control, keyed by venue (not by individual
    mic, since accessibility belongs to the building). Takes priority
    over an Accessibility/Rating column on this sheet itself if both
    happen to have data for the same venue.

    Returns a list of dicts with a resolved 'weekday' (0=Monday..6=Sunday,
    matching date.weekday()) when it could be determined — None if not —
    plus an 'occurrences' list (see parse_month_occurrences) for mics
    that only happen on specific week(s) of the month rather than every
    week.
    """
    accessibility_lookup = accessibility_lookup or {}
    current_weekday = None
    entries = []

    for row in rows:
        values = [v for v in row.values() if v is not None]
        first_val = (values[0] if values else "").strip()

        marker_weekday = WEEKDAY_LOOKUP.get(first_val.upper())
        if marker_weekday is not None:
            current_weekday = marker_weekday
            continue  # this row is a section header, not a mic

        name = pick_field(row, "Open Mic Name", "Name", "Mic") or first_val
        if not name.strip():
            continue  # blank spacer row

        explicit_day_text = pick_field(row, "Day", "Day / Time", "Schedule")
        weekday = current_weekday
        if weekday is None and explicit_day_text:
            weekday = weekday_from_free_text(explicit_day_text)

        notes = pick_field(row, "Notes/Details", "Notes", "Details")
        raw_link = pick_field(row, "Link", "Website", "Instagram")
        other_raw = pick_field(row, "Other")
        venue = pick_field(row, "Venue", "Location", "Place")
        address = pick_field(row, "Address")
        slug = slugify(f"{name.strip()}-{venue or address}")

        venue_key = slugify(venue or address)
        access_data = accessibility_lookup.get(venue_key)
        if access_data:
            accessibility = access_data["tier"]
            accessibility_note = access_data.get("note", "")
        else:
            accessibility = normalize_accessibility(
                pick_field(row, "Rating", "Accessibility", "Wheelchair Accessible", "Accessible", "ADA")
            )
            accessibility_note = ""

        entries.append({
            "name": name.strip(),
            "venue": venue or address,
            "address": address,
            "time": pick_field(row, "Time"),
            "weekday": weekday,
            "occurrences": parse_month_occurrences(notes),
            "notes": notes,
            "last_verified": pick_field(row, "Last Verified", "Last Checked"),
            "link": resolve_openmic_link(raw_link),
            "other": other_raw,
            "other_link": resolve_openmic_link(other_raw),
            "accessibility": accessibility,
            "accessibility_note": accessibility_note,
            "slug": slug,
        })

    return entries


def build_openmics_content(rows: list[dict], accessibility_lookup: dict = None) -> str:
    entries = extract_openmic_entries(rows, accessibility_lookup)

    if not entries:
        return (
            "::: {.aside-note}\n"
            "Open mic data hasn't been connected yet. Set OPENMICS_CSV_URL "
            "in scripts/generate_content.py once the sheet is published.\n"
            ":::\n"
        )

    today_str = date.today().strftime("%Y-%m-%d")

    lines = [
        "<!--",
        "AUTO-GENERATED FILE — DO NOT HAND-EDIT",
        "Regenerated by scripts/generate_content.py from the open mic",
        "listing sheet.",
        f"Last generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "-->",
        "",
        '<div class="openmic-table-wrap">',
        '<table class="openmic-table">',
        "<thead><tr>",
        "<th>Open Mic</th><th>Venue</th><th>Day / Time</th>"
        "<th>Contact</th><th>Notes</th><th>Accessibility</th><th>Last Verified</th>",
        "</tr></thead>",
        "<tbody>",
    ]

    for e in entries:
        name = escape_html(e["name"])
        venue = escape_html(e["venue"])
        day_name = WEEKDAY_DISPLAY[e["weekday"]] if e["weekday"] is not None else ""
        when = escape_html(" ".join(part for part in [day_name, e["time"]] if part).strip()) or "—"
        notes = escape_html(e["notes"]) or "—"
        last_verified = escape_html(e["last_verified"]) or today_str

        # Link the venue straight to a Google Maps search for its
        # address — no geocoding, no API key, just a URL. Falls back to
        # the venue name alone if there's no separate street address.
        map_query = ", ".join(part for part in [e.get("venue", ""), e.get("address", "")] if part) or e.get("address", "")
        if map_query:
            maps_url = "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(map_query)
            venue_cell = f'<a href="{maps_url}" target="_blank">{venue}</a>' if venue else f'<a href="{maps_url}" target="_blank">Map</a>'
        else:
            venue_cell = venue or "—"

        # Contact: primary link (Instagram/website) plus a secondary
        # contact (the sheet's "Other" column) if there is one and it's
        # not just a duplicate of the primary.
        contact_parts = []
        if e["link"]:
            contact_parts.append(f'<a href="{escape_html(e["link"])}" target="_blank">Instagram</a>')
        other = (e.get("other") or "").strip()
        if other:
            if e.get("other_link") and e["other_link"] != e["link"]:
                contact_parts.append(f'<a href="{escape_html(e["other_link"])}" target="_blank">{escape_html(other)}</a>')
            elif not e.get("other_link"):
                contact_parts.append(escape_html(other))
        contact_cell = " · ".join(contact_parts) if contact_parts else "—"

        # Accessibility badge — reads whatever's in the sheet's own
        # Accessibility/Wheelchair Accessible column (see
        # normalize_accessibility). No data in the sheet yet = "Unknown"
        # rather than a guess.
        access_value = e.get("accessibility") or "Unknown"
        access_class = {
            "Confirmed": "access-confirmed",
            "Likely": "access-likely",
            "Likely not accessible": "access-no",
        }.get(access_value, "access-unknown")
        access_note = e.get("accessibility_note", "")
        access_title = f' title="{escape_html(access_note)}"' if access_note else ""
        access_cell = f'<span class="access-badge {access_class}"{access_title}>{escape_html(access_value)}</span>'

        row_id = e["slug"]

        lines.append(
            f'<tr id="{row_id}">'
            f"<td>{name}</td>"
            f"<td>{venue_cell}</td>"
            f"<td>{when}</td>"
            f"<td>{contact_cell}</td>"
            f"<td>{notes}</td>"
            f"<td>{access_cell}</td>"
            f'<td class="last-verified">{last_verified}</td>'
            "</tr>"
        )

    lines.append("</tbody></table></div>")

    lines.append(
        '<p class="access-legend">'
        '<span class="access-badge access-confirmed">Confirmed</span> the venue\'s own listing says so '
        '&nbsp;&nbsp;<span class="access-badge access-likely">Likely</span> no listing, but the venue type suggests it '
        '&nbsp;&nbsp;<span class="access-badge access-unknown">Unknown</span> not yet checked '
        '&nbsp;&nbsp;<span class="access-badge access-no">Likely not accessible</span> known barriers (stairs-only entry, etc.) '
        "<br>This is self-reported/best-effort info, not independently verified — call ahead to confirm before you go."
        "</p>\n"
    )

    return "\n".join(lines)


# --------------------------- MAIN ---------------------------

def write_file(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")
    print(f"wrote {path}")


def main():
    any_run = False
    had_failure = False

    # Fetched once, up front, since both the Shows calendar (recurring
    # mic overlay) and the Open Mics page table need the same data.
    openmic_rows = []
    if OPENMICS_CSV_URL:
        try:
            openmic_rows = fetch_csv_rows(OPENMICS_CSV_URL, header_hint="Name")
        except Exception as e:
            print(f"[openmics] FAILED to fetch sheet: {e}", file=sys.stderr)
            had_failure = True

    accessibility_lookup = {}
    if ACCESSIBILITY_CSV_URL:
        try:
            accessibility_lookup = fetch_accessibility_lookup(ACCESSIBILITY_CSV_URL)
        except Exception as e:
            print(f"[accessibility] FAILED to fetch accessibility sheet: {e}", file=sys.stderr)
            had_failure = True

    if SHOWS_CSV_URL or EVENTBRITE_ORGANIZER_URLS or OPENMICS_CSV_URL:
        try:
            form_rows = fetch_csv_rows(SHOWS_CSV_URL) if SHOWS_CSV_URL else []
            combined_events = normalize_form_rows(form_rows) + fetch_all_eventbrite_events(
                EVENTBRITE_ORGANIZER_URLS
            )
            openmic_entries = extract_openmic_entries(openmic_rows, accessibility_lookup) if openmic_rows else []
            write_file(
                f"{OUTPUT_DIR}/shows-content.qmd",
                build_shows_content(combined_events, openmic_entries),
            )
            if SHOWS_CSV_URL:
                write_file(
                    f"{OUTPUT_DIR}/openmics-submitted.qmd",
                    build_submitted_openmics_content(form_rows),
                )
            any_run = True
        except Exception as e:
            print(f"[shows] FAILED, leaving existing shows-content.qmd untouched: {e}", file=sys.stderr)
            had_failure = True

    if COMEDIANS_CSV_URL:
        try:
            rows = fetch_csv_rows(COMEDIANS_CSV_URL)
            write_file(f"{OUTPUT_DIR}/comedians-content.qmd", build_comedians_content(rows))
            any_run = True
        except Exception as e:
            print(f"[comedians] FAILED, leaving existing comedians-content.qmd untouched: {e}", file=sys.stderr)
            had_failure = True

    if MEDIA_CSV_URL:
        try:
            rows = fetch_csv_rows(MEDIA_CSV_URL)
            write_file(f"{OUTPUT_DIR}/media-content.qmd", build_media_content(rows))
            any_run = True
        except Exception as e:
            print(f"[media] FAILED, leaving existing media-content.qmd untouched: {e}", file=sys.stderr)
            had_failure = True

    if OPENMICS_CSV_URL:
        try:
            write_file(f"{OUTPUT_DIR}/openmics-content.qmd", build_openmics_content(openmic_rows, accessibility_lookup))
            any_run = True
        except Exception as e:
            print(f"[openmics] FAILED to build page content, leaving existing openmics-content.qmd untouched: {e}", file=sys.stderr)
            had_failure = True

    if not any_run and not had_failure:
        print(
            "Nothing configured yet — nothing to do. Set SHOWS_CSV_URL / "
            "COMEDIANS_CSV_URL / OPENMICS_CSV_URL at the top of "
            "scripts/generate_content.py, and/or add organizer URLs to "
            "scripts/eventbrite_urls.txt.",
            file=sys.stderr,
        )

    # Deliberately always exit 0 (success), even if a section above failed.
    # A failed section leaves its existing _generated/*.qmd file untouched
    # (see the try/except blocks above) rather than blank or broken, so
    # there's always something valid to render and deploy. Exiting
    # non-zero here would stop the GitHub Actions workflow's later render
    # + deploy steps entirely — worse than just shipping slightly stale
    # content for the one section that had trouble.


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--debug-eventbrite":
        if len(sys.argv) < 3:
            print("Usage: python3 generate_content.py --debug-eventbrite <organizer_url>", file=sys.stderr)
            sys.exit(1)
        test_url = sys.argv[2]
        print(f"Fetching {test_url} ...")
        events = fetch_eventbrite_organizer_events(test_url)
        if not events:
            print(
                "\nNo events extracted. This means either:\n"
                "  1. Eventbrite blocked the request (bot detection), or\n"
                "  2. The page's embedded data doesn't match the patterns\n"
                "     this script looks for (__NEXT_DATA__ / JSON-LD), or\n"
                "  3. This organizer genuinely has no upcoming events listed.\n"
                "Share this output if you want help adjusting the script."
            )
        else:
            print(f"\nExtracted {len(events)} event(s):\n")
            for e in events:
                print(f"  - {e['name']!r} | venue={e['venue']!r} | date={e['date']} | {e['link']}")

    elif len(sys.argv) > 1 and sys.argv[1] == "--debug-openmics":
        if not OPENMICS_CSV_URL:
            print("OPENMICS_CSV_URL is not set at the top of this script — nothing to fetch.", file=sys.stderr)
            sys.exit(1)
        print(f"Fetching {OPENMICS_CSV_URL} ...\n")
        try:
            rows = fetch_csv_rows(OPENMICS_CSV_URL, header_hint="Name")
        except Exception as e:
            print(f"FAILED to fetch the sheet: {e}", file=sys.stderr)
            sys.exit(1)

        print(f"Fetched {len(rows)} raw row(s) from the sheet.")
        if rows:
            print(f"Column headers found: {list(rows[0].keys())}\n")
        else:
            print("The sheet came back completely empty — check the URL and that it's published as CSV.\n")

        entries = extract_openmic_entries(rows)
        with_weekday = [e for e in entries if e["weekday"] is not None]
        without_weekday = [e for e in entries if e["weekday"] is None]

        print(f"Parsed {len(entries)} mic entr{'y' if len(entries)==1 else 'ies'} total.")
        print(f"  -> {len(with_weekday)} resolved to a specific weekday (these WILL show on the calendar)")
        print(f"  -> {len(without_weekday)} did NOT resolve to a weekday (these will NOT show on the calendar)\n")

        if with_weekday:
            print("Entries that resolved correctly (first 10 shown):")
            for e in with_weekday[:10]:
                occ = f", occurrences={e['occurrences']}" if e.get("occurrences") else ""
                print(f"  - {e['name']!r} -> {WEEKDAY_DISPLAY[e['weekday']]}{occ}")
            print()

        if without_weekday:
            print("Entries that did NOT resolve a weekday (first 10 shown) — these are why mics are missing:")
            for e in without_weekday[:10]:
                print(f"  - {e['name']!r} (venue={e['venue']!r})")
            print()

        print("First 10 raw rows exactly as read from the sheet, for inspection:")
        for r in rows[:10]:
            print(" ", dict(r))

    elif len(sys.argv) > 1 and sys.argv[1] == "--debug-accessibility":
        if not ACCESSIBILITY_CSV_URL:
            print("ACCESSIBILITY_CSV_URL is not set at the top of this script.", file=sys.stderr)
            sys.exit(1)
        print(f"Fetching {ACCESSIBILITY_CSV_URL} ...\n")
        try:
            raw_rows = fetch_csv_rows(ACCESSIBILITY_CSV_URL)
        except Exception as e:
            print(f"FAILED to fetch: {e}", file=sys.stderr)
            print(
                "\nThis usually means the sheet isn't actually public yet. The "
                "/export?format=csv URL only works if sharing is set to "
                "'Anyone with the link' (Viewer) — or, more reliably, use "
                "File -> Share -> Publish to web -> CSV instead, which always "
                "works regardless of the file's regular sharing settings.",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"Fetched {len(raw_rows)} raw row(s).")
        if raw_rows:
            print(f"Column headers found: {list(raw_rows[0].keys())}\n")
            print("First 5 raw rows exactly as read:")
            for r in raw_rows[:5]:
                print(" ", dict(r))
        else:
            print(
                "\nZero rows came back. If fetching 'succeeded' but returned "
                "nothing usable, the URL is likely serving an HTML sign-in "
                "page instead of real CSV data — a strong sign the sheet "
                "isn't actually shared publicly. Try 'Publish to web -> CSV' "
                "instead of the plain /export link."
            )

        lookup = fetch_accessibility_lookup(ACCESSIBILITY_CSV_URL)
        print(f"\nBuilt a lookup with {len(lookup)} venue(s).")
        if lookup:
            print("Sample entries:")
            for k, v in list(lookup.items())[:5]:
                print(f"  {k} -> {v}")

    elif len(sys.argv) > 1 and sys.argv[1] == "--debug-media":
        if not MEDIA_CSV_URL:
            print("MEDIA_CSV_URL is not set at the top of this script.", file=sys.stderr)
            sys.exit(1)
        print(f"Fetching {MEDIA_CSV_URL} ...\n")
        try:
            raw_rows = fetch_csv_rows(MEDIA_CSV_URL)
        except Exception as e:
            print(f"FAILED to fetch: {e}", file=sys.stderr)
            print(
                "\nThis usually means the sheet isn't actually public yet. The "
                "/export?format=csv URL only works if sharing is set to "
                "'Anyone with the link' (Viewer) — or, more reliably, use "
                "File -> Share -> Publish to web -> CSV instead, which always "
                "works regardless of the file's regular sharing settings.",
                file=sys.stderr,
            )
            sys.exit(1)

        print(f"Fetched {len(raw_rows)} raw row(s).")
        if raw_rows:
            print(f"Column headers found: {list(raw_rows[0].keys())}\n")
            print("All rows exactly as read:")
            for r in raw_rows:
                print(" ", dict(r))
        else:
            print(
                "\nZero rows came back. If fetching 'succeeded' but returned "
                "nothing usable, the URL is likely serving an HTML sign-in "
                "page instead of real CSV data — a strong sign the sheet "
                "isn't actually shared publicly. Try 'Publish to web -> CSV' "
                "instead of the plain /export link."
            )

        approved = [r for r in raw_rows if is_approved(r)]
        print(f"\n{len(approved)} of {len(raw_rows)} row(s) are marked Approved (these are the only ones that show on the site).")

    else:
        main()
