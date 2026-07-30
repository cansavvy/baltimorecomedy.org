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
import time
import urllib.parse
import urllib.request
from datetime import datetime, date

# ============================== CONFIG ==============================
# Replace these with your own "Publish to web -> CSV" URLs once the forms
# and sheets exist. Leave a value as None to skip that section (it will
# be left untouched / shown as "pending" on the site).

SHOWS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTDB_QGh0L4oqe0jUFl-jvxoObctjaM2cwD4dsqtPvFJ2HBHEPggAIXCe297jxK0Dr7jvUMslWehRCL/pub?output=csv"    # "Add a Show" response sheet, published as CSV
COMEDIANS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTXiMDWyDOFeYXxdOX6KVpzMOu3yeszvBt0oQ7HlupDRuKJnWF8apg7wpYh-sPUjVBkeIcxUFBp2u4r/pub?output=csv"    # "Add a Comedian" response sheet, published as CSV
OPENMICS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vSAxpZ6jerNNfwdMnqPX3rrTN6WQ-kKOmEplH2OGiUH384XWFLB9i6-WDMXM4GzMvSlIJkjBtknnZ1Q/pub?output=csv"     # Open mic listing sheet, published as CSV

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
            "link": r.get("Link to where folks can buy tickets", "").strip() or "#",
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
        if wd is not None:
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
    # with zero one-off shows booked yet.
    months_needed = {(e["date"].year, e["date"].month) for e in dated}
    if mics_by_weekday:
        today = date.today()
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
    ("Instagram URL", "Instagram"),
    ("TikTok URL", "TikTok"),
    ("Youtube URL", "YouTube"),
    ("Facebook URL", "Facebook"),
    ("Website URL", "Website"),
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
        for field, label in SOCIAL_FIELDS:
            url = r.get(field, "").strip()
            if url:
                lines.append(f'<a href="{escape_html(url)}" target="_blank">{label}</a>')
        lines.append("</div>")
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


def resolve_openmic_link(raw: str) -> str:
    """Sheet cells here are often a bare Instagram handle (e.g.
    'cam_a_miller') rather than a full URL — turn those into real links.
    Leaves anything that's already a proper URL untouched, and drops
    anything that's neither (an email note, "check FB", etc.) rather
    than emit a broken href."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    if re.fullmatch(r"@?[A-Za-z0-9._]+", raw):
        return f"https://instagram.com/{raw.lstrip('@')}"
    return ""


def slugify(text: str) -> str:
    """Stable identifier used as a table row's id AND the calendar
    pill's link fragment, so clicking a mic on the calendar can jump to
    and highlight its actual row on the Open Mics table."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return f"mic-{slug}" if slug else "mic-unknown"


ORDINAL_WORDS = {
    "1st": 1, "first": 1,
    "2nd": 2, "second": 2,
    "3rd": 3, "third": 3,
    "4th": 4, "fourth": 4,
    "5th": 5, "fifth": 5,
    "last": -1,
}


def parse_month_occurrences(text: str):
    """
    Look for explicit "which week(s) of the month" language in a mic's
    notes, e.g. "2nd Friday of the Month", "1st and 3rd Saturday",
    "4th (4th) Sunday of Month". Returns a sorted list of occurrence
    numbers (1=first, 2=second, 3=third, 4=fourth, -1=last) if found, or
    None if the text doesn't specify — None is treated as "every week"
    by the calendar (the safest default, since most mics in practice
    are weekly; vaguer phrasing like "twice a month" without saying
    which weeks can't be placed precisely and falls back to this).
    """
    if not text:
        return None
    lowered = text.lower()
    found = {num for word, num in ORDINAL_WORDS.items() if re.search(rf"\b{re.escape(word)}\b", lowered)}
    return sorted(found) if found else None


def mic_occurs_on_day(mic: dict, day_num: int, days_in_month: int) -> bool:
    """Does this recurring mic actually happen on this specific day
    number, given its monthly-occurrence pattern (if any)?"""
    occurrences = mic.get("occurrences")
    if not occurrences:
        return True  # no specific pattern found -> assume every week
    occurrence_of_this_day = (day_num - 1) // 7 + 1  # 1st, 2nd, 3rd... of that weekday this month
    is_last_occurrence = day_num + 7 > days_in_month
    return occurrence_of_this_day in occurrences or (-1 in occurrences and is_last_occurrence)


