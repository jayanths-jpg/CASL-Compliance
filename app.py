"""
Email Source Finder
--------------------
Given a list of email addresses, this tool:
  1. Searches the open web (via Serper.dev, which proxies Google results) for
     pages that mention each email.
  2. Downloads an offline copy (raw HTML / DOM) of every page found and
     stores it in Supabase Storage, for reference and auditability.
  3. Uses an OpenAI model to read the text around each mention of the email
     and flag any nearby "do not contact" style language (unsubscribe,
     do not disturb, opt-out, remove me, do not solicit, etc.).
  4. Persists every run (emails, sources, flags, snapshot locations) to
     Supabase Postgres via direct REST calls, and produces a results table:
     email | found? | good to collect? | flags | sources (live link + a
     signed download link to the offline DOM copy).

Deploy: Streamlit Community Cloud (front-end), Supabase (Postgres + Storage,
data layer) — matches the standard internal stack. See README.md and
schema.sql for setup.

Secrets expected in st.secrets (Streamlit Cloud -> App settings -> Secrets):
    SUPABASE_URL            = "https://xxxx.supabase.co"
    SUPABASE_SERVICE_KEY    = "service_role key (server-side only)"
    SERPER_API_KEY          = "..."   (optional — can also be entered in-app)
    OPENAI_API_KEY          = "..."   (optional — can also be entered in-app)
"""

import io
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Config / constants
# --------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 EmailSourceFinder/1.0"
)

DEFAULT_STOP_FLAGS_HINT = (
    "unsubscribe, do not disturb, do not contact, do not solicit, "
    "do not call, DNC, opt out / opt-out, remove me, no marketing, "
    "no spam, do not email, private / confidential - not for distribution, "
    "no unsolicited contact"
)

EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

STORAGE_BUCKET = "dom-snapshots"
SIGNED_URL_TTL_SECONDS = 3600

RUNS_TABLE = "runs"
EMAIL_RESULTS_TABLE = "email_results"
SOURCE_RESULTS_TABLE = "source_results"


def get_secret(name: str, default: str = "") -> str:
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------

@dataclass
class SourceResult:
    url: str
    title: str = ""
    fetch_ok: bool = False
    email_confirmed_on_page: bool = False
    dom_storage_path: str = ""
    flags: list = field(default_factory=list)
    good_to_collect: bool = True
    reasoning: str = ""
    error: str = ""


@dataclass
class EmailResult:
    email: str
    found: bool = False
    good_to_collect: bool = True
    all_flags: list = field(default_factory=list)
    sources: list = field(default_factory=list)  # list[SourceResult]
    error: str = ""


# --------------------------------------------------------------------------
# Supabase REST helpers (direct PostgREST + Storage calls, no SDK —
# avoids the supabase-py / httpx dependency conflicts seen on Streamlit
# Cloud with other internal apps)
# --------------------------------------------------------------------------

def sb_headers(service_key: str, content_type: bool = True) -> dict:
    h = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
    if content_type:
        h["Content-Type"] = "application/json"
    return h


