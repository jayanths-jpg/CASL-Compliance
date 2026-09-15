# Email Source Finder

Given one or more email addresses, this tool:

1. **Searches the open web** for pages that mention each email, via
   [Serper.dev](https://serper.dev) (a Google-results search API).
2. **Archives an offline copy** (raw HTML / DOM) of every page found into
   **Supabase Storage**.
3. **Analyzes the text around each mention** with an OpenAI model, flagging
   any nearby "do not contact" language — unsubscribe, do not disturb,
   opt-out, remove me, do not solicit, etc.
4. **Persists every run to Supabase Postgres** (emails, sources, flags,
   snapshot paths) and shows a results table: email, found (yes/no), good
   to collect (yes/no), the specific flag phrases found, and a link to the
   live source plus a signed link to the offline DOM copy.
5. **History tab** lets you browse and reload any past run.

Built to match the standard internal stack: Streamlit front end (deployed
on Streamlit Community Cloud), Supabase via direct PostgREST/Storage REST
calls (no `supabase` SDK — avoids its `httpx`/`httpcore` conflicts on
Streamlit Cloud).

## 1. Set up Supabase

1. Create a Supabase project (or use an existing one).
2. Open the SQL editor and run `schema.sql` from this folder — it creates
   the `runs`, `email_results`, and `source_results` tables with RLS
   enabled (locked to the service_role key only).
3. Go to **Storage** and create a bucket named `dom-snapshots`. Leave it
   **private** (do not mark it public) — the app generates short-lived
   signed URLs for downloads instead.
4. Grab your project URL and **service_role** key from
   Project Settings -> API. The service_role key must stay server-side
   only (it goes in Streamlit secrets, never in client-facing code).

## 2. Deploy to Streamlit Community Cloud

1. Push this folder to a GitHub repo.
2. On [share.streamlit.io](https://share.streamlit.io), create a new app
   pointing at `app.py` in that repo.
3. In the app's **Settings -> Secrets**, add:

   ```toml
   SUPABASE_URL = "https://xxxx.supabase.co"
   SUPABASE_SERVICE_KEY = "eyJ..."   # service_role key — keep this private

   # Optional: pre-fill these so users don't have to paste their own each
   # session. Leave out if you'd rather each user supply their own.
   SERPER_API_KEY = "..."
   OPENAI_API_KEY = "..."
   ```

4. Deploy. `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` are required — the app
   refuses to start without them. `SERPER_API_KEY` / `OPENAI_API_KEY` are
   optional secrets; if omitted, each user enters their own in the sidebar
   for their session.

## 3. Run locally (optional, for testing before deploy)

```bash
pip install -r requirements.txt
mkdir -p .streamlit
cat > .streamlit/secrets.toml << 'EOF'
SUPABASE_URL = "https://xxxx.supabase.co"
SUPABASE_SERVICE_KEY = "eyJ..."
EOF
streamlit run app.py
```

## Input formats

- **Bulk upload**: a `.csv` file with a column named `email` (or, if there's
  no header, the first column is used), or a `.txt` file with one email per
  line.
- **Manual entry**: a growable table in the UI, one email per row.

Duplicate and malformed entries are filtered out before any searching
happens.

## How "good to collect" is decided

For each source page found, the tool pulls the text windows immediately
around every occurrence of the email (falling back to a short page excerpt
if the email isn't in the visible text — e.g. it's in an image or an
obfuscated form) and asks the model to judge, from that context alone,
whether there's a do-not-contact signal nearby. An email is marked **not
good to collect** if *any* confirmed source carries such a flag — this is
deliberately conservative, since one opt-out mention should outweigh
several silent ones.

## Data model

- `runs` — one row per search batch (timestamp, email count, model used).
- `email_results` — one row per email per run (found, good_to_collect,
  aggregated flags).
- `source_results` — one row per source page per email (URL, fetch status,
  per-page flags/reasoning, and the Supabase Storage path of the archived
  DOM snapshot).

RLS is enabled on all three tables with no anon/authenticated policies —
only the service_role key (used server-side by the app) can read or write,
so none of this is exposed via Supabase's public API.

## Notes for production use

- **Rate limiting & politeness**: the sidebar has a per-fetch delay slider.
  For large batches, keep this reasonable, and consider adding a
  `robots.txt` check if you'll run this against sites with strict scraping
  policies.
- **Cost**: each email costs 1 Serper search + up to `max_results` page
  fetches + up to `max_results` OpenAI calls. Tune "Max sources to check
  per email" down for large batches.
- **Signed URL links** to DOM snapshots are valid for 1 hour from when the
  Results/History tab is viewed — they're regenerated each time the page
  renders, so reopening the tab refreshes them.
- **Compliance framing**: this tool is built to *surface* do-not-contact
  signals, not bypass them — worth keeping that framing when positioning
  it internally or with clients, given the CASL/consent-adjacent nature of
  the underlying data.