def extract_openmic_entries(rows: list[dict]) -> list[dict]:
    """
    Parses the open mic listing sheet, handling the layout it's actually
    kept in: mics are grouped under day-header marker rows (a row whose
    first cell is literally just "TUESDAYS" etc.) rather than having an
    explicit day column repeated on every data row. Falls back to an
    explicit day/schedule column if one exists instead, for sheets built
    differently.

    Returns a list of dicts with a resolved 'weekday' (0=Monday..6=Sunday,
    matching date.weekday()) when it could be determined — None if not —
    plus an 'occurrences' list (see parse_month_occurrences) for mics
    that only happen on specific week(s) of the month rather than every
    week.
    """
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
            "slug": slugify(f"{name.strip()}-{venue or address}"),
        })

    return entries


def build_openmics_content(rows: list[dict]) -> str:
    entries = extract_openmic_entries(rows)

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
        "<th>Contact</th><th>Notes</th><th>Last Verified</th>",
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

        row_id = e["slug"]

        lines.append(
            f'<tr id="{row_id}">'
            f"<td>{name}</td>"
            f"<td>{venue}</td>"
            f"<td>{when}</td>"
            f"<td>{contact_cell}</td>"
            f"<td>{notes}</td>"
            f'<td class="last-verified">{last_verified}</td>'
            "</tr>"
        )

    lines.append("</tbody></table></div>")
    return "\n".join(lines)


# --------------------------- MAP ---------------------------

GEOCODE_CACHE_PATH = f"{OUTPUT_DIR}/geocode-cache.json"
MAX_NEW_GEOCODES_PER_RUN = 60  # safety valve; leftovers just get picked up next run