def sb_insert(base_url: str, service_key: str, table: str, rows) -> tuple:
    """Insert one or more rows. Returns (inserted_rows, error)."""
    try:
        resp = requests.post(
            f"{base_url}/rest/v1/{table}",
            headers={**sb_headers(service_key), "Prefer": "return=representation"},
            json=rows,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json(), ""
    except Exception as e:
        detail = getattr(e, "response", None)
        msg = detail.text if detail is not None else str(e)
        return [], msg


def sb_select(base_url: str, service_key: str, table: str, params: dict) -> tuple:
    try:
        resp = requests.get(
            f"{base_url}/rest/v1/{table}",
            headers=sb_headers(service_key),
            params=params,
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json(), ""
    except Exception as e:
        detail = getattr(e, "response", None)
        msg = detail.text if detail is not None else str(e)
        return [], msg


def sb_storage_upload(base_url: str, service_key: str, path: str, content: bytes, content_type: str) -> str:
    """Upload (or overwrite) an object in the DOM snapshots bucket. Returns error string, "" on success."""
    try:
        resp = requests.post(
            f"{base_url}/storage/v1/object/{STORAGE_BUCKET}/{quote(path)}",
            headers={
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
                "Content-Type": content_type,
                "x-upsert": "true",
            },
            data=content,
            timeout=30,
        )
        resp.raise_for_status()
        return ""
    except Exception as e:
        detail = getattr(e, "response", None)
        return detail.text if detail is not None else str(e)


def sb_storage_signed_url(base_url: str, service_key: str, path: str, expires_in: int = SIGNED_URL_TTL_SECONDS) -> str:
    try:
        resp = requests.post(
            f"{base_url}/storage/v1/object/sign/{STORAGE_BUCKET}/{quote(path)}",
            headers=sb_headers(service_key),
            json={"expiresIn": expires_in},
            timeout=20,
        )
        resp.raise_for_status()
        signed_path = resp.json().get("signedURL", "")
        if not signed_path:
            return ""
        return f"{base_url}/storage/v1{signed_path}"
    except Exception:
        return ""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def safe_slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]", "_", text)[:80]


def parse_bulk_input(uploaded_file) -> list:
    """Accepts a .csv (looks for an 'email' column, else first column) or
    a .txt file (one email per line)."""
    import csv
    name = uploaded_file.name.lower()
    content = uploaded_file.read().decode("utf-8", errors="ignore")
    emails = []
    if name.endswith(".csv"):
        reader = csv.reader(io.StringIO(content))
        rows = list(reader)
        if not rows:
            return []
        header = [h.strip().lower() for h in rows[0]]
        if "email" in header:
            idx = header.index("email")
            data_rows = rows[1:]
        else:
            idx = 0
            data_rows = rows
        for row in data_rows:
            if len(row) > idx and row[idx].strip():
                emails.append(row[idx].strip())
    else:
        for line in content.splitlines():
            line = line.strip().strip(",")
            if line:
                emails.append(line)
    seen = set()
    cleaned = []
    for e in emails:
        e = e.strip()
        if e and EMAIL_REGEX.fullmatch(e) and e.lower() not in seen:
            seen.add(e.lower())
            cleaned.append(e)
    return cleaned


def serper_search(email: str, api_key: str, num_results: int = 5) -> dict:
    url = "https://google.serper.dev/search"
    payload = {"q": f'"{email}"', "num": num_results}
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return {"error": str(e), "results": []}
    organic = data.get("organic", [])[:num_results]
    results = [
        {"link": item.get("link", ""), "title": item.get("title", "")}
        for item in organic
        if item.get("link")
    ]
    return {"error": "", "results": results}


def fetch_page(url: str, timeout: int = 15):
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type and not resp.text.strip().startswith("<"):
            return "", "", f"Non-HTML content-type: {content_type}"
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        visible_text = soup.get_text(separator=" ", strip=True)
        return html, visible_text, ""
    except Exception as e:
        return "", "", str(e)


def context_snippets(text: str, email: str, window: int = 400, max_snippets: int = 4) -> str:
    lower_text = text.lower()
    lower_email = email.lower()
    snippets = []
    start = 0
    while len(snippets) < max_snippets:
        pos = lower_text.find(lower_email, start)
        if pos == -1:
            break
        s = max(0, pos - window)
        e = min(len(text), pos + len(email) + window)
        snippets.append(text[s:e])
        start = pos + len(email)
    if not snippets:
        snippets.append(text[:1500])
    return "\n---\n".join(snippets)


def analyze_with_openai(email: str, url: str, snippet_text: str, api_key: str, model: str) -> dict:
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    system_prompt = (
        "You audit web pages for a data-collection compliance check. Given a "
        "target email address and text excerpted from a web page (windows "
        "around each occurrence of the email, or a page excerpt if the email "
        "wasn't found in visible text), determine:\n"
        "1. email_confirmed: true if the email genuinely appears in the text.\n"
        "2. flags: a list of short exact phrases found in the text that signal "
        "the email/person should NOT be contacted or collected — e.g. "
        f"mentions like: {DEFAULT_STOP_FLAGS_HINT}. Empty list if none.\n"
        "3. good_to_collect: false if any such flag is present or the context "
        "otherwise clearly signals opt-out/do-not-contact intent; true "
        "otherwise.\n"
        "4. reasoning: one short sentence explaining the call.\n"
        "Respond with ONLY a JSON object with keys: email_confirmed (bool), "
        "flags (array of strings), good_to_collect (bool), reasoning (string)."
    )
    user_prompt = f"Target email: {email}\nSource URL: {url}\n\nExcerpt(s):\n{snippet_text}"
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(resp.choices[0].message.content)
        return {
            "email_confirmed": bool(parsed.get("email_confirmed", False)),
            "flags": list(parsed.get("flags", []) or []),
            "good_to_collect": bool(parsed.get("good_to_collect", True)),
            "reasoning": str(parsed.get("reasoning", "")),
            "error": "",
        }
    except Exception as e:
        return {"email_confirmed": False, "flags": [], "good_to_collect": True, "reasoning": "", "error": str(e)}


def process_email(email: str, serper_key: str, openai_key: str, model: str,
                   max_results: int, fetch_delay: float, run_slug: str,
                   sb_url: str, sb_key: str) -> EmailResult:
    result = EmailResult(email=email)

    search = serper_search(email, serper_key, max_results)
    if search["error"]:
        result.error = f"Search error: {search['error']}"
        return result
    if not search["results"]:
        result.found = False
        return result

    for idx, item in enumerate(search["results"], start=1):
        url = item["link"]
        src = SourceResult(url=url, title=item.get("title", ""))

        html, visible_text, fetch_err = fetch_page(url)
        if fetch_err:
            src.error = fetch_err
            result.sources.append(src)
            time.sleep(fetch_delay)
            continue

        src.fetch_ok = True
        domain = safe_slug(urlparse(url).netloc or f"source{idx}")
        storage_path = f"{run_slug}/{safe_slug(email)}/{idx:02d}_{domain}.html"
        upload_err = sb_storage_upload(sb_url, sb_key, storage_path, html.encode("utf-8", errors="ignore"), "text/html")
        if not upload_err:
            src.dom_storage_path = storage_path
        else:
            src.error = (src.error + " | " if src.error else "") + f"Storage upload error: {upload_err}"

        src.email_confirmed_on_page = email.lower() in visible_text.lower()
        snippet_text = context_snippets(visible_text, email)
        analysis = analyze_with_openai(email, url, snippet_text, openai_key, model)
        if analysis["error"]:
            src.error = (src.error + " | " if src.error else "") + f"Analysis error: {analysis['error']}"
        else:
            src.flags = analysis["flags"]
            src.good_to_collect = analysis["good_to_collect"]
            src.reasoning = analysis["reasoning"]
            src.email_confirmed_on_page = src.email_confirmed_on_page or analysis["email_confirmed"]

        result.sources.append(src)
        time.sleep(fetch_delay)

    result.found = any(s.email_confirmed_on_page for s in result.sources)
    all_flags = []
    for s in result.sources:
        all_flags.extend(s.flags)
    result.all_flags = sorted(set(all_flags))
    result.good_to_collect = not any(
        (not s.good_to_collect) and s.email_confirmed_on_page for s in result.sources
    ) if result.sources else True

    return result


def persist_run(sb_url: str, sb_key: str, model: str, results: list) -> tuple:
    """Writes the run + email_results + source_results to Supabase. Returns (run_id, error)."""
    run_rows, err = sb_insert(sb_url, sb_key, RUNS_TABLE, [{"email_count": len(results), "model": model}])
    if err or not run_rows:
        return "", f"Could not create run record: {err}"
    run_id = run_rows[0]["id"]

    for r in results:
        er_rows, err = sb_insert(sb_url, sb_key, EMAIL_RESULTS_TABLE, [{
            "run_id": run_id,
            "email": r.email,
            "found": r.found,
            "good_to_collect": r.good_to_collect,
            "flags": r.all_flags,
            "error": r.error,
        }])
        if err or not er_rows:
            st.warning(f"Could not save results for {r.email}: {err}")
            continue
        email_result_id = er_rows[0]["id"]

        if r.sources:
            source_rows = [{
                "email_result_id": email_result_id,
                "url": s.url,
                "title": s.title,
                "fetch_ok": s.fetch_ok,
                "email_confirmed_on_page": s.email_confirmed_on_page,
                "good_to_collect": s.good_to_collect,
                "flags": s.flags,
                "reasoning": s.reasoning,
                "error": s.error,
                "dom_storage_path": s.dom_storage_path,
            } for s in r.sources]
            _, err = sb_insert(sb_url, sb_key, SOURCE_RESULTS_TABLE, source_rows)
            if err:
                st.warning(f"Could not save sources for {r.email}: {err}")

    return run_id, ""


def load_run(sb_url: str, sb_key: str, run_id: str) -> list:
    """Reads a past run back out of Supabase into EmailResult/SourceResult objects."""
    email_rows, err = sb_select(sb_url, sb_key, EMAIL_RESULTS_TABLE, {"run_id": f"eq.{run_id}", "order": "created_at.asc"})
    if err:
        st.error(f"Could not load run: {err}")
        return []
    results = []
    for row in email_rows:
        r = EmailResult(
            email=row["email"], found=row["found"], good_to_collect=row["good_to_collect"],
            all_flags=row.get("flags") or [], error=row.get("error") or "",
        )
        src_rows, s_err = sb_select(sb_url, sb_key, SOURCE_RESULTS_TABLE, {"email_result_id": f"eq.{row['id']}", "order": "created_at.asc"})
        if not s_err:
            for sr in src_rows:
                r.sources.append(SourceResult(
                    url=sr["url"], title=sr.get("title") or "", fetch_ok=sr["fetch_ok"],
                    email_confirmed_on_page=sr["email_confirmed_on_page"], dom_storage_path=sr.get("dom_storage_path") or "",
                    flags=sr.get("flags") or [], good_to_collect=sr["good_to_collect"],
                    reasoning=sr.get("reasoning") or "", error=sr.get("error") or "",
                ))
        results.append(r)
    return results


def results_to_dataframe(results: list) -> pd.DataFrame:
    rows = []
    for r in results:
        rows.append({
            "Email": r.email,
            "Found": "Yes" if r.found else "No",
            "# Sources": len(r.sources),
            "Good to Collect": "Yes" if r.good_to_collect else "No",
            "Flags": "; ".join(r.all_flags) if r.all_flags else "",
            "Error": r.error,
        })
    return pd.DataFrame(rows)


def render_results(results: list, sb_url: str, sb_key: str, key_prefix: str):
    df = results_to_dataframe(results)
    st.dataframe(df, use_container_width=True)

    st.download_button(
        "Download results as CSV",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name="email_source_results.csv",
        mime="text/csv",
        key=f"{key_prefix}_csv",
    )

    st.divider()
    st.subheader("Per-email detail")
    for r in results:
        header = f"{r.email} — {'Found' if r.found else 'Not found'} — {'Good to collect' if r.good_to_collect else 'FLAGGED: not good to collect'}"
        with st.expander(header):
            if r.error:
                st.error(r.error)
            if not r.sources:
                st.write("No sources found on the open web.")
            for s in r.sources:
                st.markdown(f"**Source:** [{s.title or s.url}]({s.url})")
                cols = st.columns([2, 2, 2, 3])
                cols[0].write("Fetched OK" if s.fetch_ok else "Fetch failed")
                cols[1].write("Email confirmed on page" if s.email_confirmed_on_page else "Email not confirmed in visible text")
                cols[2].write("Good to collect" if s.good_to_collect else "FLAGGED")
                if s.flags:
                    cols[3].write("Flags: " + ", ".join(s.flags))
                if s.reasoning:
                    st.caption(s.reasoning)
                if s.error:
                    st.warning(s.error)
                if s.dom_storage_path:
                    signed = sb_storage_signed_url(sb_url, sb_key, s.dom_storage_path)
                    if signed:
                        st.markdown(f"[Open offline DOM copy]({signed})  *(link valid for 1 hour)*")
                    else:
                        st.caption("Offline DOM copy stored, but couldn't generate a link right now.")
                st.markdown("---")


# --------------------------------------------------------------------------
# Streamlit UI
# --------------------------------------------------------------------------

st.set_page_config(page_title="Email Source Finder", layout="wide")
st.title("Email Source Finder")
st.caption(
    "Search the open web for each email you provide, archive an offline "
    "copy of every page found in Supabase, and flag do-not-contact signals "
    "near the mention using GPT."
)

sb_url = get_secret("SUPABASE_URL")
sb_key = get_secret("SUPABASE_SERVICE_KEY")

if not sb_url or not sb_key:
    st.error(
        "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in this app's "
        "Streamlit secrets before it can run — see README.md / schema.sql "
        "for setup."
    )
    st.stop()

if "results" not in st.session_state:
    st.session_state.results = []
if "run_id" not in st.session_state:
    st.session_state.run_id = ""

with st.sidebar:
    st.header("API Keys")
    serper_key = st.text_input("Serper.dev API key", type="password", value=get_secret("SERPER_API_KEY"))
    openai_key = st.text_input("OpenAI API key", type="password", value=get_secret("OPENAI_API_KEY"))

    st.header("Settings")
    max_results = st.slider("Max sources to check per email", 1, 10, 5)
    model = st.selectbox("OpenAI model for analysis", ["gpt-4o-mini", "gpt-4o"], index=0)
    fetch_delay = st.slider("Delay between page fetches (seconds)", 0.0, 5.0, 1.0, 0.5)
    st.caption("Every run — emails, sources, flags, and DOM snapshots — is saved to Supabase.")

tab_input, tab_results, tab_history = st.tabs(["Input", "Results", "History"])

with tab_input:
    mode = st.radio("How do you want to provide emails?", ["Bulk upload", "Type in emails"], horizontal=True)
    emails_to_process = []

    if mode == "Bulk upload":
        uploaded = st.file_uploader("Upload a .csv (with an 'email' column) or .txt (one email per line)", type=["csv", "txt"])
        if uploaded:
            emails_to_process = parse_bulk_input(uploaded)
            st.success(f"Loaded {len(emails_to_process)} valid, de-duplicated email(s).")
            st.dataframe(pd.DataFrame({"Email": emails_to_process}), use_container_width=True, height=200)
    else:
        st.caption("Enter one email per row. Add rows as needed.")
        default_df = pd.DataFrame({"email": [""]})
        edited_df = st.data_editor(default_df, num_rows="dynamic", use_container_width=True, key="manual_emails")
        raw = [str(e).strip() for e in edited_df["email"].tolist() if str(e).strip()]
        seen = set()
        for e in raw:
            if EMAIL_REGEX.fullmatch(e) and e.lower() not in seen:
                seen.add(e.lower())
                emails_to_process.append(e)
        invalid = [e for e in raw if not EMAIL_REGEX.fullmatch(e)]
        if invalid:
            st.warning(f"Ignoring {len(invalid)} row(s) that aren't valid email addresses.")

    run = st.button("Run Search", type="primary", disabled=not emails_to_process)

    if run:
        if not serper_key or not openai_key:
            st.error("Please enter both a Serper.dev API key and an OpenAI API key in the sidebar.")
        else:
            run_slug = datetime.now().strftime("%Y%m%d-%H%M%S")
            progress = st.progress(0.0, text="Starting...")
            results = []
            total = len(emails_to_process)
            for i, email in enumerate(emails_to_process, start=1):
                progress.progress((i - 1) / total, text=f"Processing {email} ({i}/{total})")
                res = process_email(email, serper_key, openai_key, model, max_results, fetch_delay, run_slug, sb_url, sb_key)
                results.append(res)
            progress.progress(1.0, text="Saving to Supabase...")
            run_id, persist_err = persist_run(sb_url, sb_key, model, results)
            if persist_err:
                st.warning(persist_err)
            st.session_state.results = results
            st.session_state.run_id = run_id
            st.session_state.run_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            st.success(f"Processed {total} email(s). See the Results tab.")

with tab_results:
    results = st.session_state.results
    if not results:
        st.info("No results yet. Provide emails and click Run Search on the Input tab.")
    else:
        st.caption(f"Last run: {st.session_state.get('run_timestamp', '')}" + (f" (run id: {st.session_state.run_id})" if st.session_state.run_id else ""))
        render_results(results, sb_url, sb_key, key_prefix="current")

with tab_history:
    st.caption("Browse and re-view past runs stored in Supabase.")
    runs, err = sb_select(sb_url, sb_key, RUNS_TABLE, {"order": "created_at.desc", "limit": "50"})
    if err:
        st.error(f"Could not load run history: {err}")
    elif not runs:
        st.info("No past runs yet.")
    else:
        options = {f"{r['created_at']} — {r['email_count']} email(s) — {r.get('model', '')} — {r['id']}": r["id"] for r in runs}
        choice = st.selectbox("Select a past run", list(options.keys()))
        if st.button("Load this run"):
            loaded = load_run(sb_url, sb_key, options[choice])
            if loaded:
                render_results(loaded, sb_url, sb_key, key_prefix="history")
