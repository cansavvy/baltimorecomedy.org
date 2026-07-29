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

import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, date

# ============================== CONFIG ==============================
# Replace these with your own "Publish to web -> CSV" URLs once the forms
# and sheets exist. Leave a value as None to skip that section (it will
# be left untouched / shown as "pending" on the site).

SHOWS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTDB_QGh0L4oqe0jUFl-jvxoObctjaM2cwD4dsqtPvFJ2HBHEPggAIXCe297jxK0Dr7jvUMslWehRCL/pub?output=csv"  # "Add a Show" response sheet, published as CSV
COMEDIANS_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vTXiMDWyDOFeYXxdOX6KVpzMOu3yeszvBt0oQ7HlupDRuKJnWF8apg7wpYh-sPUjVBkeIcxUFBp2u4r/pub?output=csv"  # "Add a Comedian" response sheet, published as CSV
OPENMICS_CSV_URL = None  # Open mic listing sheet, published as CSV

# Eventbrite organizer pages to pull events from automatically, e.g.:
#   "https://www.eventbrite.com/o/119257059441"
# Add as many as you like. See the big warning in fetch_eventbrite_organizer_events()
# below about how reliable this is (short version: best-effort, not an
# official API, may need retuning if Eventbrite changes their site).
EVENTBRITE_ORGANIZER_URLS: list[str] = [
    "https://www.eventbrite.com/o/5340822879",
    "https://www.eventbrite.com/o/32042122709",
    "https://www.eventbrite.com/o/119257059441",
]

OUTPUT_DIR = "_generated"

# ======================================================================


def fetch_csv_rows(url: str) -> list[dict]:
    """Download a published-to-web Google Sheet CSV and return rows as dicts."""
    with urllib.request.urlopen(url, timeout=30) as response:
        raw = response.read().decode("utf-8-sig")
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


def build_shows_content(events: list[dict]) -> str:
    """events: list of normalized dicts with name/venue/date/link/tag/source
    (see normalize_form_rows and fetch_eventbrite_organizer_events)."""
    dated = [(e.get("date"), e) for e in events]
    # Undated rows sort last, but still get included
    dated.sort(key=lambda pair: (pair[0] is None, pair[0] or date.max))

    if not dated:
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
        "rows and configured Eventbrite organizer pages.",
        f"Last generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "-->",
        "",
    ]

    current_month = None
    for d, e in dated:
        month_label = d.strftime("%B %Y") if d else "Date TBD"
        if month_label != current_month:
            if current_month is not None:
                lines.append("</ul>\n")
            lines.append(f"### {month_label}\n")
            lines.append('<ul class="show-list">\n')
            current_month = month_label

        name = escape_html(e.get("name", "Untitled Show"))
        venue = escape_html(e.get("venue", ""))
        link = e.get("link") or "#"
        day_label = d.strftime("%a, %b %-d") if d else "TBD"
        label = f"{name} — {venue}" if venue else name

        tag = e.get("tag", "")
        tag_html = f" <em>({escape_html(tag)})</em>" if tag else ""

        lines.append('<li class="show-item">')
        lines.append(
            f'<span class="show-name"><a href="{escape_html(link)}" target="_blank">{label}</a>{tag_html}</span>'
        )
        lines.append(f'<span class="show-date">{day_label}</span>')
        lines.append("</li>\n")

    lines.append("</ul>")
    return "\n".join(lines)


# --------------------------- COMEDIANS ---------------------------

SOCIAL_FIELDS = [
    ("Instagram URL", "📷", "Instagram"),
    ("TikTok URL", "🎵", "TikTok"),
    ("Youtube URL", "▶️", "YouTube"),
    ("Facebook URL", "📘", "Facebook"),
    ("Website URL", "🌐", "Website"),
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
        for field, emoji, label in SOCIAL_FIELDS:
            url = r.get(field, "").strip()
            if url:
                lines.append(f'<a href="{escape_html(url)}" target="_blank" title="{label}">{emoji}</a>')
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


def build_openmics_content(rows: list[dict]) -> str:
    if not rows:
        return (
            "::: {.aside-note}\n"
            "Open mic data hasn't been connected yet. Set OPENMICS_CSV_URL "
            "in scripts/generate_content.py once the sheet is published.\n"
            ":::\n"
        )

    today_str = date.today().strftime("%Y-%m-%d")

    # Be tolerant of whatever column names the source sheet actually uses.
    def pick(row, *candidates):
        for c in candidates:
            if c in row and row[c]:
                return row[c]
        return ""

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
        "<th>Sign-up</th><th>Cost</th><th>Last Verified</th>",
        "</tr></thead>",
        "<tbody>",
    ]

    for r in rows:
        name = escape_html(pick(r, "Open Mic Name", "Name", "Mic"))
        venue = escape_html(pick(r, "Venue", "Location", "Address"))
        when = escape_html(pick(r, "Day / Time", "Day", "Time", "Schedule"))
        signup = escape_html(pick(r, "Sign-up", "Sign Up", "How to Sign Up"))
        cost = escape_html(pick(r, "Cost", "Price")) or "—"
        # If the source sheet already tracks its own verification date,
        # respect it. Otherwise stamp today's date as of this sync.
        last_verified = pick(r, "Last Verified", "Last Checked") or today_str

        link = pick(r, "Link", "Website", "Instagram")
        name_cell = f'<a href="{escape_html(link)}" target="_blank">{name}</a>' if link else name

        lines.append(
            "<tr>"
            f"<td>{name_cell}</td>"
            f"<td>{venue}</td>"
            f"<td>{when}</td>"
            f"<td>{signup}</td>"
            f"<td>{cost}</td>"
            f'<td class="last-verified">{escape_html(last_verified)}</td>'
            "</tr>"
        )

    lines.append("</tbody></table></div>")
    return "\n".join(lines)


# --------------------------- MAIN ---------------------------

def write_file(path: str, content: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content.rstrip() + "\n")
    print(f"wrote {path}")


def main():
    any_run = False

    if SHOWS_CSV_URL or EVENTBRITE_ORGANIZER_URLS:
        form_rows = fetch_csv_rows(SHOWS_CSV_URL) if SHOWS_CSV_URL else []
        combined_events = normalize_form_rows(form_rows) + fetch_all_eventbrite_events(
            EVENTBRITE_ORGANIZER_URLS
        )
        write_file(f"{OUTPUT_DIR}/shows-content.qmd", build_shows_content(combined_events))
        if SHOWS_CSV_URL:
            write_file(
                f"{OUTPUT_DIR}/openmics-submitted.qmd",
                build_submitted_openmics_content(form_rows),
            )
        any_run = True

    if COMEDIANS_CSV_URL:
        rows = fetch_csv_rows(COMEDIANS_CSV_URL)
        write_file(f"{OUTPUT_DIR}/comedians-content.qmd", build_comedians_content(rows))
        any_run = True

    if OPENMICS_CSV_URL:
        rows = fetch_csv_rows(OPENMICS_CSV_URL)
        write_file(f"{OUTPUT_DIR}/openmics-content.qmd", build_openmics_content(rows))
        any_run = True

    if not any_run:
        print(
            "Nothing configured yet — nothing to do. Set SHOWS_CSV_URL / "
            "COMEDIANS_CSV_URL / OPENMICS_CSV_URL and/or "
            "EVENTBRITE_ORGANIZER_URLS at the top of scripts/generate_content.py.",
            file=sys.stderr,
        )


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
    else:
        main()