def load_geocode_cache() -> dict:
    try:
        with open(GEOCODE_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_geocode_cache(cache: dict):
    with open(GEOCODE_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def geocode_address(query: str):
    """
    Look up [lat, lng] for a free-text address using OpenStreetMap's
    Nominatim — free, no API key, but their usage policy requires: max
    ~1 request/second, and a descriptive User-Agent identifying the app
    (both handled below). Returns None if nothing was found or the
    request failed, rather than raising — a single bad address shouldn't
    break the whole map.
    """
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "q": query, "format": "json", "limit": 1,
    })
    req = urllib.request.Request(url, headers={
        "User-Agent": "BaltimoreComedyMapBot/1.0 (contact: candyscomedybaltimore@gmail.com)"
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data:
            return [float(data[0]["lat"]), float(data[0]["lon"])]
    except Exception as e:
        print(f"  [geocode] failed for {query!r}: {e}", file=sys.stderr)
    return None


def build_geocode_candidates(venue: str, address: str) -> list[str]:
    """
    Nominatim's free-form search often fails outright when a query
    combines an unlisted business name with a street address in one
    string — rather than gracefully ignoring the part it doesn't
    recognize, it just returns nothing. Try a few query shapes, most
    specific first, falling back to plainer ones:
      1. "Venue, Address" (best when the venue itself is a known POI)
      2. "Address" alone (most reliable for a real street address)
      3. "Venue" alone (helps if the address is missing or malformed)
    """
    venue = (venue or "").strip()
    address = (address or "").strip()
    candidates = []
    if venue and address:
        candidates.append(f"{venue}, {address}")
    if address:
        candidates.append(address)
    if venue:
        candidates.append(venue)
    seen = set()
    out = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def geocode_with_fallback(venue: str, address: str, cache: dict, budget: dict):
    """
    Tries build_geocode_candidates() in order, using/populating the
    shared cache dict, stopping at the first query that resolves.
    `budget` is a mutable {"remaining": N} shared across calls so the
    per-run safety cap applies across ALL candidate attempts, not per
    entry. Returns (coords_or_None, used_query_or_None).
    """
    for query in build_geocode_candidates(venue, address):
        if query in cache:
            coords = cache[query]
        elif budget["remaining"] <= 0:
            continue  # cap hit; leave ungeocoded for next run
        else:
            coords = geocode_address(query)
            cache[query] = coords
            budget["remaining"] -= 1
            budget["new_lookups"] += 1
            time.sleep(1.1)  # respect Nominatim's ~1 req/sec rate limit
        if coords:
            return coords, query
    return None, None


def build_map_data(entries: list[dict]) -> str:
    """
    Geocodes each mic's venue/address (using a cache on disk so repeat
    runs only look up NEW addresses, not all of them every time — this
    is also the caching behavior Nominatim's own usage policy asks for)
    and returns a Quarto-includable fragment: a <script> tag defining
    window.BC_MIC_LOCATIONS for the map page to plot.
    """
    cache = load_geocode_cache()
    budget = {"remaining": MAX_NEW_GEOCODES_PER_RUN, "new_lookups": 0}
    points = []

    for e in entries:
        venue = (e.get("venue") or "").strip()
        address = (e.get("address") or "").strip()
        if not venue and not address:
            continue

        coords, _used_query = geocode_with_fallback(venue, address, cache, budget)

        if coords:
            points.append({
                "name": e.get("name", ""),
                "venue": venue,
                "lat": coords[0],
                "lng": coords[1],
                "weekday_name": WEEKDAY_DISPLAY[e["weekday"]] if e.get("weekday") is not None else "",
                "time": e.get("time", ""),
                "link": f"open-mics.qmd#{e.get('slug', '')}",
            })

    if budget["new_lookups"]:
        save_geocode_cache(cache)
        print(f"  [geocode] {budget['new_lookups']} new address(es) looked up and cached")

    payload = json.dumps(points, indent=2)
    return (
        "<!--\n"
        "AUTO-GENERATED FILE — DO NOT HAND-EDIT\n"
        "Regenerated by scripts/generate_content.py. Geocoded coordinates\n"
        f"are cached in {GEOCODE_CACHE_PATH} so repeat runs only look up\n"
        "NEW addresses.\n"
        f"Last generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        "-->\n\n"
        f'<script>\nwindow.BC_MIC_LOCATIONS = {payload};\n</script>\n'
    )


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

    if SHOWS_CSV_URL or EVENTBRITE_ORGANIZER_URLS or OPENMICS_CSV_URL:
        try:
            form_rows = fetch_csv_rows(SHOWS_CSV_URL) if SHOWS_CSV_URL else []
            combined_events = normalize_form_rows(form_rows) + fetch_all_eventbrite_events(
                EVENTBRITE_ORGANIZER_URLS
            )
            openmic_entries = extract_openmic_entries(openmic_rows) if openmic_rows else []
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

    if OPENMICS_CSV_URL:
        try:
            write_file(f"{OUTPUT_DIR}/openmics-content.qmd", build_openmics_content(openmic_rows))
            any_run = True
        except Exception as e:
            print(f"[openmics] FAILED to build page content, leaving existing openmics-content.qmd untouched: {e}", file=sys.stderr)
            had_failure = True

        try:
            entries_for_map = extract_openmic_entries(openmic_rows) if openmic_rows else []
            write_file(f"{OUTPUT_DIR}/openmics-map.qmd", build_map_data(entries_for_map))
            any_run = True
        except Exception as e:
            print(f"[map] FAILED to build map data, leaving existing openmics-map.qmd untouched: {e}", file=sys.stderr)
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

    elif len(sys.argv) > 1 and sys.argv[1] == "--debug-geocode":
        if not OPENMICS_CSV_URL:
            print("OPENMICS_CSV_URL is not set at the top of this script — nothing to fetch.", file=sys.stderr)
            sys.exit(1)
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
        print(f"Fetching {OPENMICS_CSV_URL} ...")
        rows = fetch_csv_rows(OPENMICS_CSV_URL, header_hint="Name")
        entries = extract_openmic_entries(rows)
        print(f"Geocoding the first {n} of {len(entries)} mic(s), not using the cache, showing each fallback attempt:\n")
        for e in entries[:n]:
            candidates = build_geocode_candidates(e.get("venue", ""), e.get("address", ""))
            print(f"  {e['name']!r}")
            found = False
            for query in candidates:
                coords = geocode_address(query)
                print(f"    try {query!r} -> {coords}")
                time.sleep(1.1)
                if coords:
                    found = True
                    break
            if not found:
                print("    (none of the fallback queries resolved)")
            print()

    else:
        main()
