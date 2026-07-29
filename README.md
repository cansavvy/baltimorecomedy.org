# Baltimore Comedy.org

A Quarto website for promoting Baltimore-area comedians and their shows.

## What's in here

- `_quarto.yml` — site config (nav bar, footer, theme)
- `index.qmd` — homepage
- `shows.qmd` — list of shows with dates
- `comedians.qmd` — grid of comedian profiles with photos + social links
- `submit.qmd` — "Get Listed" page (Google Form goes here)
- `styles.scss` / `styles.css` — the dark speakeasy / gold theme
- `images/` — logo, placeholder headshot, and (eventually) comedian photos

## Previewing it locally

You need [Quarto](https://quarto.org/docs/get-started/) installed once. Then, from this folder:

```
quarto preview
```

This opens a live-reloading preview in your browser — edit any `.qmd` file and it updates instantly.

## Publishing it

Easiest free option: **Quarto + GitHub Pages**.

1. Push this folder to a GitHub repo.
2. Run `quarto publish gh-pages` from this folder (one-time setup, then it's a one-command deploy going forward).
3. Point your `baltimorecomedy.org` domain at the GitHub Pages site (GitHub's docs on custom domains walk through the DNS records).

Other options: Netlify, Render, or Quarto Pub (`quarto publish quarto-pub`) if you want something even simpler than GitHub.

## How updates work (approve-and-it-publishes-itself)

Shows, comedians, and the open mic table are **not** hand-edited anymore.
Each page's content lives in an auto-generated file inside `_generated/`,
built by `scripts/generate_content.py` and pulled into the real pages
(`shows.qmd`, `comedians.qmd`, `open-mics.qmd`) via a Quarto
`{{< include >}}`. The flow:

1. Someone submits a show or a comedian profile through a Google Form.
2. It lands as a row in the linked Google Sheet.
3. You review it and type `Yes` in an **Approved** column on that sheet
   (add this column once, by hand — it's not part of the form).
4. `scripts/generate_content.py` pulls each sheet (published as CSV),
   keeps only the Approved rows, and rewrites the matching file in
   `_generated/`.
5. A GitHub Actions workflow (`.github/workflows/update-site.yml`) runs
   that script automatically every morning — and any time you push, or
   trigger it manually from the Actions tab — then rebuilds and
   republishes the site.

So the only ongoing manual step for you is typing "Yes" in a spreadsheet
cell. Nothing to redeploy, no code to touch.

**Full setup instructions:** see [`FORM-SPEC.md`](FORM-SPEC.md) for the
exact Google Form fields to use (the column names have to match what the
script expects), and the "wiring it up" section at the bottom of that
file for connecting the published CSV URLs.

### Open Mics data source

The Open Mics page's main table works the same way, but pulls from a
listing sheet instead of a form — set `OPENMICS_CSV_URL` in
`scripts/generate_content.py` to that sheet's published-CSV link. The
generator adds a **Last Verified** column automatically, stamped with
whatever date it last successfully pulled that row (or the sheet's own
value in a `Last Verified` column, if you're tracking that yourself).

Separately, the Shows form has an "Is this an Open Mic?" question — any
approved response marked Yes there skips the Shows page entirely and
shows up in a "Recently Submitted" section at the bottom of the Open
Mics page instead. That's driven by `SHOWS_CSV_URL`, not
`OPENMICS_CSV_URL` — no extra setup once the Shows pipeline is live.

### Doing it by hand instead

If you'd rather skip the form/sheet pipeline for something one-off, you
can still hand-edit the files in `_generated/` directly — just know the
next automated run will overwrite anything not also reflected in the
source sheet.

## Notes on the theme

Colors and fonts live in `styles.scss` (variables) and `styles.css`
(component styling — cards, buttons, show list, hero). If you want to
adjust the palette, the gold/burgundy/cream hex values are all named
variables at the top of `styles.scss`.
