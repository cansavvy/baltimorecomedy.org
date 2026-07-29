# Form Specs — Add a Show / Add a Comedian

Build these as two separate Google Forms (or one form with a "Section"
branch for each — see note at the bottom). The **question titles below
must match exactly** — the automation script reads sheet columns by
these names.

For both forms: **Settings → Responses → turn on "Collect email
addresses"** and **"Get email notifications for new responses"** so they
land in candyscomedybaltimore@gmail.com.

---

## Form 1: "Add a Show to BaltimoreComedy.org" — ✅ live

**Live link:** https://forms.gle/4G8zEWtQHKgnEFaZ7

Actual fields, as built (the automation script now matches these exactly):

| Question | Type | Required |
|----------|------|----------|
| Email for showrunner | Short answer | Yes |
| Show name | Short answer | Yes |
| Venue | Short answer | Yes |
| Date | Date | Yes |
| Is this a recurring show? If so, how frequently? Weekly? Monthly? | Short answer | Yes |
| Link to where folks can buy tickets | Short answer | Yes |

**⚠️ One field still to add, by hand, in the form editor:**

| Question | Type | Required |
|----------|------|----------|
| Is this an Open Mic? | Multiple choice: Yes / No | Yes |

The automation already expects this exact question title. Once you add
it (Google Forms → open the form → Add question → paste that title in →
set type to Multiple choice → add "Yes" and "No" as the two options →
mark required), submissions marked "Yes" will automatically show up on
the **Open Mics** page instead of the Shows page, under a "Recently
Submitted" section — no other setup needed. This also means people no
longer have to DM the list's maintainer to get a new mic added; they can
just use this form.

This one embeds fine (no file upload) — it's live on the Submit page.

After publishing, open the linked response Sheet and add one more column
by hand, filled in as you review each submission:

| Column | What goes in it |
|--------|------------------|
| **Approved** | Type `Yes` once you've reviewed a row and want it live. Leave blank (or `No`) to keep it off the site. |

---

## Form 2: "Add a Comedian to BaltimoreComedy.org" — ✅ live, fields confirmed

**Live link:** https://forms.gle/3m7YKsYXMsZRmUJXA

Actual fields, as built (the automation script now matches these exactly):

| Question | Type | Required |
|----------|------|----------|
| Email | Short answer | (as built) |
| Stage Name | Short answer | (as built) |
| Comedic Style (short sentence descriptor) | Short answer | (as built) |
| Headshot | File upload | (as built) |
| Website URL | Short answer | No |
| Instagram URL | Short answer | No |
| TikTok URL | Short answer | No |
| Youtube URL | Short answer | No |
| Facebook URL | Short answer | No |

This form has a photo-upload question, which means Google blocks it from
being previewed or embedded by anything that isn't signed into a Google
account. It links out from the Submit page instead of embedding.

Note: this form doesn't currently collect a separate bio/description —
just the one-line style descriptor. If you want a fuller bio on comedian
cards later, add a "Bio" question to the form and the script can be
extended to use it.

Same as the Shows form — add an **Approved** column to the response
sheet by hand.

**About headshots:** Google Forms saves uploaded files to a folder in
your Drive, not as a public image link, so the automation can't hotlink
them directly. When you approve a comedian, download their photo from
that Drive folder, drop it into the site's `/images` folder, and add one
more column to the sheet called **Photo Filename** with just the
filename (e.g. `jo-test.jpg`) — the generator will use that automatically
on the next run. Until you do, their card shows the placeholder headshot.

---

## Combining into one form (optional)

If you'd rather have a single form, add a first question:

> **What are you submitting?** — Multiple choice: "A Show" / "My Comedian Profile"

...then use Google Forms' **Section** + **"Go to section based on
answer"** feature to branch into the two question sets above. The
resulting sheet will just have both sets of columns (mostly blank
depending on which branch someone took) — that's fine, the script only
reads the columns it needs for each output.

---

## Wiring it up once the forms exist

1. Create both forms using the fields above.
2. For each: **Responses tab → green Sheets icon → Create spreadsheet**.
3. On each response spreadsheet: **File → Share → Publish to web →**
   select the sheet → format **CSV** → **Publish**. Copy that URL.
4. Paste the two (or three, with open mics) URLs into
   `scripts/generate_content.py` at the top, replacing the `None`
   placeholders for `SHOWS_CSV_URL` and `COMEDIANS_CSV_URL`.
5. Commit and push — the GitHub Action picks it up on the next scheduled
   run, or trigger it manually from the Actions tab.
