"""
SEC 13F Filing Tool
====================
1. Pulls all 13F filing index data between two dates from SEC EDGAR
2. Launches a GUI (Tkinter) for company selection via text search or index browsing
3. Fetches the selected 13F filings and extracts portfolio tickers

Dependencies:
    pip install requests pandas lxml

IMPORTANT – User-Agent:
    SEC EDGAR requires a real name and email in the User-Agent string.
    Set yours in the CONSTANTS section below, or the tool will prompt you
    on first run and save it for the session.
"""

import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import threading
import requests
import pandas as pd
import xml.etree.ElementTree as ET
import re
import time
from datetime import datetime, date

# ---------------------------------------------------------------------------
# CONSTANTS  –  *** SET YOUR NAME AND EMAIL HERE ***
# ---------------------------------------------------------------------------
SEC_BASE        = "https://www.sec.gov"
DATA_SEC_BASE   = "https://data.sec.gov"
FULL_INDEX_BASE = f"{SEC_BASE}/Archives/edgar/full-index"

# Replace with "Firstname Lastname your@email.com"
# SEC EDGAR returns 503 if this is left as the placeholder or looks fake.
USER_AGENT = "Bala Balasubramaniam bbalasub@gmail.com"

RATE_LIMIT_DELAY = 0.15   # seconds between requests (SEC asks ≤ 10 req/s)


def _make_headers(host: str = "www.sec.gov") -> dict:
    """Return request headers with the correct Host for the given domain."""
    return {
        "User-Agent":      USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
        "Host":            host,
    }


def _sec_get(url: str, retries: int = 3, backoff: float = 2.0) -> requests.Response:
    """
    GET a SEC URL with automatic retry + exponential backoff on 429/503.
    Chooses the right Host header based on the URL domain.
    """
    host = "data.sec.gov" if "data.sec.gov" in url else "www.sec.gov"
    headers = _make_headers(host)
    for attempt in range(retries):
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code in (429, 503):
            wait = backoff * (2 ** attempt)
            time.sleep(wait)
            continue
        r.raise_for_status()
        time.sleep(RATE_LIMIT_DELAY)
        return r
    r.raise_for_status()   # raise on final failure
    return r


# ---------------------------------------------------------------------------
# PART 1 – FETCH INDEX DATA
# ---------------------------------------------------------------------------

def _quarters_between(start: date, end: date):
    """Yield (year, quarter) tuples covering start..end inclusive."""
    y, q = start.year, (start.month - 1) // 3 + 1
    ey, eq = end.year, (end.month - 1) // 3 + 1
    while (y, q) <= (ey, eq):
        yield y, q
        q += 1
        if q > 4:
            q, y = 1, y + 1


def fetch_13f_index(start_date: str, end_date: str,
                    progress_callback=None) -> pd.DataFrame:
    """
    Download the EDGAR quarterly full-index files and return a DataFrame of
    13F-HR filings filed between start_date and end_date (YYYY-MM-DD).
    """
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end   = datetime.strptime(end_date,   "%Y-%m-%d").date()

    frames = []
    for year, quarter in _quarters_between(start, end):
        url = f"{FULL_INDEX_BASE}/{year}/QTR{quarter}/company.idx"
        if progress_callback:
            progress_callback(f"Fetching index {year} Q{quarter}…")
        try:
            r = _sec_get(url)
        except requests.RequestException as e:
            if progress_callback:
                progress_callback(f"  ⚠ Could not fetch {url}: {e}")
            continue

        rows = []
        for line in r.text.splitlines():
            if len(line) < 98 or line.startswith('-') or line.startswith('Company'):
                continue
            company    = line[0:62].strip()
            form_type  = line[62:74].strip()
            cik        = line[74:86].strip()
            date_filed = line[86:98].strip()
            filename   = line[98:].strip()
            if form_type in ("13F-HR", "13F-HR/A"):
                rows.append({
                    "company_name": company,
                    "form_type":    form_type,
                    "cik":          cik,
                    "date_filed":   date_filed,
                    "filename":     filename,
                    "year":         year,
                    "quarter":      quarter,
                })

        if rows:
            df = pd.DataFrame(rows)
            df["date_filed"] = pd.to_datetime(df["date_filed"], errors="coerce")
            df = df[df["date_filed"].between(pd.Timestamp(start), pd.Timestamp(end))]
            df["date_filed"] = df["date_filed"].dt.strftime("%Y-%m-%d")
            frames.append(df)

    if not frames:
        return pd.DataFrame(columns=["company_name", "form_type", "cik",
                                     "date_filed", "filename", "year", "quarter",
                                     "filing_quarter"])
    result = pd.concat(frames, ignore_index=True)
    result = result.drop_duplicates(subset=["cik", "date_filed", "form_type"])
    result["filing_quarter"] = (result["year"].astype(str)
                                + " Q" + result["quarter"].astype(str))
    result = result.sort_values(["company_name", "date_filed"]).reset_index(drop=True)
    return result


# ---------------------------------------------------------------------------
# PART 2 – FETCH & PARSE 13F FILING FOR PORTFOLIO TICKERS
# ---------------------------------------------------------------------------

def _get_infotable_urls_from_index(filename: str,
                                    progress_callback=None) -> tuple:
    """
    Uses the EDGAR filing index page (-index.htm) to find all documents
    marked as 'Information Table', extracting their direct URLs.
    Falls back to the .txt envelope for date fields.

    Returns (infotable_files, accepted_dt, date_filed, effective_date) where
    infotable_files is a list of {"url": ..., "ext": "xml"|"html"|"txt"} dicts.
    """
    filename   = filename.lstrip("/")
    parts      = filename.rsplit("/", 1)
    folder_path, base = parts
    accession  = re.sub(r'\.(txt|htm|html)$', '', base, flags=re.I)
    cik        = folder_path.split("/")[-1]
    acc_nodash = accession.replace("-", "")
    base_folder   = f"{SEC_BASE}/Archives/edgar/data/{cik}/{acc_nodash}/"
    txt_index_url = base_folder + accession + ".txt"
    htm_index_url = base_folder + accession + "-index.htm"

    # ── Step 1: fetch .txt envelope for the three dates ───────────────────────
    accepted_dt    = None
    date_filed_env = None
    effective_date = None

    print(f"[1] Fetching filing envelope for dates: {txt_index_url}")
    if progress_callback:
        progress_callback(f"Fetching filing envelope: {txt_index_url}")

    try:
        r_txt = _sec_get(txt_index_url)

        def _parse_date8(raw):
            return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if raw and len(raw) >= 8 else None

        m = re.search(r"<ACCEPTANCE-DATETIME>\s*(\d{14})", r_txt.text, re.I)
        if m:
            raw = m.group(1)
            accepted_dt = (f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} "
                           f"{raw[8:10]}:{raw[10:12]}:{raw[12:14]}")
        m = re.search(r"<FILED-AS-OF-DATE>\s*(\d{8})", r_txt.text, re.I)
        if m:
            date_filed_env = _parse_date8(m.group(1))
        m = re.search(r"<EFFECTIVENESS-DATE>\s*(\d{8})", r_txt.text, re.I)
        if m:
            effective_date = _parse_date8(m.group(1))

        print(f"[*] Accepted: {accepted_dt}  Filed: {date_filed_env}  Effective: {effective_date}")

    except requests.RequestException as e:
        print(f"[!] Could not fetch envelope: {e}")

    # ── Step 2: fetch -index.htm to get direct document URLs ─────────────────
    print(f"[2] Fetching filing index page: {htm_index_url}")
    if progress_callback:
        progress_callback(f"Fetching filing index: {htm_index_url}")

    try:
        r_htm = _sec_get(htm_index_url)
    except requests.RequestException as e:
        print(f"[!] Could not fetch index page: {e}")
        if progress_callback:
            progress_callback(f"\u26a0 Could not fetch filing index page: {e}")
        return [], accepted_dt, date_filed_env, effective_date

    # ── Step 3: parse the index page table to find Information Table URLs ─────
    # The index page is an HTML table with columns: Seq, Description, Document, Type, Size
    # We look for rows where Type contains "Information Table" (case-insensitive)
    # and extract the href from the Document column directly.
    from html.parser import HTMLParser

    class IndexPageParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows        = []   # list of {type, href, description}
            self._cur_row    = {}
            self._cur_cell   = ""
            self._cell_idx   = 0
            self._in_td      = False
            self._in_table   = False
            self._cur_href   = None
            self._depth      = 0

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "table":
                self._depth += 1
                if self._depth == 1:
                    self._in_table = True
            if self._in_table and tag == "tr":
                self._cur_row  = {}
                self._cell_idx = 0
                self._cur_href = None
            if self._in_table and tag == "td":
                self._in_td    = True
                self._cur_cell = ""
            if self._in_table and tag == "a":
                href = attrs.get("href", "")
                if href:
                    self._cur_href = href

        def handle_endtag(self, tag):
            if tag == "table":
                self._depth -= 1
                if self._depth == 0:
                    self._in_table = False
            if self._in_table and tag == "td":
                val = self._cur_cell.strip()
                # Columns: 0=Seq, 1=Description, 2=Document(link), 3=Type, 4=Size
                if self._cell_idx == 1:
                    self._cur_row["description"] = val
                elif self._cell_idx == 2:
                    self._cur_row["href"]        = self._cur_href or ""
                elif self._cell_idx == 3:
                    self._cur_row["type"]        = val
                self._cell_idx += 1
                self._in_td    = False
                self._cur_href = None
            if self._in_table and tag == "tr":
                if self._cur_row.get("href"):
                    self.rows.append(dict(self._cur_row))

        def handle_data(self, data):
            if self._in_td:
                self._cur_cell += data

    parser = IndexPageParser()
    parser.feed(r_htm.text)

    print(f"[3] Index page rows found: {len(parser.rows)}")
    for row in parser.rows:
        print(f"    type={row.get('type','')!r:35s}  href={row.get('href','')!r}")

    # Collect rows whose type indicates an information table
    INFO_TABLE_KEYWORDS = ("information table", "13f holdings", "holdings")
    infotable_files = []
    seen_urls = set()

    for row in parser.rows:
        doc_type = row.get("type", "").lower().strip()
        href     = row.get("href", "").strip()
        if not href:
            continue
        is_info = any(kw in doc_type for kw in INFO_TABLE_KEYWORDS)
        if not is_info:
            continue

        # Build the absolute URL directly from the href
        if href.startswith("http"):
            url = href
        elif href.startswith("/"):
            url = SEC_BASE + href
        else:
            url = base_folder + href

        if url in seen_urls:
            continue
        seen_urls.add(url)

        ext = url.rsplit(".", 1)[-1].lower() if "." in url.split("/")[-1] else "txt"
        infotable_files.append({"url": url, "ext": ext})
        print(f"    → Collected ({ext}): {url}")

    # Fallback: if no Information Table type found, take any non-primary parseable file
    if not infotable_files:
        print("[!] No 'Information Table' type rows — using fallback")
        SKIP = {"primary_doc", "xbrl", "summary", "cover"}
        PARSEABLE = {"xml", "htm", "html", "txt"}
        for row in parser.rows:
            href = row.get("href", "").strip()
            if not href:
                continue
            name = href.split("/")[-1].lower()
            ext  = name.rsplit(".", 1)[-1] if "." in name else ""
            if ext not in PARSEABLE:
                continue
            if any(s in name for s in SKIP):
                continue
            url = (href if href.startswith("http")
                   else SEC_BASE + href if href.startswith("/")
                   else base_folder + href)
            if url in seen_urls:
                continue
            seen_urls.add(url)
            infotable_files.append({"url": url, "ext": ext})
            print(f"    → Fallback ({ext}): {url}")

    if not infotable_files:
        print("[!] No infotable documents found in index page.")
        if progress_callback:
            progress_callback("\u26a0 No Information Table documents found.")

    return infotable_files, accepted_dt, date_filed_env, effective_date



def _parse_infotable_html(html_text: str, log=None) -> list[dict]:
    """
    Parse an HTML 13F information table and return a list of holding dicts.
    Handles multi-row headers (EDGAR's XSL-rendered format) and single-row headers.
    log: optional callable(str) for progress messages.
    """
    def _log(msg):
        print(msg)
        if log:
            log(msg)
    from html.parser import HTMLParser

    class TableParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.rows     = []   # all rows as lists of strings
            self.cur_row  = []
            self.cur_cell = ""
            self.in_cell  = False
            self.depth    = 0    # table nesting depth

        def handle_starttag(self, tag, attrs):
            if tag == "table":
                self.depth += 1
            if self.depth >= 1 and tag in ("td", "th"):
                self.in_cell  = True
                self.cur_cell = ""

        def handle_endtag(self, tag):
            if self.depth >= 1 and tag in ("td", "th"):
                self.cur_row.append(self.cur_cell.strip())
                self.in_cell = False
            if self.depth >= 1 and tag == "tr":
                if self.cur_row:
                    self.rows.append(self.cur_row)
                    self.cur_row = []
            if tag == "table":
                self.depth -= 1

        def handle_data(self, data):
            if self.in_cell:
                self.cur_cell += data

    parser = TableParser()
    parser.feed(html_text)

    if len(parser.rows) < 2:
        _log(f"  HTML parser: only {len(parser.rows)} rows found, skipping")
        return []

    _log(f"  HTML parser: {len(parser.rows)} rows, first row sample: {parser.rows[0][:5]}")

    # The EDGAR XSL renderer produces TWO header rows:
    #   Row A: "COLUMN 1" | "COLUMN 2" | "COLUMN 3" | "FIGI" | "VALUE" | "SHRS OR" | "SH/" | ...
    #   Row B: "NAME OF ISSUER" | "TITLE OF CLASS" | "CUSIP" | "FIGI" | "(to the nearest dollar)" | "PRN AMT" | "PRN" | ...
    #
    # We use Row B as the definitive header (it has the real column names).
    # Mappings per user spec:
    #   "NAME OF ISSUER"           → nameofissuer  (filer/manager name in CFM format)
    #   "TITLE OF CLASS"           → titleofclass  (security/issuer name)
    #   "CUSIP"                    → cusip
    #   "(to the nearest dollar)"  → value         (Value x1000)
    #   "PRN AMT"                  → sshprnamt     (Shares)

    COL_MAP = {
        # NAME OF ISSUER → nameofissuer
        "nameofissuer":              "nameofissuer",
        "issuer":                    "nameofissuer",
        "issuername":                "nameofissuer",
        # TITLE OF CLASS → titleofclass (security name)
        "titleofclass":              "titleofclass",
        "titleofclass":              "titleofclass",
        "classoftitle":              "titleofclass",
        # CUSIP
        "cusip":                     "cusip",
        # FIGI
        "figi":                      "figi",
        # VALUE / (to the nearest dollar) → value
        "tothenearestdollar":        "value",
        "thenearestdollar":          "value",
        "nearestdollar":             "value",
        "valuetothenearestdollar":   "value",
        "value":                     "value",
        "valuex1000":                "value",
        "marketvalue":               "value",
        "mktval":                    "value",
        # PRN AMT → sshprnamt (Shares)
        "prnamt":                    "sshprnamt",
        "prnamount":                 "sshprnamt",
        "shrsorprnamt":              "sshprnamt",
        "sshprnamt":                 "sshprnamt",
        "shares":                    "sshprnamt",
        "sharesornprincipalamt":     "sshprnamt",
        "amount":                    "sshprnamt",
        # SH/PRN type
        "prn":                       "sshprnamttype",
        "shprn":                     "sshprnamttype",
        "sshprnamttype":             "sshprnamttype",
        # Put/call
        "call":                      "putcall",
        "putcall":                   "putcall",
        "putorcall":                 "putcall",
        # Investment discretion
        "discretion":                "investmentdiscretion",
        "investmentdiscretion":      "investmentdiscretion",
        # Other manager
        "manager":                   "othermanager",
        "othermanager":              "othermanager",
        # Voting
        "sole":                      "voting_sole",
        "shared":                    "voting_shared",
        "none":                      "voting_none",
        "votingauthority":           "votingauthority",
        # Ticker
        "ticker":                    "ticker",
    }

    def normalise(s: str) -> str:
        """Strip all non-alphanumeric chars and lowercase for fuzzy matching."""
        return re.sub(r'[^a-z0-9]', '', s.lower())

    # Find the best header row: the one that scores highest on unambiguous field names.
    # Row B ("NAME OF ISSUER" etc.) scores higher than Row A ("COLUMN 1" etc.)
    # because "cusip", "nameofissuer", "titleofclass" are high-confidence signals.
    HIGH_CONFIDENCE = {"cusip", "nameofissuer", "titleofclass", "prnamt",
                       "tothenearestdollar", "nearestdollar", "sshprnamt"}
    LOW_CONFIDENCE  = {"value", "figi", "prn", "discretion", "manager"}

    def row_score(row):
        hi = sum(1 for c in row if normalise(c) in HIGH_CONFIDENCE
                                or COL_MAP.get(normalise(c)) in HIGH_CONFIDENCE)
        lo = sum(1 for c in row if normalise(c) in LOW_CONFIDENCE
                                or COL_MAP.get(normalise(c)) in LOW_CONFIDENCE)
        return hi * 10 + lo

    scored = [(row_score(row), i, row) for i, row in enumerate(parser.rows)]
    best   = max(scored, key=lambda x: x[0])

    if best[0] == 0:
        _log("  HTML parser: could not identify header row")
        for i, row in enumerate(parser.rows[:4]):
            _log(f"    row {i}: {row}")
        return []

    header_idx = best[1]

    _log(f"  HTML parser: header at row {header_idx}: {parser.rows[header_idx]}")

    raw_headers = parser.rows[header_idx]
    mapped = [COL_MAP.get(normalise(h), normalise(h)) for h in raw_headers]
    _log(f"  HTML parser: mapped columns: {mapped}")

    def is_numeric(s):
        """Return True if s is a non-empty numeric string (allows decimals)."""
        try:
            float(s.replace(",", "").replace(" ", ""))
            return True
        except (ValueError, AttributeError):
            return False

    holdings     = []
    dropped_rows = []
    for row in parser.rows[header_idx + 1:]:
        if len(row) < 3:
            continue
        h = {}
        for i, val in enumerate(row):
            if i >= len(mapped) or not val:
                continue
            col = mapped[i]
            v   = val.strip().replace(",", "")   # strip commas from numbers
            if col not in h or not h[col]:
                h[col] = v
        if not (h.get("cusip") or h.get("nameofissuer")):
            continue
        val_v    = h.get("value",     "")
        shares_v = h.get("sshprnamt", "")
        if (val_v    and not is_numeric(val_v)) or \
           (shares_v and not is_numeric(shares_v)):
            dropped_rows.append(h)
            continue
        holdings.append(h)

    if dropped_rows:
        _log(f"  HTML parser: dropped {len(dropped_rows)} row(s) with non-numeric value/shares")
        examples = (dropped_rows[:2] +
                    (dropped_rows[-2:] if len(dropped_rows) > 2 else []))
        seen = set()
        for h in examples:
            key = str(h)
            if key in seen:
                continue
            seen.add(key)
            _log(f"    example: issuer={h.get('nameofissuer','')!r}  "
                 f"cusip={h.get('cusip','')!r}  "
                 f"value={h.get('value','')!r}  "
                 f"shares={h.get('sshprnamt','')!r}")
    return holdings


def _parse_infotable_xml(xml_text: str) -> list[dict]:
    """Parse 13F-HR information table XML and return a list of holdings."""
    # Strip Clark-notation namespaces: {http://...}tag → tag
    xml_clean = re.sub(r'\{[^}]+\}', '', xml_text)
    # Strip prefix:tag style namespaces
    xml_clean = re.sub(r'<(\w+):',   '<',  xml_clean)
    xml_clean = re.sub(r'</(\w+):',  '</', xml_clean)
    # Strip xmlns declarations (so ET doesn't choke)
    xml_clean = re.sub(r'\s+xmlns(?::\w+)?="[^"]*"', '', xml_clean)

    try:
        root = ET.fromstring(xml_clean)
    except ET.ParseError:
        return []

    def _is_numeric(s):
        try:
            float(str(s).replace(",", "").replace(" ", ""))
            return True
        except (ValueError, AttributeError):
            return False

    holdings     = []
    dropped_rows = []
    ENTRY_TAGS = {"infotable", "infoentry", "holding", "entry"}

    for entry in root.iter():
        if entry.tag.lower() in ENTRY_TAGS:
            h = {}
            for child in entry:
                t = child.tag.lower()
                if t in ("shrsorprnamt", "shrsamt"):
                    for sub in child:
                        h[sub.tag.lower()] = (sub.text or "").strip()
                else:
                    h[t] = (child.text or "").strip()
            if not h:
                continue
            val_v    = h.get("value", "")
            shares_v = h.get("sshprnamt", "")
            if (val_v    and not _is_numeric(val_v)) or \
               (shares_v and not _is_numeric(shares_v)):
                dropped_rows.append(h)
                continue
            holdings.append(h)

    if dropped_rows:
        print(f"    XML parser: dropped {len(dropped_rows)} row(s) with non-numeric value/shares")
        examples = (dropped_rows[:2] +
                    (dropped_rows[-2:] if len(dropped_rows) > 2 else []))
        seen = set()
        for h in examples:
            key = str(h)
            if key in seen:
                continue
            seen.add(key)
            print(f"      example: issuer={h.get('nameofissuer','')!r}  "
                  f"cusip={h.get('cusip','')!r}  "
                  f"value={h.get('value','')!r}  "
                  f"shares={h.get('sshprnamt','')!r}")
    return holdings


def _parse_infotable_file(url: str, ext: str,
                          log=None) -> list[dict]:
    """
    Fetch a holdings file and parse it based on the actual HTTP response
    content-type, not the file extension or URL structure.
    log: callable(str) that receives progress messages (in addition to print).
    """
    def _log(msg):
        print(msg)
        if log:
            log(msg)

    try:
        r = _sec_get(url)
    except requests.RequestException as e:
        _log(f"[!] Could not fetch {url}: {e}")
        return []

    content_type = r.headers.get("Content-Type", "").lower()
    is_html = "html" in content_type

    _log(f"  Fetched ({len(r.text):,} chars, content-type={content_type!r}): {url}")

    if is_html:
        _log("  → Parsing as HTML")
        holdings = _parse_infotable_html(r.text, log=_log)
        if not holdings:
            _log("  HTML parse returned 0 — trying XML fallback")
            holdings = _parse_infotable_xml(r.text)
    else:
        _log("  → Parsing as XML")
        holdings = _parse_infotable_xml(r.text)
        if not holdings:
            _log("  XML parse returned 0 — trying HTML fallback")
            holdings = _parse_infotable_html(r.text, log=_log)

    _log(f"  Parsed {len(holdings)} holdings from this file")
    return holdings




def _accession_from_filename(filename: str) -> str:
    """Extract the accession number from a company.idx filename."""
    base = filename.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r'\.(txt|htm|html)$', '', base, flags=re.I)


# ---------------------------------------------------------------------------
# SIC LOOKUP VIA TICKER
# ---------------------------------------------------------------------------
# SEC publishes company_tickers_exchange.json: ticker → {cik, name, exchange}
# We load it once, then for each holding ticker fetch that issuer's SIC
# from its submissions JSON (also cached).

_TICKER_TO_CIK: dict[str, str] = {}   # ticker (upper) → CIK (zero-padded)
_SIC_CACHE:     dict[str, dict] = {}  # CIK (padded)   → {"sic": .., "sic_description": ..}
_TICKER_INDEX_LOADED = False


def _load_ticker_index() -> None:
    """Download SEC's ticker→CIK mapping once per session."""
    global _TICKER_INDEX_LOADED
    if _TICKER_INDEX_LOADED:
        return
    url = f"{DATA_SEC_BASE}/files/company_tickers_exchange.json"
    print(f"[SIC] Loading ticker index: {url}")
    try:
        r = _sec_get(url)
        data = r.json()
        # Format: {"fields": [...], "data": [[cik, name, ticker, exchange], ...]}
        fields = data.get("fields", [])
        rows   = data.get("data", [])
        cik_i    = fields.index("cik")    if "cik"    in fields else 0
        ticker_i = fields.index("ticker") if "ticker" in fields else 2
        for row in rows:
            try:
                ticker = str(row[ticker_i]).upper().strip()
                cik    = str(row[cik_i]).zfill(10)
                if ticker:
                    _TICKER_TO_CIK[ticker] = cik
            except (IndexError, TypeError):
                pass
        print(f"[SIC] Ticker index loaded: {len(_TICKER_TO_CIK):,} tickers")
    except Exception as e:
        print(f"[SIC] Could not load ticker index: {e}")
    _TICKER_INDEX_LOADED = True


def _fetch_sic_for_ticker(ticker: str) -> dict:
    """
    Look up SIC for a holding by its ticker symbol.
    Returns {"sic": "...", "sic_description": "..."} (empty strings if unknown).
    """
    if not ticker or not str(ticker).strip():
        return {"sic": "", "sic_description": ""}

    _load_ticker_index()
    cik = _TICKER_TO_CIK.get(str(ticker).upper().strip())
    if not cik:
        return {"sic": "", "sic_description": ""}

    if cik in _SIC_CACHE:
        return _SIC_CACHE[cik]

    url = f"{DATA_SEC_BASE}/submissions/CIK{cik}.json"
    try:
        r    = _sec_get(url)
        data = r.json()
        result = {
            "sic":             str(data.get("sic", "")),
            "sic_description": str(data.get("sicDescription", "")),
        }
    except Exception as e:
        print(f"[SIC] Error fetching CIK {cik}: {e}")
        result = {"sic": "", "sic_description": ""}

    _SIC_CACHE[cik] = result
    return result


def fetch_portfolio(row: pd.Series, progress_callback=None) -> pd.DataFrame:
    """
    Given a row from the 13F index DataFrame, fetch the holding details
    and return a DataFrame with issuer name, CUSIP, value, shares, ticker.
    Uses the filing envelope (.txt) from company.idx to find the INFORMATION TABLE XML.
    """
    cik      = str(row["cik"]).strip()
    filename = str(row["filename"]).strip()

    if progress_callback:
        progress_callback(f"Fetching portfolio for CIK {cik}…")

    infotable_files, accepted_dt, date_filed_env, effective_date =         _get_infotable_urls_from_index(filename, progress_callback=progress_callback)

    if not infotable_files:
        if progress_callback:
            progress_callback("  ⚠ Could not locate any infotable documents.")
        return pd.DataFrame(), None

    # Fall back to company.idx date if envelope dates are missing
    fallback_date  = str(row["date_filed"]).strip()
    accepted_dt    = accepted_dt    or fallback_date
    date_filed_env = date_filed_env or fallback_date
    effective_date = effective_date or fallback_date

    print(f"[fetch_portfolio] Accepted: {accepted_dt}  Filed: {date_filed_env}")
    if progress_callback:
        progress_callback(f"Parsing {len(infotable_files)} infotable file(s)…")

    # Parse all files, keeping XML and HTML results separate
    xml_holdings:  list[dict] = []
    html_holdings: list[dict] = []

    for file_info in infotable_files:
        url = file_info["url"]
        ext = file_info["ext"]
        print(f"[fetch_portfolio] Fetching {ext.upper()}: {url}")
        if progress_callback:
            progress_callback(f"  Fetching infotable ({ext.upper()}): {url}")
        file_holdings = _parse_infotable_file(url, ext, log=progress_callback)

        # Determine whether the actual parsed content came from XML or HTML
        # by checking which parser succeeded (ext is the declared type, but
        # content-type detection may have used the other parser)
        r_check = _sec_get(url)
        is_html_content = "html" in r_check.headers.get("Content-Type", "").lower()

        if is_html_content:
            html_holdings.extend(file_holdings)
        else:
            xml_holdings.extend(file_holdings)

    # Merge: XML rows take priority; HTML rows only fill gaps not covered by XML
    xml_cusips = {h.get("cusip", "").strip() for h in xml_holdings
                  if h.get("cusip", "").strip()}

    all_holdings = list(xml_holdings)  # start with all XML rows
    for h in html_holdings:
        cusip = h.get("cusip", "").strip()
        if not cusip or cusip not in xml_cusips:
            all_holdings.append(h)
            if cusip:
                xml_cusips.add(cusip)  # prevent HTML duplicates too

    print(f"[fetch_portfolio] XML: {len(xml_holdings)} rows, "
          f"HTML: {len(html_holdings)} rows, "
          f"merged: {len(all_holdings)} rows")
    if progress_callback:
        progress_callback(f"  XML: {len(xml_holdings)} rows, "
                          f"HTML: {len(html_holdings)} rows, "
                          f"merged: {len(all_holdings)} total")

    holdings = all_holdings

    if not holdings:
        if progress_callback:
            progress_callback("  ⚠ No holdings parsed from any file.")
        return pd.DataFrame(), None

    df = pd.DataFrame(holdings)

    # Normalise column names across different filer schemas.
    # Each source tag maps to exactly one target name — no two tags share a target.
    rename = {}
    for col in df.columns:
        lc = col.lower()
        if lc in ("nameofissuer", "issuer", "issuername", "name"):
            rename[col] = "issuer_name"
        elif lc == "cusip":
            rename[col] = "cusip"
        elif lc in ("ticker", "tickersymbol"):
            rename[col] = "ticker"
        elif lc in ("value", "mktval", "marketvalue"):
            rename[col] = "value_x1000"
        elif lc in ("sshprnamt", "sharesamt"):
            rename[col] = "shares"
        elif lc == "sshprnamttype":
            rename[col] = "share_type"   # separate column, not "shares"
    df = df.rename(columns=rename)

    # Drop any columns that ended up duplicated (safety net for unusual schemas)
    df = df.loc[:, ~df.columns.duplicated()]

    # Keep only the columns we care about (in order), adding blanks for any missing
    want = ["issuer_name", "cusip", "ticker", "value_x1000", "shares", "share_type"]
    for col in want:
        if col not in df.columns:
            df[col] = ""
    df = df[want]

    # Attach filer metadata
    df.insert(0, "filer_name",      row["company_name"])
    df.insert(1, "cik",             cik)
    df.insert(2, "form_type",       row.get("form_type", ""))
    df.insert(3, "accepted_dt",     accepted_dt)
    df.insert(4, "date_filed",      date_filed_env)
    df.insert(5, "effective_date",  effective_date)
    df.insert(6, "filing_quarter",  row.get("filing_quarter", ""))

    # Look up SIC for each holding by its ticker (uses cached index + per-CIK cache)
    sic_data = df["ticker"].apply(_fetch_sic_for_ticker)
    df["sic"]             = sic_data.apply(lambda d: d["sic"])
    df["sic_description"] = sic_data.apply(lambda d: d["sic_description"])

    env_dates = {
        "accepted_dt":    accepted_dt,
        "effective_date": effective_date,
    }
    return df, env_dates


# ---------------------------------------------------------------------------
# PART 3 – TKINTER GUI
# ---------------------------------------------------------------------------

SEARCH_DEBOUNCE_MS = 300


class App13F(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SEC 13F Filing Tool")
        self.geometry("1150x800")
        self.resizable(True, True)
        self.configure(bg="#f5f5f5")

        self.index_df: pd.DataFrame = pd.DataFrame()
        self.selected_rows: list[int] = []
        self.portfolio_df: pd.DataFrame = pd.DataFrame()
        self._search_after_id = None

        self._ensure_user_agent()
        self._build_ui()
        # Auto-load last saved state if it exists
        import os
        _auto_save = os.path.join(os.path.expanduser("~"), ".13f_tool_autosave.13f")
        if os.path.exists(_auto_save):
            self.after(200, lambda: self._load_state(_auto_save))

    def _ensure_user_agent(self):
        """Prompt for User-Agent if not set, and refuse to run without one."""
        global USER_AGENT
        if USER_AGENT and "@" in USER_AGENT:
            return
        self.withdraw()   # hide main window during prompt
        while not USER_AGENT or "@" not in USER_AGENT:
            val = simpledialog.askstring(
                "SEC EDGAR User-Agent Required",
                "SEC EDGAR requires your name and email to make requests.\n\n"
                "Enter in the format:  Firstname Lastname your@email.com\n\n"
                "(This is sent as the HTTP User-Agent header only.)",
                parent=self,
            )
            if val is None:   # user cancelled
                self.destroy()
                raise SystemExit("User-Agent is required to use this tool.")
            val = val.strip()
            if "@" in val and " " in val:
                USER_AGENT = val
            else:
                messagebox.showwarning(
                    "Invalid format",
                    "Please enter your name AND email, e.g.:\n"
                    "  Jane Smith jane@example.com")
        self.deiconify()

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        top = tk.Frame(self, bg="#2c3e50", pady=8)
        top.pack(fill="x")

        tk.Label(top, text="SEC 13F Filing Tool", bg="#2c3e50", fg="white",
                 font=("Helvetica", 16, "bold")).pack(side="left", padx=16)

        ctrl = tk.Frame(top, bg="#2c3e50")
        ctrl.pack(side="right", padx=16)

        # Build list of all quarters from 2000 Q1 to current quarter
        _today = date.today()
        _cur_y, _cur_q = _today.year, (_today.month - 1) // 3 + 1
        _quarters = []
        for y in range(2000, _cur_y + 1):
            for q in range(1, 5):
                if y == _cur_y and q > _cur_q:
                    break
                _quarters.append(f"{y} Q{q}")
        _quarters_rev = list(reversed(_quarters))   # newest first for default

        tk.Label(ctrl, text="From quarter:", bg="#2c3e50",
                 fg="white").grid(row=0, column=0, padx=4)
        self.start_qtr_var = tk.StringVar(value=f"{_cur_y - 2} Q1")
        ttk.Combobox(ctrl, textvariable=self.start_qtr_var,
                     values=_quarters, state="readonly",
                     width=10).grid(row=0, column=1)

        tk.Label(ctrl, text="To quarter:", bg="#2c3e50",
                 fg="white").grid(row=0, column=2, padx=4)
        self.end_qtr_var = tk.StringVar(value=f"{_cur_y} Q{_cur_q}")
        ttk.Combobox(ctrl, textvariable=self.end_qtr_var,
                     values=_quarters_rev, state="readonly",
                     width=10).grid(row=0, column=3)

        tk.Button(ctrl, text="Fetch Index", command=self._fetch_index,
                  bg="#27ae60", fg="white", font=("Helvetica", 10, "bold"),
                  padx=10).grid(row=0, column=4, padx=12)

        save_load = tk.Frame(top, bg="#2c3e50")
        save_load.pack(side="left", padx=12)
        tk.Button(save_load, text="💾 Save State", command=self._save_state,
                  bg="#8e44ad", fg="white", padx=8).pack(side="left", padx=2)
        tk.Button(save_load, text="📂 Load State", command=self._load_state,
                  bg="#2c3e50", fg="#aaa", padx=8,
                  relief="groove").pack(side="left", padx=2)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=6)

        self.tab_search    = tk.Frame(self.nb, bg="#f5f5f5")
        self.tab_browse    = tk.Frame(self.nb, bg="#f5f5f5")
        self.tab_watchlist = tk.Frame(self.nb, bg="#f5f5f5")
        self.tab_queue     = tk.Frame(self.nb, bg="#f5f5f5")
        self.tab_portfolio = tk.Frame(self.nb, bg="#f5f5f5")
        self.tab_overlap   = tk.Frame(self.nb, bg="#f5f5f5")
        self.tab_stats     = tk.Frame(self.nb, bg="#f5f5f5")
        self.tab_log       = tk.Frame(self.nb, bg="#f5f5f5")

        self.nb.add(self.tab_search,    text="  🔍 Text Search  ")
        self.nb.add(self.tab_browse,    text="  📂 Index Browse  ")
        self.nb.add(self.tab_watchlist, text="  📝 Watchlist  ")
        self.nb.add(self.tab_queue,     text="  🗂 Queue (0)  ")
        self.nb.add(self.tab_portfolio, text="  📊 Portfolio  ")
        self.nb.add(self.tab_overlap,   text="  📋 Overlap  ")
        self.nb.add(self.tab_stats,     text="  📈 Statistics  ")
        self.nb.add(self.tab_log,       text="  📋 Log  ")

        self._build_search_tab()
        self._build_browse_tab()
        self._build_watchlist_tab()
        self._build_queue_tab()
        self._build_portfolio_tab()
        self._build_overlap_tab()
        self._build_stats_tab()
        self._build_log_tab()

        self.status_var = tk.StringVar(
            value="Ready. Select a quarter range and click Fetch Index.")
        tk.Label(self, textvariable=self.status_var, bg="#ecf0f1",
                 anchor="w", relief="sunken", padx=6).pack(fill="x", side="bottom")

    def _build_search_tab(self):
        f = self.tab_search
        row0 = tk.Frame(f, bg="#f5f5f5")
        row0.pack(fill="x", padx=8, pady=6)

        tk.Label(row0, text="Search company name:", bg="#f5f5f5").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._schedule_search())
        tk.Entry(row0, textvariable=self.search_var, width=40).pack(side="left", padx=6)
        tk.Button(row0, text="Clear", command=lambda: self.search_var.set(""),
                  bg="#bdc3c7").pack(side="left")

        cols = ("company_name", "form_type", "filing_quarter", "cik")
        self.search_tree = self._make_tree(f, cols)

        # Right-click context menu on the search results tree
        self._search_ctx_menu = tk.Menu(self, tearoff=0)
        self._search_ctx_menu.add_command(
            label="Copy Company Name to Watchlist",
            command=self._search_copy_to_watchlist)
        self.search_tree.bind("<Button-3>",  self._search_show_ctx)   # Windows/Linux
        self.search_tree.bind("<Button-2>",  self._search_show_ctx)   # macOS

        btn_row = tk.Frame(f, bg="#f5f5f5")
        btn_row.pack(fill="x", padx=8, pady=4)
        tk.Button(btn_row, text="➕ Add Selected to Queue",
                  command=lambda: self._add_from_tree(self.search_tree),
                  bg="#2980b9", fg="white", padx=10).pack(side="left")
        tk.Button(btn_row, text="⬇ Fetch Portfolios for Queue",
                  command=self._fetch_portfolios,
                  bg="#8e44ad", fg="white", padx=10).pack(side="right", padx=6)

    def _search_show_ctx(self, event):
        """Select the row under the cursor and show the right-click menu."""
        item = self.search_tree.identify_row(event.y)
        if item:
            self.search_tree.selection_set(item)
            self._search_ctx_menu.tk_popup(event.x_root, event.y_root)

    def _search_copy_to_watchlist(self):
        """Copy the Company Name of each selected search-tree row to the watchlist."""
        added = 0
        for item in self.search_tree.selection():
            vals = self.search_tree.item(item, "values")
            if not vals:
                continue
            name = vals[0]   # company_name is the first column
            if name:
                before = len(self._watchlist_get_names())
                self._watchlist_add_name(name)
                if len(self._watchlist_get_names()) > before:
                    added += 1
        msg = (f"Added {added} name(s) to watchlist."
               if added else "No new names added (already present or nothing selected).")
        self._set_status(msg)

    def _build_browse_tab(self):
        f = self.tab_browse
        cols = ("company_name", "form_type", "filing_quarter", "cik")
        self.browse_tree = self._make_tree(f, cols)

        btn_row = tk.Frame(f, bg="#f5f5f5")
        btn_row.pack(fill="x", padx=8, pady=4)
        tk.Button(btn_row, text="➕ Add Selected to Queue",
                  command=lambda: self._add_from_tree(self.browse_tree),
                  bg="#2980b9", fg="white", padx=10).pack(side="left")

    def _build_watchlist_tab(self):
        f = self.tab_watchlist
        self._watchlist_file_path = None   # path of the currently loaded file

        # ── Instruction label ─────────────────────────────────────────────
        tk.Label(f,
                 text="Type or paste manager names below — one per line. "
                      "You can also load/save a file, or right-click a row "
                      "in Text Search to copy it here.",
                 bg="#f5f5f5", fg="#555", wraplength=900, justify="left",
                 anchor="w").pack(fill="x", padx=8, pady=(8, 2))

        # ── Button row ────────────────────────────────────────────────────
        btn_frame = tk.Frame(f, bg="#f5f5f5")
        btn_frame.pack(fill="x", padx=8, pady=4)

        tk.Button(btn_frame, text="📂 Load File",
                  command=self._watchlist_load_file,
                  bg="#2980b9", fg="white", padx=10).pack(side="left")
        tk.Button(btn_frame, text="💾 Save File",
                  command=self._watchlist_save_file,
                  bg="#2c3e50", fg="white", padx=10).pack(side="left", padx=4)
        tk.Button(btn_frame, text="💾 Save As…",
                  command=self._watchlist_save_as,
                  bg="#2c3e50", fg="white", padx=10).pack(side="left", padx=4)
        tk.Button(btn_frame, text="🗑 Clear",
                  command=self._watchlist_clear,
                  bg="#e74c3c", fg="white", padx=10).pack(side="left", padx=4)
        tk.Button(btn_frame, text="🔍 Search & Add All to Queue",
                  command=self._watchlist_search_all,
                  bg="#27ae60", fg="white", padx=10).pack(side="left", padx=8)

        self.wl_file_label = tk.Label(btn_frame, text="No file loaded",
                                       bg="#f5f5f5", fg="#888",
                                       font=("Helvetica", 9, "italic"))
        self.wl_file_label.pack(side="right", padx=8)

        # ── Editable Text widget (the watchlist editor) ───────────────────
        edit_frame = tk.Frame(f, bg="#f5f5f5")
        edit_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        vsb = ttk.Scrollbar(edit_frame, orient="vertical")
        self.wl_text = tk.Text(edit_frame, wrap="none",
                                font=("Courier", 11),
                                bg="#ffffff", fg="#1a1a1a",
                                insertbackground="#1a1a1a",
                                yscrollcommand=vsb.set,
                                undo=True, relief="solid", bd=1)
        vsb.configure(command=self.wl_text.yview)
        vsb.pack(side="right", fill="y")
        self.wl_text.pack(fill="both", expand=True)

        # Update file label title on any edit
        self.wl_text.bind("<<Modified>>", self._watchlist_on_edit)

    def _watchlist_get_names(self) -> list[str]:
        """Return the current non-empty lines from the watchlist editor."""
        raw = self.wl_text.get("1.0", "end")
        return [ln.strip() for ln in raw.splitlines() if ln.strip()]

    def _watchlist_on_edit(self, event=None):
        """Mark title as modified when text changes."""
        n = len(self._watchlist_get_names())
        path = self._watchlist_file_path
        label = (f"✏  {path}  ({n} names)" if path
                 else f"Unsaved  ({n} names)")
        self.wl_file_label.configure(text=label)
        self.wl_text.edit_modified(False)   # reset flag so next edit fires again

    def _watchlist_load_file(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Select manager name file",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except Exception as e:
            messagebox.showerror("File Error", str(e))
            return
        self.wl_text.delete("1.0", "end")
        self.wl_text.insert("1.0", text.rstrip())
        self._watchlist_file_path = path
        self.wl_text.edit_modified(False)
        self._watchlist_on_edit()
        self._set_status(f"Watchlist loaded from {path}")

    def _watchlist_save_file(self):
        if not self._watchlist_file_path:
            self._watchlist_save_as()
            return
        import os
        if os.path.exists(self._watchlist_file_path):
            if not messagebox.askyesno(
                    "Overwrite file?",
                    f"The file already exists:\n\n{self._watchlist_file_path}\n\n"
                    "Do you want to overwrite it?"):
                return
        self._do_save(self._watchlist_file_path)

    def _watchlist_save_as(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            title="Save watchlist as",
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        self._watchlist_file_path = path
        self._do_save(path)

    def _do_save(self, path):
        try:
            text = self.wl_text.get("1.0", "end").rstrip() + "\n"
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as e:
            messagebox.showerror("Save Error", str(e))
            return
        self.wl_text.edit_modified(False)
        self._watchlist_on_edit()
        self._set_status(f"Watchlist saved to {path}")

    def _watchlist_clear(self):
        if messagebox.askyesno("Clear watchlist",
                               "Clear all names from the watchlist?"):
            self.wl_text.delete("1.0", "end")
            self._watchlist_file_path = None
            self._watchlist_on_edit()
            self._set_status("Watchlist cleared.")

    def _watchlist_add_name(self, name: str):
        """Append a name to the watchlist editor if not already present."""
        existing = self._watchlist_get_names()
        if name in existing:
            return
        # Move to end of file and insert
        current = self.wl_text.get("1.0", "end").rstrip()
        new_text = (current + "\n" + name) if current else name
        self.wl_text.delete("1.0", "end")
        self.wl_text.insert("1.0", new_text)
        self._watchlist_on_edit()

    def _watchlist_search_all(self):
        if self.index_df.empty:
            messagebox.showinfo("No index",
                                "Please fetch the index first before searching.")
            return
        names = self._watchlist_get_names()
        if not names:
            messagebox.showinfo("Empty list",
                                "Add manager names to the watchlist first.")
            return

        added_total  = 0
        result_lines = []

        for name in names:
            # Use the same search as the Text Search tab
            query = name.lower()
            mask  = self.index_df["company_name"].str.lower().str.contains(
                query, na=False, regex=False)
            matches = self.index_df[mask]
            count   = 0
            for idx in matches.index:
                row   = self.index_df.loc[idx]
                cik   = row["cik"]
                filed = row["date_filed"]
                if not any(r["cik"] == cik and r["date_filed"] == filed
                           for r in self._queue()):
                    self.selected_rows.append(idx)
                    count += 1
                    added_total += 1
            status = f"{count} filing(s) added" if count else "no matches"
            result_lines.append(f"{name}  →  [{status}]")

        self._refresh_queue_tree()
        self._update_queue_tab_title()

        # Show results in a popup so the editor text is preserved
        result_win = tk.Toplevel(self)
        result_win.title("Search Results")
        result_win.geometry("700x400")
        txt = tk.Text(result_win, font=("Courier", 10), wrap="none",
                      bg="#f9f9f9", relief="flat")
        sb  = ttk.Scrollbar(result_win, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(fill="both", expand=True, padx=6, pady=6)
        txt.insert("1.0", "\n".join(result_lines))
        txt.configure(state="disabled")

        self._set_status(
            f"Watchlist search done: {added_total} filings added to queue "
            f"from {len(names)} manager names.")

    def _build_queue_tab(self):
        f = self.tab_queue

        btn_row = tk.Frame(f, bg="#f5f5f5")
        btn_row.pack(fill="x", padx=8, pady=6)
        tk.Button(btn_row, text="🗑 Remove Selected",
                  command=self._remove_from_queue,
                  bg="#e74c3c", fg="white", padx=10).pack(side="left")
        tk.Button(btn_row, text="🗑 Clear All",
                  command=self._clear_queue,
                  bg="#c0392b", fg="white", padx=10).pack(side="left", padx=6)
        tk.Button(btn_row, text="⬇ Fetch Portfolios for Queue",
                  command=self._fetch_portfolios,
                  bg="#8e44ad", fg="white", padx=10).pack(side="right", padx=6)

        # ── Filter bar ────────────────────────────────────────────────────────
        flt = tk.Frame(f, bg="#e8ecf0", bd=1, relief="groove")
        flt.pack(fill="x", padx=8, pady=(0, 4))

        tk.Label(flt, text="Company:", bg="#e8ecf0",
                 font=("Helvetica", 9)).pack(side="left", padx=(8, 2))
        self.q_search_var = tk.StringVar()
        self.q_search_var.trace_add("write", lambda *_: self._apply_queue_filter())
        tk.Entry(flt, textvariable=self.q_search_var,
                 width=22).pack(side="left", padx=(0, 8))

        tk.Label(flt, text="Form Type:", bg="#e8ecf0",
                 font=("Helvetica", 9)).pack(side="left", padx=(0, 2))
        self.q_ftype_var = tk.StringVar(value="All")
        self.q_ftype_cb  = ttk.Combobox(flt, textvariable=self.q_ftype_var,
                                          state="readonly", width=10)
        self.q_ftype_cb.pack(side="left", padx=(0, 8))
        self.q_ftype_cb.bind("<<ComboboxSelected>>",
                              lambda _: self._apply_queue_filter())

        tk.Label(flt, text="Filing Quarter:", bg="#e8ecf0",
                 font=("Helvetica", 9)).pack(side="left", padx=(0, 2))
        self.q_qtr_var = tk.StringVar(value="All")
        self.q_qtr_cb  = ttk.Combobox(flt, textvariable=self.q_qtr_var,
                                        state="readonly", width=10)
        self.q_qtr_cb.pack(side="left", padx=(0, 8))
        self.q_qtr_cb.bind("<<ComboboxSelected>>",
                            lambda _: self._apply_queue_filter())

        tk.Button(flt, text="Clear filters",
                  command=self._clear_queue_filters,
                  bg="#bdc3c7", padx=6).pack(side="right", padx=6)

        self.q_filter_label = tk.Label(flt, text="", bg="#e8ecf0",
                                        fg="#888", font=("Helvetica", 9))
        self.q_filter_label.pack(side="right", padx=4)

        cols = ("company_name", "form_type", "date_filed", "filing_quarter", "cik")
        self.queue_tree = self._make_tree(f, cols)

        # Right-click context menu for delete
        self._queue_ctx = tk.Menu(self, tearoff=0)
        self._queue_ctx.add_command(label="🗑 Delete Selected Row(s)",
                                     command=self._remove_from_queue)
        self.queue_tree.bind("<Button-3>", self._queue_right_click)
        self.queue_tree.bind("<Button-2>", self._queue_right_click)  # macOS

    def _queue_right_click(self, event):
        """Select the row under cursor and show the context menu."""
        row = self.queue_tree.identify_row(event.y)
        if row:
            if row not in self.queue_tree.selection():
                self.queue_tree.selection_set(row)
        self._queue_ctx.tk_popup(event.x_root, event.y_root)

    def _update_queue_filter_dropdowns(self):
        """Refresh Form Type and Filing Quarter dropdown options from current queue."""
        rows = self._queue()
        if not rows:
            self.q_ftype_cb["values"] = ["All"]
            self.q_qtr_cb["values"]   = ["All"]
            return
        df = pd.DataFrame([r.to_dict() for r in rows])
        ftypes   = ["All"] + sorted(df["form_type"].dropna().unique().tolist())
        quarters = ["All"] + sorted(df["filing_quarter"].dropna().unique().tolist())
        self.q_ftype_cb["values"] = ftypes
        self.q_qtr_cb["values"]   = quarters

    def _apply_queue_filter(self):
        """Filter the queue Treeview without changing self.selected_rows."""
        rows = self._queue()
        if not rows:
            self.queue_tree.delete(*self.queue_tree.get_children())
            return
        df = pd.DataFrame([r.to_dict() for r in rows])

        search = self.q_search_var.get().strip().lower()
        ftype  = self.q_ftype_var.get()
        qtr    = self.q_qtr_var.get()

        mask = pd.Series(True, index=df.index)
        if search:
            mask &= df["company_name"].str.lower().str.contains(
                search, na=False, regex=False)
        if ftype and ftype != "All" and "form_type" in df.columns:
            mask &= df["form_type"] == ftype
        if qtr and qtr != "All" and "filing_quarter" in df.columns:
            mask &= df["filing_quarter"] == qtr

        filtered = df[mask]
        cols = ("company_name", "form_type", "date_filed", "filing_quarter", "cik")
        self._fast_populate_tree(self.queue_tree,
                                 filtered[[c for c in cols if c in filtered.columns]])
        n_shown = len(filtered)
        n_total = len(rows)
        self.q_filter_label.configure(
            text=f"{n_shown} / {n_total}" if n_shown < n_total else "")

    def _clear_queue_filters(self):
        self.q_search_var.set("")
        self.q_ftype_var.set("All")
        self.q_qtr_var.set("All")
        self._apply_queue_filter()

    # ── Save / Load state ─────────────────────────────────────────────────────
    def _save_state(self):
        """Save the full application state to a pickle file."""
        import pickle
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            title="Save application state",
            defaultextension=".13f",
            filetypes=[("13F state files", "*.13f"), ("All files", "*.*")])
        if not path:
            return
        state = {
            "version":        2,
            # Index
            "index_df":       self.index_df,
            "start_qtr":      self.start_qtr_var.get(),
            "end_qtr":        self.end_qtr_var.get(),
            # Queue
            "selected_rows":  self.selected_rows,
            # Portfolio
            "portfolio_df":   self.portfolio_df,
            # Watchlist
            "watchlist_text": self.wl_text.get("1.0", "end").rstrip(),
            "watchlist_path": self._watchlist_file_path,
            # Portfolio filter settings
            "pf_filer":       self.pf_filer_var.get(),
            "pf_qtr":         self.pf_qtr_var.get(),
            "pf_issuer":      self.pf_issuer_var.get(),
            "pf_wt_min":      self.pf_wt_min_var.get(),
            "pf_wt_max":      self.pf_wt_max_var.get(),
            "pf_val_min":     self.pf_val_min_var.get(),
            "pf_val_max":     self.pf_val_max_var.get(),
            # Stats filter settings
            "st_filer":       self.st_filer_var.get(),
            "st_qtr":         self.st_qtr_var.get(),
            "st_ftype":       self.st_ftype_var.get(),
            # Overlap filter settings
            "ov_min_mgrs":    self.overlap_min_var.get(),
            "ov_sort":        self.overlap_sort_var.get(),
            # Time-series traces
            "ts_traces":      getattr(self, "_ts_traces", []),
        }
        try:
            with open(path, "wb") as fh:
                pickle.dump(state, fh)
            # Also write to autosave location for auto-load on restart
            import os
            _auto = os.path.join(os.path.expanduser("~"), ".13f_tool_autosave.13f")
            with open(_auto, "wb") as fh:
                pickle.dump(state, fh)
            self._set_status(f"State saved to {path}")
        except Exception as e:
            messagebox.showerror("Save Error", str(e))

    def _load_state(self, path: str = ""):
        """Load application state from a pickle file."""
        import pickle
        from tkinter import filedialog
        if not path:
            path = filedialog.askopenfilename(
                title="Load application state",
                filetypes=[("13F state files", "*.13f"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as fh:
                state = pickle.load(fh)
        except Exception as e:
            messagebox.showerror("Load Error", str(e))
            return

        # Index — set quarter vars WITHOUT triggering auto-fetch
        self.index_df = state.get("index_df", pd.DataFrame())
        self._loading_state = True   # suppress _fetch_index during load
        try:
            if state.get("start_qtr"):
                self.start_qtr_var.set(state["start_qtr"])
            if state.get("end_qtr"):
                self.end_qtr_var.set(state["end_qtr"])
        finally:
            self._loading_state = False

        # Queue
        self.selected_rows = state.get("selected_rows", [])

        # Portfolio
        self.portfolio_df = state.get("portfolio_df", pd.DataFrame())

        # Watchlist
        wl_text = state.get("watchlist_text", "")
        self.wl_text.delete("1.0", "end")
        if wl_text:
            self.wl_text.insert("1.0", wl_text)
        self._watchlist_file_path = state.get("watchlist_path")
        self._watchlist_on_edit()

        # Restore filter settings
        for var, key in [
            (self.pf_filer_var,  "pf_filer"),
            (self.pf_qtr_var,    "pf_qtr"),
            (self.pf_issuer_var, "pf_issuer"),
            (self.pf_wt_min_var, "pf_wt_min"),
            (self.pf_wt_max_var, "pf_wt_max"),
            (self.pf_val_min_var,"pf_val_min"),
            (self.pf_val_max_var,"pf_val_max"),
            (self.st_filer_var,  "st_filer"),
            (self.st_qtr_var,    "st_qtr"),
            (self.st_ftype_var,  "st_ftype"),
        ]:
            if key in state:
                var.set(state[key])

        if "ov_min_mgrs" in state:
            self.overlap_min_var.set(state["ov_min_mgrs"])
        if "ov_sort" in state:
            self.overlap_sort_var.set(state["ov_sort"])

        # Time-series traces
        self._ts_traces = state.get("ts_traces", [])

        # Refresh all derived views
        if not self.index_df.empty:
            self._fast_populate_tree(self.browse_tree, self.index_df)
            self._apply_search()
        self._refresh_queue_tree()
        self._update_queue_tab_title()
        self._update_queue_filter_dropdowns()
        if not self.portfolio_df.empty:
            n = len(self.portfolio_df)
            self.port_count_label.configure(
                text=f"{n} holdings across "
                     f"{self.portfolio_df['filer_name'].nunique()} filers")
            self._apply_portfolio_filter()
            self._refresh_overlap_tab()
            self._refresh_stats_tab()

        self._set_status(f"State loaded from {path}")

    def _build_portfolio_tab(self):
        f = self.tab_portfolio

        # ── Top button row ─────────────────────────────────────────────────
        btn_row = tk.Frame(f, bg="#f5f5f5")
        btn_row.pack(fill="x", padx=8, pady=(6, 2))
        tk.Button(btn_row, text="Export Portfolio CSV",
                  command=self._export_csv, bg="#27ae60", fg="white",
                  padx=10).pack(side="right")
        tk.Button(btn_row, text="📊 Chart Holdings",
                  command=self._chart_holdings, bg="#2980b9", fg="white",
                  padx=10).pack(side="right", padx=6)
        tk.Button(btn_row, text="🗑 Clear Time Chart",
                  command=self._clear_timeseries_chart, bg="#e74c3c", fg="white",
                  padx=10).pack(side="right", padx=6)
        self.port_count_label = tk.Label(btn_row, text="No data yet.", bg="#f5f5f5")
        self.port_count_label.pack(side="left")

        # ── Filter bar ─────────────────────────────────────────────────────
        flt = tk.Frame(f, bg="#e8ecf0", bd=1, relief="groove")
        flt.pack(fill="x", padx=8, pady=2)

        def lbl(text):
            tk.Label(flt, text=text, bg="#e8ecf0", fg="#444",
                     font=("Helvetica", 9)).pack(side="left", padx=(8, 2))

        DEBOUNCE_MS = 300

        lbl("Filer:")
        self.pf_filer_var = tk.StringVar()
        self.pf_filer_var.trace_add("write", lambda *_: self._schedule_port_filter())
        tk.Entry(flt, textvariable=self.pf_filer_var,
                 width=18).pack(side="left", padx=(0, 6))

        lbl("Quarter:")
        self.pf_qtr_var = tk.StringVar()
        self.pf_qtr_var.trace_add("write", lambda *_: self._schedule_port_filter())
        tk.Entry(flt, textvariable=self.pf_qtr_var,
                 width=9).pack(side="left", padx=(0, 6))

        lbl("Issuer:")
        self.pf_issuer_var = tk.StringVar()
        self.pf_issuer_var.trace_add("write", lambda *_: self._schedule_port_filter())
        tk.Entry(flt, textvariable=self.pf_issuer_var,
                 width=18).pack(side="left", padx=(0, 6))

        lbl("Weight % ≥")
        self.pf_wt_min_var = tk.StringVar()
        self.pf_wt_min_var.trace_add("write", lambda *_: self._schedule_port_filter())
        tk.Entry(flt, textvariable=self.pf_wt_min_var,
                 width=6).pack(side="left", padx=(0, 2))
        lbl("≤")
        self.pf_wt_max_var = tk.StringVar()
        self.pf_wt_max_var.trace_add("write", lambda *_: self._schedule_port_filter())
        tk.Entry(flt, textvariable=self.pf_wt_max_var,
                 width=6).pack(side="left", padx=(0, 6))

        lbl("Value($000s) ≥")
        self.pf_val_min_var = tk.StringVar()
        self.pf_val_min_var.trace_add("write", lambda *_: self._schedule_port_filter())
        tk.Entry(flt, textvariable=self.pf_val_min_var,
                 width=10).pack(side="left", padx=(0, 2))
        lbl("≤")
        self.pf_val_max_var = tk.StringVar()
        self.pf_val_max_var.trace_add("write", lambda *_: self._schedule_port_filter())
        tk.Entry(flt, textvariable=self.pf_val_max_var,
                 width=10).pack(side="left", padx=(0, 6))

        tk.Button(flt, text="Clear filters",
                  command=self._clear_portfolio_filters,
                  bg="#bdc3c7", padx=6).pack(side="right", padx=6)

        self.port_filter_label = tk.Label(flt, text="", bg="#e8ecf0",
                                          fg="#888", font=("Helvetica", 9))
        self.port_filter_label.pack(side="right", padx=4)

        self._port_filter_after = None

        # ── Table ──────────────────────────────────────────────────────────
        cols = ("filing_quarter", "filer_name", "form_type", "accepted_dt",
                "issuer_name", "cusip", "ticker",
                "weight_%", "value_x1000", "shares", "sic", "sic_description")
        self.port_tree = self._make_tree(f, cols)
        self.port_tree.bind("<Double-1>", self._on_portfolio_row_click)
        self.port_tree.bind("<Button-3>",  self._on_portfolio_row_right_click)
        self.port_tree.bind("<Button-2>",  self._on_portfolio_row_right_click)  # macOS

        # Right-click context menu for portfolio rows
        self._port_ctx = tk.Menu(self, tearoff=0)
        self._port_ctx.add_command(
            label="📈 Plot Weight Over Time",
            command=self._port_ctx_plot_weight)
        self._port_ctx.add_command(
            label="📊 Show Holdings Detail",
            command=self._port_ctx_show_detail)

    def _build_log_tab(self):
        f = self.tab_log

        btn_row = tk.Frame(f, bg="#f5f5f5")
        btn_row.pack(fill="x", padx=8, pady=6)
        tk.Button(btn_row, text="Clear Log", command=self._clear_log,
                  bg="#bdc3c7", padx=10).pack(side="right")
        tk.Button(btn_row, text="Copy All", command=self._log_copy_all,
                  bg="#2980b9", fg="white", padx=10).pack(side="right", padx=6)
        tk.Button(btn_row, text="Copy Selected", command=self._log_copy_selected,
                  bg="#2c3e50", fg="white", padx=10).pack(side="right", padx=6)
        tk.Label(btn_row, text="All status messages with timestamps  (select text then Copy Selected, or Copy All)",
                 bg="#f5f5f5", fg="#555").pack(side="left")

        self.log_text = tk.Text(f, wrap="word", state="disabled",
                                bg="#1e1e1e", fg="#d4d4d4",
                                font=("Courier", 10), relief="flat",
                                padx=6, pady=4)
        vsb = ttk.Scrollbar(f, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True, padx=(8, 0), pady=(0, 8))

        # Colour tags
        self.log_text.tag_configure("blue",   foreground="#4fc3f7")
        self.log_text.tag_configure("normal", foreground="#d4d4d4")

        # Right-click context menu
        self._log_ctx = tk.Menu(self, tearoff=0)
        self._log_ctx.add_command(label="Copy Selected", command=self._log_copy_selected)
        self._log_ctx.add_command(label="Copy All",      command=self._log_copy_all)
        self._log_ctx.add_separator()
        self._log_ctx.add_command(label="Select All",    command=self._log_select_all)
        self._log_ctx.add_separator()
        self._log_ctx.add_command(label="Clear Log",     command=self._clear_log)
        self.log_text.bind("<Button-3>",
                           lambda e: self._log_ctx.tk_popup(e.x_root, e.y_root))
        self.log_text.bind("<Button-2>",   # macOS
                           lambda e: self._log_ctx.tk_popup(e.x_root, e.y_root))

    def _log(self, msg: str, colour: str = "normal"):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}]  {msg}\n", colour)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _log_blue(self, msg: str):
        self._log(msg, colour="blue")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _log_copy_all(self):
        text = self.log_text.get("1.0", "end").rstrip()
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._set_status(f"Log copied to clipboard ({len(text.splitlines())} lines).")

    def _log_copy_selected(self):
        try:
            text = self.log_text.get("sel.first", "sel.last")
        except tk.TclError:
            text = ""
        if text:
            self.clipboard_clear()
            self.clipboard_append(text)
            self._set_status("Selected log text copied to clipboard.")
        else:
            self._log_copy_all()   # fall back to copy-all if nothing selected

    def _log_select_all(self):
        self.log_text.tag_add("sel", "1.0", "end")

    # ── Helpers ────────────────────────────────────────────────────────────
    def _make_tree(self, parent, columns):
        frame = tk.Frame(parent, bg="#f5f5f5")
        frame.pack(fill="both", expand=True, padx=8, pady=4)

        vsb = ttk.Scrollbar(frame, orient="vertical")
        hsb = ttk.Scrollbar(frame, orient="horizontal")
        tree = ttk.Treeview(frame, columns=columns, show="headings",
                            yscrollcommand=vsb.set, xscrollcommand=hsb.set,
                            selectmode="extended")
        vsb.configure(command=tree.yview)
        hsb.configure(command=tree.xview)

        for col in columns:
            tree.heading(col, text=col.replace("_", " ").title(),
                         command=lambda c=col, t=tree: self._sort_tree(t, c))
            tree.column(col, width=160, anchor="w")

        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        return tree

    def _sort_tree(self, tree, col):
        # Track sort state per (tree id, column): True = descending
        state_key = (id(tree), col)
        if not hasattr(self, "_sort_state"):
            self._sort_state = {}
        descending = self._sort_state.get(state_key, False)

        # Fast path: if we have the backing DataFrame for this tree, sort it
        # at the pandas level and repopulate (much faster than moving rows)
        backing_key = f"_backing_{id(tree)}"
        backing_df  = getattr(self, backing_key, None)

        if backing_df is not None and col in backing_df.columns:
            # Sort numerically if possible, else lexicographically
            series = pd.to_numeric(backing_df[col], errors="coerce")
            if series.notna().any():
                backing_df = backing_df.copy()
                backing_df["__sort__"] = series
                backing_df = backing_df.sort_values(
                    "__sort__", ascending=not descending,
                    na_position="last").drop(columns="__sort__")
            else:
                backing_df = backing_df.sort_values(
                    col, ascending=not descending, key=lambda s: s.str.lower())
            setattr(self, backing_key, backing_df)
            self._fast_populate_tree(tree, backing_df, _store_backing=False)
        else:
            # Fallback: read values from the tree and move rows (original method)
            items = [(tree.set(k, col), k) for k in tree.get_children("")]
            try:
                items.sort(
                    key=lambda x: float(x[0]) if x[0] not in ("", "-") else float("-inf"),
                    reverse=descending)
            except ValueError:
                items.sort(key=lambda x: x[0].lower(), reverse=descending)
            # Batch the moves: detach widget during update
            tree.config(displaycolumns=tree["columns"])
            for idx, (_, k) in enumerate(items):
                tree.move(k, "", idx)

        # Flip direction and update heading arrows
        self._sort_state[state_key] = not descending
        for c in tree["columns"]:
            heading = c.replace("_", " ").title()
            arrow   = (" ▲" if descending else " ▼") if c == col else ""
            tree.heading(c, text=heading + arrow,
                         command=lambda c=c, t=tree: self._sort_tree(t, c))

    def _fast_populate_tree(self, tree, df: pd.DataFrame,
                             _store_backing: bool = True):
        """Batch-insert all rows from df into tree. ~10× faster than iterrows."""
        # Store the backing DataFrame so _sort_tree can sort at pandas level
        if _store_backing:
            setattr(self, f"_backing_{id(tree)}", df.copy())

        tree.delete(*tree.get_children())
        cols    = tree["columns"]
        insert  = tree.insert
        # Pre-build list of value tuples for maximum speed
        col_indices = [df.columns.get_loc(c) if c in df.columns else -1
                       for c in cols]
        arr = df.values  # numpy array — fast row access
        for row in arr:
            vals = tuple(
                row[i] if i >= 0 else ""
                for i in col_indices
            )
            insert("", "end", values=vals)

    def _set_status(self, msg: str):
        self.status_var.set(msg)
        # Messages about fetching a specific filer are shown in blue
        msg_lower = msg.lower()
        if any(kw in msg_lower for kw in ("fetching portfolio", "fetching filing",
                                           "fetching infotable", "found information",
                                           "fetching filing envelope",
                                           "fetching filing index",
                                           "] fetching ")):  # worker "[1/N] Fetching Name…"
            self._log_blue(msg)
        else:
            self._log(msg)
        self.update_idletasks()

    def _update_queue_tab_title(self):
        idx = self.nb.index(self.tab_queue)
        self.nb.tab(idx, text=f"  🗂 Queue ({len(self.selected_rows)})  ")

    def _schedule_search(self):
        if self._search_after_id is not None:
            self.after_cancel(self._search_after_id)
        self._search_after_id = self.after(SEARCH_DEBOUNCE_MS, self._apply_search)

    def _apply_search(self):
        self._search_after_id = None
        if self.index_df.empty:
            return
        query = self.search_var.get().strip().lower()
        filtered = (self.index_df[self.index_df["company_name"].str.lower()
                    .str.contains(query, na=False, regex=False)]
                    if query else self.index_df)
        self._fast_populate_tree(self.search_tree, filtered)

    # ── Actions ────────────────────────────────────────────────────────────
    @staticmethod
    def _quarter_to_dates(qtr_str):
        """Convert e.g. '2024 Q1' to (start_date_str, end_date_str)."""
        try:
            year, q = qtr_str.split()
            year = int(year)
            q    = int(q[1])
        except (ValueError, IndexError):
            raise ValueError(f"Invalid quarter: {qtr_str!r}")
        q_start_month = (q - 1) * 3 + 1
        q_end_month   = q * 3
        # Last day of end month
        import calendar
        last_day = calendar.monthrange(year, q_end_month)[1]
        start = date(year, q_start_month, 1).strftime("%Y-%m-%d")
        end   = date(year, q_end_month, last_day).strftime("%Y-%m-%d")
        return start, end

    def _fetch_index(self):
        if getattr(self, "_loading_state", False):
            return   # suppress auto-fetch during state load
        start_qtr = self.start_qtr_var.get().strip()
        end_qtr   = self.end_qtr_var.get().strip()
        try:
            start, _  = self._quarter_to_dates(start_qtr)
            _, end    = self._quarter_to_dates(end_qtr)
        except ValueError as e:
            messagebox.showerror("Quarter Error", str(e))
            return

        if start > end:
            messagebox.showerror("Range Error",
                                 "Start quarter must be before or equal to end quarter.")
            return

        self._set_status(
            f"Fetching index {start_qtr} – {end_qtr}… this may take a minute.")

        def worker():
            df = fetch_13f_index(start, end, progress_callback=self._set_status)
            self.index_df = df
            self.after(0, self._on_index_loaded)

        threading.Thread(target=worker, daemon=True).start()

    def _on_index_loaded(self):
        self._set_status(f"Loaded {len(self.index_df)} 13F filings.")
        self._fast_populate_tree(self.browse_tree, self.index_df)
        self._apply_search()

    def _add_from_tree(self, tree):
        selected = tree.selection()
        if not selected:
            messagebox.showinfo("Nothing selected",
                                "Please select one or more rows first.")
            return
        cols = tree["columns"]
        added = 0
        for item in selected:
            rd    = dict(zip(cols, tree.item(item, "values")))
            cik   = rd.get("cik", "")
            filed = rd.get("date_filed", "")
            if not any(r["cik"] == cik and r["date_filed"] == filed
                       for r in self._queue()):
                match = self.index_df[
                    (self.index_df["cik"] == cik) &
                    (self.index_df["date_filed"] == filed)]
                if not match.empty:
                    self.selected_rows.append(match.index[0])
                    added += 1

        self._refresh_queue_tree()
        self._update_queue_tab_title()
        self._set_status(f"Added {added} filing(s) to queue. "
                         f"Total: {len(self.selected_rows)}")

    def _remove_from_queue(self):
        selected = self.queue_tree.selection()
        if not selected:
            return
        cols = self.queue_tree["columns"]
        to_remove = set()
        for item in selected:
            rd    = dict(zip(cols, self.queue_tree.item(item, "values")))
            cik   = rd.get("cik", "")
            filed = rd.get("date_filed", "")
            for idx in self.selected_rows:
                if (idx in self.index_df.index
                        and self.index_df.loc[idx, "cik"] == cik
                        and self.index_df.loc[idx, "date_filed"] == filed):
                    to_remove.add(idx)
        self.selected_rows = [i for i in self.selected_rows if i not in to_remove]
        self._refresh_queue_tree()
        self._update_queue_tab_title()
        self._set_status(f"Removed {len(to_remove)} filing(s). "
                         f"Queue: {len(self.selected_rows)}")

    def _queue(self):
        return [self.index_df.loc[i] for i in self.selected_rows
                if i in self.index_df.index]

    def _refresh_queue_tree(self):
        rows = self._queue()
        if not rows:
            self.queue_tree.delete(*self.queue_tree.get_children())
            self._update_queue_filter_dropdowns()
            return
        df = pd.DataFrame([r.to_dict() for r in rows])
        cols = ("company_name", "form_type", "date_filed", "filing_quarter", "cik")
        self._fast_populate_tree(self.queue_tree,
                                 df[[c for c in cols if c in df.columns]])
        self._update_queue_filter_dropdowns()
        self._apply_queue_filter()

    def _clear_queue(self):
        self.selected_rows.clear()
        self._refresh_queue_tree()
        self._update_queue_tab_title()
        self._set_status("Queue cleared.")

    def _fetch_portfolios(self):
        if not self.selected_rows:
            messagebox.showinfo("Empty queue", "Add companies to the queue first.")
            return
        self._set_status("Fetching portfolios…")

        def worker():
            frames = []
            rows = self._queue()
            for i, row in enumerate(rows):
                self.after(0, lambda i=i, r=row: self._set_status(
                    f"[{i+1}/{len(rows)}] Fetching {r['company_name']}…"))
                df, env_dates = fetch_portfolio(
                    row,
                    progress_callback=lambda m: self.after(
                        0, lambda m=m: self._set_status(m)))
                if not df.empty:
                    frames.append(df)
            result = (pd.concat(frames, axis=0, ignore_index=True, sort=False)
                      if frames else pd.DataFrame())
            self.portfolio_df = result
            self.after(0, self._on_portfolios_loaded)

        threading.Thread(target=worker, daemon=True).start()

    def _on_portfolios_loaded(self):
        df = self.portfolio_df
        if df.empty:
            self._set_status("No portfolio data retrieved.")
            return

        # Compute weight% and store back into portfolio_df
        df["value_x1000"] = pd.to_numeric(df["value_x1000"], errors="coerce").fillna(0)
        if "filing_quarter" in df.columns and "filer_name" in df.columns:
            totals = df.groupby(["filer_name", "filing_quarter"])["value_x1000"].transform("sum")
            df["weight_%"] = (df["value_x1000"] / totals.replace(0, float("nan")) * 100).round(2)
        else:
            df["weight_%"] = float("nan")

        self.port_count_label.configure(
            text=f"{len(df)} holdings across {df['filer_name'].nunique()} filers")
        self._apply_portfolio_filter()
        self._refresh_overlap_tab()
        self._refresh_stats_tab()
        self._set_status(f"Done. {len(df)} holdings loaded.")

    def _schedule_port_filter(self):
        if self._port_filter_after is not None:
            self.after_cancel(self._port_filter_after)
        self._port_filter_after = self.after(300, self._apply_portfolio_filter)

    def _clear_portfolio_filters(self):
        for var in (self.pf_filer_var, self.pf_qtr_var, self.pf_issuer_var,
                    self.pf_wt_min_var, self.pf_wt_max_var,
                    self.pf_val_min_var, self.pf_val_max_var):
            var.set("")
        self._apply_portfolio_filter()

    def _apply_portfolio_filter(self):
        self._port_filter_after = None
        df = self.portfolio_df
        if df.empty:
            return

        filer  = self.pf_filer_var.get().strip().lower()
        qtr    = self.pf_qtr_var.get().strip().lower()
        issuer = self.pf_issuer_var.get().strip().lower()

        def to_float(var):
            try:
                return float(var.get().strip())
            except ValueError:
                return None

        wt_min  = to_float(self.pf_wt_min_var)
        wt_max  = to_float(self.pf_wt_max_var)
        val_min = to_float(self.pf_val_min_var)
        val_max = to_float(self.pf_val_max_var)

        mask = pd.Series(True, index=df.index)
        if filer:
            mask &= df["filer_name"].str.lower().str.contains(filer, na=False, regex=False)
        if qtr and "filing_quarter" in df.columns:
            mask &= df["filing_quarter"].str.lower().str.contains(qtr, na=False, regex=False)
        if issuer and "issuer_name" in df.columns:
            mask &= df["issuer_name"].str.lower().str.contains(issuer, na=False, regex=False)
        if "weight_%" in df.columns:
            w = pd.to_numeric(df["weight_%"], errors="coerce")
            if wt_min is not None:
                mask &= w >= wt_min
            if wt_max is not None:
                mask &= w <= wt_max
        v = pd.to_numeric(df["value_x1000"], errors="coerce")
        if val_min is not None:
            mask &= v >= val_min
        if val_max is not None:
            mask &= v <= val_max

        filtered = df[mask].copy()

        # Sort by filing_quarter, filer_name, issuer_name (then form_type if present)
        sort_cols = [c for c in ("filing_quarter", "filer_name", "issuer_name", "form_type")
                     if c in filtered.columns]
        if sort_cols:
            filtered = filtered.sort_values(sort_cols).reset_index(drop=True)

        display_cols = [c for c in ("filing_quarter", "filer_name", "form_type",
                                    "accepted_dt", "issuer_name", "cusip", "ticker",
                                    "weight_%", "value_x1000", "shares",
                                    "sic", "sic_description")
                        if c in filtered.columns]
        self._fast_populate_tree(self.port_tree, filtered[display_cols])
        n_shown = len(filtered)
        n_total = len(df)
        self.port_filter_label.configure(
            text=f"{n_shown} / {n_total} rows" if n_shown < n_total else "")

    def _build_overlap_tab(self):
        f = self.tab_overlap
        self._overlap_tree  = None
        self._overlap_df    = pd.DataFrame()

        ctrl = tk.Frame(f, bg="#f5f5f5")
        ctrl.pack(fill="x", padx=8, pady=6)

        tk.Label(ctrl, text="Min managers:", bg="#f5f5f5").pack(side="left")
        self.overlap_min_var = tk.IntVar(value=1)
        tk.Spinbox(ctrl, from_=1, to=99, width=4,
                   textvariable=self.overlap_min_var,
                   command=self._refresh_overlap_tab).pack(side="left", padx=4)

        tk.Label(ctrl, text="  Sort by:", bg="#f5f5f5").pack(side="left", padx=(12, 0))
        self.overlap_sort_var = tk.StringVar(value="total_managers")
        tk.Radiobutton(ctrl, text="Most held", variable=self.overlap_sort_var,
                       value="total_managers", bg="#f5f5f5",
                       command=self._refresh_overlap_tab).pack(side="left", padx=4)
        tk.Radiobutton(ctrl, text="Issuer name", variable=self.overlap_sort_var,
                       value="issuer_name", bg="#f5f5f5",
                       command=self._refresh_overlap_tab).pack(side="left", padx=4)

        tk.Button(ctrl, text="Export CSV", command=self._export_overlap_csv,
                  bg="#27ae60", fg="white", padx=8).pack(side="right")

        self.overlap_info = tk.Label(ctrl, text="", bg="#f5f5f5", fg="#555")
        self.overlap_info.pack(side="right", padx=8)

        self._overlap_tree_frame = tk.Frame(f, bg="#f5f5f5")
        self._overlap_tree_frame.pack(fill="both", expand=True, padx=8, pady=4)

    def _refresh_overlap_tab(self):
        df = self.portfolio_df
        if df.empty:
            return
        for col in ("cusip", "filer_name", "filing_quarter"):
            if col not in df.columns:
                return

        min_mgrs = max(1, self.overlap_min_var.get())

        # Build CUSIP → consolidated issuer name (semicolon-separated if multiple)
        if "issuer_name" in df.columns:
            cusip_names = (
                df[df["cusip"].astype(str).str.strip().ne("")]
                .groupby("cusip")["issuer_name"]
                .apply(lambda s: "; ".join(sorted(s.dropna().unique())))
                .reset_index()
                .rename(columns={"issuer_name": "issuer_names"})
            )
        else:
            cusip_names = pd.DataFrame(columns=["cusip", "issuer_names"])

        # Count distinct managers per (CUSIP, quarter)
        pivot = (
            df[df["cusip"].astype(str).str.strip().ne("")]
            .groupby(["cusip", "filing_quarter"])["filer_name"]
            .nunique()
            .reset_index()
            .rename(columns={"filer_name": "n_managers"})
        )

        pivot_wide = pivot.pivot_table(
            index="cusip",
            columns="filing_quarter",
            values="n_managers",
            aggfunc="sum",
            fill_value=0,
        ).reset_index()

        quarters = [c for c in pivot_wide.columns if c != "cusip"]
        pivot_wide["total_managers"] = pivot_wide[quarters].max(axis=1)
        pivot_wide = pivot_wide[pivot_wide["total_managers"] >= min_mgrs].copy()

        # Attach issuer names
        pivot_wide = pivot_wide.merge(cusip_names, on="cusip", how="left")
        pivot_wide["issuer_names"] = pivot_wide["issuer_names"].fillna("")

        # Sort
        sort_col = self.overlap_sort_var.get()
        if sort_col == "issuer_name":
            pivot_wide = pivot_wide.sort_values("issuer_names", ascending=True)
        else:
            pivot_wide = pivot_wide.sort_values("total_managers", ascending=False)
        pivot_wide = pivot_wide.reset_index(drop=True)

        self._overlap_df = pivot_wide

        # Rebuild the Treeview (columns change with quarters)
        for w in self._overlap_tree_frame.winfo_children():
            w.destroy()
        self._overlap_tree = None

        cols = ["issuer_names", "cusip"] + quarters + ["total_managers"]

        frame = tk.Frame(self._overlap_tree_frame, bg="#f5f5f5")
        frame.pack(fill="both", expand=True)

        vsb = ttk.Scrollbar(frame, orient="vertical")
        hsb = ttk.Scrollbar(frame, orient="horizontal")
        tree = ttk.Treeview(frame, columns=cols, show="headings",
                            yscrollcommand=vsb.set, xscrollcommand=hsb.set,
                            selectmode="extended")
        vsb.configure(command=tree.yview)
        hsb.configure(command=tree.xview)

        for col in cols:
            if col == "issuer_names":
                heading, width, anchor = "Issuer Name(s)", 320, "w"
            elif col == "cusip":
                heading, width, anchor = "CUSIP", 100, "w"
            elif col == "total_managers":
                heading, width, anchor = "Peak # Mgrs", 90, "center"
            else:
                heading, width, anchor = col, 90, "center"
            tree.heading(col, text=heading,
                         command=lambda c=col, t=tree: self._sort_overlap(t, c))
            tree.column(col, width=width, anchor=anchor, minwidth=55)

        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self._overlap_tree = tree
        tree.bind("<Double-1>", self._on_overlap_row_click)

        for _, row in pivot_wide.iterrows():
            vals = []
            for i, c in enumerate(cols):
                v = row.get(c, "")
                # Show "-" for zero counts in quarter columns
                if c not in ("issuer_names", "cusip", "total_managers") and v == 0:
                    vals.append("-")
                else:
                    vals.append(v)
            tree.insert("", "end", values=vals)

        n = len(pivot_wide)
        self.overlap_info.configure(
            text=f"{n} CUSIPs · {len(quarters)} quarter(s) · min {min_mgrs} mgr(s)")

    def _on_portfolio_row_right_click(self, event):
        """Right-click on a portfolio row: show context menu."""
        item = self.port_tree.identify_row(event.y)
        if not item:
            return
        # Select the right-clicked row if not already selected
        if item not in self.port_tree.selection():
            self.port_tree.selection_set(item)
        # Store the row data for use by menu commands
        cols = self.port_tree["columns"]
        vals = dict(zip(cols, self.port_tree.item(item, "values")))
        self._port_ctx_vals = vals
        self._port_ctx.tk_popup(event.x_root, event.y_root)

    def _port_ctx_plot_weight(self):
        """Context menu: plot weight-over-time for the right-clicked row."""
        vals = getattr(self, "_port_ctx_vals", {})
        cusip      = vals.get("cusip", "").strip()
        filer_name = vals.get("filer_name", "").strip()
        issuer     = vals.get("issuer_name", "").strip()
        if not cusip and not issuer:
            return
        self._plot_weight_over_time(cusip=cusip, filer_name=filer_name,
                                    label=issuer or cusip)

    def _port_ctx_show_detail(self):
        """Context menu: show holdings detail popup for the right-clicked row."""
        vals = getattr(self, "_port_ctx_vals", {})
        issuer = vals.get("issuer_name", "")
        cusip  = vals.get("cusip", "")
        if not issuer and not cusip:
            return
        df = self.portfolio_df
        if df.empty:
            return
        if cusip and "cusip" in df.columns:
            rows = df[df["cusip"] == cusip]
        elif issuer and "issuer_name" in df.columns:
            rows = df[df["issuer_name"] == issuer]
        else:
            rows = pd.DataFrame()
        self._show_detail_popup(f"Holdings: {issuer or cusip}", rows)

    def _on_portfolio_row_click(self, event):
        """Double-click on a portfolio row: show holdings detail popup."""
        item = self.port_tree.identify_row(event.y)
        if not item:
            return
        cols = self.port_tree["columns"]
        self._port_ctx_vals = dict(zip(cols, self.port_tree.item(item, "values")))
        self._port_ctx_show_detail()

    def _get_timeseries_chart_path(self) -> str:
        """Return the path to the persistent time-series chart HTML file."""
        import tempfile, os
        if not hasattr(self, "_ts_chart_path") or not self._ts_chart_path:
            tmp = tempfile.NamedTemporaryFile(
                suffix=".html", delete=False, prefix="13f_timeseries_",
                mode="w", encoding="utf-8")
            tmp.write("")
            tmp.close()
            self._ts_chart_path = tmp.name
        return self._ts_chart_path

    def _ts_data_path(self) -> str:
        """Return the companion JSON data file path (same name, .json extension)."""
        return self._get_timeseries_chart_path().replace(".html", "_data.json")

    def _write_timeseries_files(self):
        """Write both the HTML shell and the companion data JSON."""
        import json as _json
        if not hasattr(self, "_ts_version"):
            self._ts_version = 0
        self._ts_version += 1

        # Write companion data JSON (polled by JS)
        data = {
            "version": self._ts_version,
            "traces":  self._ts_traces,
        }
        with open(self._ts_data_path(), "w", encoding="utf-8") as f:
            _json.dump(data, f)

        # Write HTML shell (only needs to change when the file is first created
        # or after a clear — the JS polls the data file for live updates)
        chart_path = self._get_timeseries_chart_path()
        html = self._build_timeseries_html(self._ts_traces,
                                           version=self._ts_version)
        with open(chart_path, "w", encoding="utf-8") as f:
            f.write(html)

    def _clear_timeseries_chart(self):
        """Show a popup to selectively remove traces, with a Clear All button."""
        if not hasattr(self, "_ts_traces") or not self._ts_traces:
            messagebox.showinfo("No traces", "No traces to clear.")
            return

        win = tk.Toplevel(self)
        win.title("Manage Time-Series Traces")
        win.geometry("520x360")
        win.resizable(True, True)

        tk.Label(win, text="Select traces to REMOVE, then click Delete Selected:",
                 font=("Helvetica", 10), anchor="w").pack(fill="x", padx=10, pady=(10, 4))

        frame = tk.Frame(win)
        frame.pack(fill="both", expand=True, padx=10, pady=4)
        vsb = ttk.Scrollbar(frame, orient="vertical")
        lb  = tk.Listbox(frame, selectmode="multiple", yscrollcommand=vsb.set,
                         font=("Courier", 10), activestyle="dotbox")
        vsb.configure(command=lb.yview)
        vsb.pack(side="right", fill="y")
        lb.pack(fill="both", expand=True)

        for t in self._ts_traces:
            lb.insert("end", t.get("name", "?"))

        btn_row = tk.Frame(win)
        btn_row.pack(fill="x", padx=10, pady=8)

        def _refresh_chart():
            self._write_timeseries_files()
            # If no traces remain, reset opened flag so next addition re-opens browser
            if not self._ts_traces:
                self._ts_chart_opened = False
            self._set_status(f"Time-series chart: {len(self._ts_traces)} trace(s) remaining.")

        def delete_selected():
            selected = lb.curselection()
            if not selected:
                messagebox.showinfo("Nothing selected",
                                    "Select one or more traces to remove.")
                return
            names_to_remove = {lb.get(i) for i in selected}
            self._ts_traces = [t for t in self._ts_traces
                                if t.get("name") not in names_to_remove]
            _refresh_chart()
            win.destroy()

        def clear_all():
            self._ts_traces = []
            self._ts_chart_opened = False
            _refresh_chart()
            win.destroy()

        tk.Button(btn_row, text="Delete Selected",
                  command=delete_selected,
                  bg="#e74c3c", fg="white", padx=10).pack(side="left")
        tk.Button(btn_row, text="Clear All",
                  command=clear_all,
                  bg="#c0392b", fg="white", padx=10).pack(side="left", padx=6)
        tk.Button(btn_row, text="Cancel",
                  command=win.destroy,
                  bg="#bdc3c7", padx=10).pack(side="right")

    def _build_timeseries_html(self, traces: list, version: int = 0) -> str:
        import json as _json
        traces_json = _json.dumps(traces)
        n = len(traces)
        # Embed the data JSON URL as a sibling file that JS polls.
        # The HTML never reloads — JS fetches the data file and re-renders.
        data_url = self._ts_data_path().replace("\\", "/")
        return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Position Over Time</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: Arial, sans-serif; margin: 0; padding: 10px; background:#f0f2f5; }}
  h2   {{ margin: 8px 0 4px; color: #2c3e50; font-size: 16px; }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
               margin-bottom: 8px; background:#fff; padding: 8px 12px;
               border-radius:6px; box-shadow:0 1px 4px rgba(0,0,0,.1); }}
  .radio-row {{ display: flex; gap: 14px; }}
  .radio-row label {{ font-size: 13px; color: #333; }}
  #info {{ font-size: 12px; color: #888; margin-left: auto; }}
  #new-badge {{ display:none; background:#e74c3c; color:#fff; border-radius:4px;
                padding:2px 8px; font-size:11px; margin-left:8px; }}
  #chart {{ background:#fff; border-radius:6px; box-shadow:0 1px 4px rgba(0,0,0,.12); }}
</style>
</head>
<body>
<h2>Position Over Time</h2>
<div class="controls">
  <b style="font-size:12px;color:#555;">Y-axis:</b>
  <div class="radio-row">
    <label><input type="radio" name="yaxis" value="weight" checked onchange="redraw()">
      Portfolio Weight (%)</label>
    <label><input type="radio" name="yaxis" value="value" onchange="redraw()">
      Market Value ($000s)</label>
  </div>
  <span id="info">{n} trace(s).</span>
  <span id="new-badge">● New data available — updating…</span>
</div>
<div id="chart"></div>
<script>
// ── Data is stored in a companion JSON file; poll it for changes ───────────
const DATA_FILE = "file:///{data_url}";
let _currentVersion = {version};
let _allTraces      = {traces_json};
let _init           = false;

function getYMode() {{
  return document.querySelector('input[name="yaxis"]:checked').value;
}}

function buildPlotlyTraces(traces, isWeight) {{
  // Sort x values chronologically before plotting
  return traces.map(t => {{
    const pairs = t.x.map((q, i) => [q, isWeight ? (t.y_weight||[])[i] : (t.y_value||[])[i]])
                    .filter(([q,v]) => q !== undefined && v !== undefined)
                    .sort((a, b) => {{
                      const pa = a[0].split(" "), pb = b[0].split(" ");
                      const ya = parseInt(pa[0])||0, yb = parseInt(pb[0])||0;
                      const qa = parseInt((pa[1]||"Q0").slice(1))||0;
                      const qb = parseInt((pb[1]||"Q0").slice(1))||0;
                      return ya !== yb ? ya - yb : qa - qb;
                    }});
    return {{
      x:    pairs.map(p => p[0]),
      y:    pairs.map(p => p[1] ?? null),
      name: t.name,
      mode: "lines+markers",
      type: "scatter",
      hovertemplate: "<b>" + t.name + "</b><br>Quarter: %{{x}}<br>"
        + (isWeight ? "Weight: %{{y:.2f}}%" : "Value: $%{{y:,.0f}}k")
        + "<extra></extra>",
    }};
  }});
}}

function redraw(newTraces) {{
  if (newTraces !== undefined) _allTraces = newTraces;
  const isWeight = getYMode() === "weight";
  const traces   = buildPlotlyTraces(_allTraces, isWeight);
  const layout   = {{
    xaxis: {{ title: "Filing Quarter", tickangle: -30, automargin: true,
              type: "category" }},
    yaxis: {{
      title: isWeight ? "Portfolio Weight (%)" : "Market Value ($000s)",
      ticksuffix: isWeight ? "%" : "",
      tickprefix: isWeight ? "" : "$",
    }},
    legend: {{ orientation: "h", y: -0.3 }},
    height: 520,
    margin: {{ t: 30, b: 120, l: 70, r: 20 }},
    plot_bgcolor: "#f9f9f9",
    paper_bgcolor: "#fff",
    hovermode: "x unified",
  }};
  if (!_init) {{
    Plotly.newPlot("chart", traces, layout, {{responsive: true}});
    _init = true;
  }} else {{
    Plotly.react("chart", traces, layout);
  }}
  document.getElementById("info").textContent =
    _allTraces.length + " trace(s). Click rows in the Portfolio tab to add.";
  document.getElementById("new-badge").style.display = "none";
}}

// ── Poll the companion JSON file every 2 seconds for new data ─────────────
async function poll() {{
  try {{
    const r = await fetch(DATA_FILE + "?v=" + Date.now());
    if (!r.ok) return;
    const data = await r.json();
    if (data.version !== _currentVersion) {{
      _currentVersion = data.version;
      document.getElementById("new-badge").style.display = "inline";
      redraw(data.traces);
    }}
  }} catch(e) {{
    // File not yet written or CORS — ignore
  }}
}}

redraw();
setInterval(poll, 2000);
</script>
</body>
</html>"""



    def _plot_weight_over_time(self, cusip: str = "", filer_name: str = "",
                                label: str = ""):
        """
        Add a weight-over-time trace for the given (cusip, filer_name) to a
        persistent Plotly line chart.  Traces are maintained in self._ts_traces
        on the Python side so that "Clear All" always starts from a clean state.
        """
        import json as _json, webbrowser, os

        df = self.portfolio_df
        if df.empty or "weight_%" not in df.columns:
            messagebox.showinfo("No data", "Fetch portfolios first.")
            return

        # Filter to this CUSIP + filer
        if cusip and "cusip" in df.columns:
            sub = df[df["cusip"] == cusip]
        elif label and "issuer_name" in df.columns:
            sub = df[df["issuer_name"].str.contains(label, case=False, na=False)]
        else:
            return

        if filer_name:
            sub = sub[sub["filer_name"] == filer_name]

        if sub.empty:
            messagebox.showinfo("No data",
                                f"No holdings found for \'{label}\' / \'{filer_name}\'.")
            return

        # Aggregate by filing_quarter, taking both weight% and value_x1000
        agg_cols = {"weight_%": "max"}
        if "value_x1000" in sub.columns:
            agg_cols["value_x1000"] = "sum"
        ts = (sub.groupby("filing_quarter")
                 .agg(agg_cols)
                 .reset_index())

        # Sort quarters chronologically: "2024 Q3" → (2024, 3)
        def _qtr_sort_key(q):
            try:
                parts = q.split()
                return (int(parts[0]), int(parts[1][1:]))
            except Exception:
                return (0, 0)
        ts = ts.sort_values("filing_quarter",
                             key=lambda s: s.map(_qtr_sort_key))

        trace_label = f"{filer_name} — {label}" if filer_name else label
        new_trace   = {
            "x":         ts["filing_quarter"].tolist(),
            "y_weight":  ts["weight_%"].round(2).tolist(),
            "y_value":   ts["value_x1000"].astype(int).tolist()
                         if "value_x1000" in ts.columns else [],
            "name":      trace_label,
            "mode":      "lines+markers",
            "type":      "scatter",
        }

        # Maintain trace list on the Python side (not read back from HTML)
        if not hasattr(self, "_ts_traces"):
            self._ts_traces = []
        # Replace existing trace with same name, or append
        self._ts_traces = [t for t in self._ts_traces if t.get("name") != trace_label]
        self._ts_traces.append(new_trace)
        existing_traces = self._ts_traces

        # Write both files; open browser only on first call
        self._write_timeseries_files()
        chart_path = self._get_timeseries_chart_path()
        url = f"file:///{chart_path.replace(chr(92), '/')}"
        if not getattr(self, "_ts_chart_opened", False):
            webbrowser.open(url)
            self._ts_chart_opened = True
        self._set_status(
            f"Time-series chart: {len(self._ts_traces)} trace(s) — '{trace_label}'")


    def _on_overlap_row_click(self, event):
        """Double-click on an overlap row: show all managers holding that CUSIP."""
        item = self._overlap_tree.identify_row(event.y)
        if not item:
            return
        cols = self._overlap_tree["columns"]
        vals = dict(zip(cols, self._overlap_tree.item(item, "values")))
        cusip        = vals.get("cusip", "")
        issuer_names = vals.get("issuer_names", "")
        if not cusip:
            return

        df = self.portfolio_df
        if df.empty or "cusip" not in df.columns:
            return

        rows  = df[df["cusip"] == cusip]
        title = f"Managers holding CUSIP {cusip}  ({issuer_names})"
        self._show_detail_popup(title, rows)

    def _show_detail_popup(self, title: str, rows: pd.DataFrame):
        """Show a resizable popup with a sortable table of the given rows."""
        if rows.empty:
            messagebox.showinfo("No data", "No matching rows found.")
            return

        # Compute portfolio weight (%) per (filer, quarter) using FULL portfolio as denominator
        rows = rows.copy()
        rows["value_x1000"] = pd.to_numeric(rows["value_x1000"], errors="coerce").fillna(0)
        full = self.portfolio_df.copy()
        full["value_x1000"] = pd.to_numeric(full["value_x1000"], errors="coerce").fillna(0)
        if "filing_quarter" in rows.columns and "filer_name" in rows.columns:
            port_totals = (full.groupby(["filer_name", "filing_quarter"])["value_x1000"]
                             .sum()
                             .rename("port_total")
                             .reset_index())
            rows = rows.merge(port_totals, on=["filer_name", "filing_quarter"], how="left")
            rows["weight_%"] = (rows["value_x1000"] / rows["port_total"].replace(0, float("nan")) * 100).round(2)
            rows = rows.drop(columns=["port_total"])
        else:
            rows["weight_%"] = float("nan")

        # Columns to display (only those present)
        want = ["filer_name", "filing_quarter", "accepted_dt",
                "issuer_name", "cusip", "ticker",
                "weight_%", "value_x1000", "shares", "share_type",
                "sic", "sic_description", "cik"]
        show_cols = [c for c in want if c in rows.columns]

        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("1000x420")
        win.resizable(True, True)

        # Title label
        tk.Label(win, text=title, font=("Helvetica", 11, "bold"),
                 anchor="w", padx=8, pady=4).pack(fill="x")

        # Treeview
        frame = tk.Frame(win)
        frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        vsb = ttk.Scrollbar(frame, orient="vertical")
        hsb = ttk.Scrollbar(frame, orient="horizontal")
        tree = ttk.Treeview(frame, columns=show_cols, show="headings",
                            yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.configure(command=tree.yview)
        hsb.configure(command=tree.xview)

        col_widths = {
            "filer_name":      220, "filing_quarter":   90, "accepted_dt":     145,
            "issuer_name":     200, "cusip":            90, "ticker":           70,
            "weight_%":         80, "value_x1000":     110, "shares":          110,
            "share_type":       70, "sic":              60, "sic_description": 180,
            "cik":              90,
        }
        for col in show_cols:
            heading = col.replace("_", " ").title()
            width   = col_widths.get(col, 120)
            tree.heading(col, text=heading,
                         command=lambda c=col, t=tree: self._sort_overlap(t, c))
            tree.column(col, width=width, anchor="w", minwidth=50)

        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

        def fmt(col, val):
            if col in ("value_x1000", "shares"):
                try:
                    return f"{int(float(val)):,}"
                except (ValueError, TypeError):
                    return val
            return val

        # Sort by filing_quarter then filer_name for readability
        display = rows[show_cols].sort_values(
            [c for c in ("filing_quarter", "filer_name") if c in show_cols]
        )
        for _, row in display.iterrows():
            tree.insert("", "end", values=[fmt(c, row.get(c, "")) for c in show_cols])

        # Row count label
        tk.Label(win, text=f"{len(display)} row(s)",
                 fg="#666", anchor="w", padx=8).pack(fill="x", side="bottom")

    def _sort_overlap(self, tree, col):
        items = [(tree.set(k, col), k) for k in tree.get_children("")]
        try:
            items.sort(key=lambda x: float(x[0]) if x[0] not in ("", "-") else -1,
                       reverse=True)
        except ValueError:
            items.sort()
        for idx, (_, k) in enumerate(items):
            tree.move(k, "", idx)

    def _export_overlap_csv(self):
        if self._overlap_df.empty:
            messagebox.showinfo("No data", "Fetch portfolios first.")
            return
        path = "13f_overlap.csv"
        self._overlap_df.to_csv(path, index=False)
        messagebox.showinfo("Exported", f"Saved to {path}")
        self._set_status(f"Overlap table exported to {path}")

    def _build_stats_tab(self):
        f = self.tab_stats
        self._stats_df = pd.DataFrame()

        # ── Filter / search bar ───────────────────────────────────────────────
        flt = tk.Frame(f, bg="#e8ecf0", bd=1, relief="groove")
        flt.pack(fill="x", padx=8, pady=(8, 2))

        def lbl(text):
            tk.Label(flt, text=text, bg="#e8ecf0", fg="#444",
                     font=("Helvetica", 9)).pack(side="left", padx=(8, 2))

        lbl("Filer:")
        self.st_filer_var = tk.StringVar()
        self.st_filer_var.trace_add("write", lambda *_: self._apply_stats_filter())
        tk.Entry(flt, textvariable=self.st_filer_var,
                 width=20).pack(side="left", padx=(0, 6))

        lbl("Quarter:")
        self.st_qtr_var = tk.StringVar()
        self.st_qtr_var.trace_add("write", lambda *_: self._apply_stats_filter())
        tk.Entry(flt, textvariable=self.st_qtr_var,
                 width=9).pack(side="left", padx=(0, 6))

        lbl("Form Type:")
        self.st_ftype_var = tk.StringVar()
        self.st_ftype_var.trace_add("write", lambda *_: self._apply_stats_filter())
        tk.Entry(flt, textvariable=self.st_ftype_var,
                 width=9).pack(side="left", padx=(0, 6))

        tk.Button(flt, text="Clear filters",
                  command=self._clear_stats_filters,
                  bg="#bdc3c7", padx=6).pack(side="right", padx=6)

        self.st_filter_label = tk.Label(flt, text="", bg="#e8ecf0",
                                        fg="#888", font=("Helvetica", 9))
        self.st_filter_label.pack(side="right", padx=4)

        # ── Treeview ─────────────────────────────────────────────────────────
        cols = (
            "filer_name", "filing_quarter", "form_type",
            "total_value_x1000",
            "top_issuer", "top_value_x1000", "top_weight_%",
            "p90_weight_%", "p90_value_x1000",
        )
        self.stats_tree = self._make_tree(f, cols)

    def _refresh_stats_tab(self):
        """Compute and display portfolio statistics grouped by filer/quarter/form_type."""
        df = self.portfolio_df
        if df.empty:
            return

        df = df.copy()
        df["value_x1000"] = pd.to_numeric(df["value_x1000"], errors="coerce").fillna(0)
        df["weight_%"]    = pd.to_numeric(df["weight_%"],    errors="coerce").fillna(0)

        group_cols = [c for c in ("filer_name", "filing_quarter", "form_type")
                      if c in df.columns]

        rows = []
        for keys, grp in df.groupby(group_cols, sort=True):
            if not isinstance(keys, tuple):
                keys = (keys,)
            row = dict(zip(group_cols, keys))

            # 1. Total portfolio value
            row["total_value_x1000"] = int(grp["value_x1000"].sum())

            # 2. Largest position
            if not grp.empty:
                top_idx = grp["value_x1000"].idxmax()
                top_row = grp.loc[top_idx]
                row["top_issuer"]      = str(top_row.get("issuer_name", ""))
                row["top_value_x1000"] = int(top_row.get("value_x1000", 0))
                row["top_weight_%"]    = round(float(top_row.get("weight_%", 0)), 2)
            else:
                row["top_issuer"]      = ""
                row["top_value_x1000"] = 0
                row["top_weight_%"]    = 0.0

            # 3. 90th percentile weight% and value_x1000
            row["p90_weight_%"]    = round(grp["weight_%"].quantile(0.9),    2)
            row["p90_value_x1000"] = int(grp["value_x1000"].quantile(0.9))

            rows.append(row)

        self._stats_df = pd.DataFrame(rows)
        self._apply_stats_filter()

    def _apply_stats_filter(self):
        if self._stats_df.empty:
            return
        df = self._stats_df

        filer = self.st_filer_var.get().strip().lower()
        qtr   = self.st_qtr_var.get().strip().lower()
        ftype = self.st_ftype_var.get().strip().lower()

        mask = pd.Series(True, index=df.index)
        if filer and "filer_name" in df.columns:
            mask &= df["filer_name"].str.lower().str.contains(filer, na=False, regex=False)
        if qtr and "filing_quarter" in df.columns:
            mask &= df["filing_quarter"].str.lower().str.contains(qtr, na=False, regex=False)
        if ftype and "form_type" in df.columns:
            mask &= df["form_type"].str.lower().str.contains(ftype, na=False, regex=False)

        filtered = df[mask].copy()
        cols = self.stats_tree["columns"]

        self.stats_tree.delete(*self.stats_tree.get_children())
        for _, row in filtered.iterrows():
            vals = []
            for c in cols:
                v = row.get(c, "")
                if c in ("total_value_x1000", "top_value_x1000", "p90_value_x1000"):
                    try:
                        v = f"{int(v):,}"
                    except (ValueError, TypeError):
                        pass
                vals.append(v)
            self.stats_tree.insert("", "end", values=vals)

        n_shown = len(filtered)
        n_total = len(df)
        self.st_filter_label.configure(
            text=f"{n_shown} / {n_total} rows" if n_shown < n_total else "")

    def _clear_stats_filters(self):
        for var in (self.st_filer_var, self.st_qtr_var, self.st_ftype_var):
            var.set("")
        self._apply_stats_filter()

    def _chart_holdings(self):
        if self.portfolio_df.empty:
            messagebox.showinfo("No data", "Fetch portfolios first.")
            return
        try:
            import plotly.graph_objects as go
            import plotly.io as pio
        except ImportError:
            messagebox.showerror("Missing library",
                                 "Plotly is required for charting.\n\n"
                                 "Install it with:  pip install plotly")
            return

        import tempfile, webbrowser, json

        df = self.portfolio_df.copy()

        # Ensure value_x1000 is numeric
        df["value_x1000"] = pd.to_numeric(df["value_x1000"], errors="coerce").fillna(0)
        df = df[df["value_x1000"] > 0].copy()
        if df.empty:
            messagebox.showinfo("No data", "No numeric position values found.")
            return

        # Ensure filing_quarter column exists
        if "filing_quarter" not in df.columns or df["filing_quarter"].eq("").all():
            df["filing_quarter"] = pd.to_datetime(
                df["date_filed"], errors="coerce").apply(
                lambda d: f"{d.year} Q{(d.month-1)//3+1}" if pd.notna(d) else "Unknown")

        # Compute weight per (manager, quarter)
        df["port_total"] = df.groupby(
            ["filer_name", "filing_quarter"])["value_x1000"].transform("sum")
        df["weight_pct"] = (df["value_x1000"] / df["port_total"] * 100).round(2)

        # Build CUSIP → full display name (concatenate unique issuer names)
        # and CUSIP → short label (truncated to 20 chars for x-axis)
        cusip_names = (
            df.groupby("cusip")["issuer_name"]
              .apply(lambda s: " / ".join(sorted(s.dropna().astype(str).unique())))
              .reset_index()
              .rename(columns={"issuer_name": "full_name"})
        )
        cusip_names["short_label"] = cusip_names["full_name"].apply(
            lambda s: s[:20] if len(s) > 20 else s)

        df = df.merge(cusip_names, on="cusip", how="left")
        df["full_name"]   = df["full_name"].fillna(df["cusip"])
        df["short_label"] = df["short_label"].fillna(df["cusip"])

        quarters   = sorted(df["filing_quarter"].dropna().unique())
        managers   = sorted(df["filer_name"].dropna().unique())
        form_types = sorted(df["form_type"].dropna().astype(str).unique()) \
                     if "form_type" in df.columns else []

        # Serialise all data to JSON for the browser-side filtering.
        # Key: aggregate is done by CUSIP; display uses short_label / full_name.
        df["value_x1000"] = pd.to_numeric(df["value_x1000"], errors="coerce").fillna(0)
        df["shares"]      = pd.to_numeric(df["shares"],      errors="coerce").fillna(0)
        if "form_type" not in df.columns:
            df["form_type"] = ""
        records = df[["filer_name", "filing_quarter", "form_type", "cusip",
                       "short_label", "full_name",
                       "weight_pct", "value_x1000", "shares"]].to_dict(orient="records")

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>13F Holdings Chart</title>
<script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: Arial, sans-serif; margin: 0; padding: 10px;
          background: #f0f2f5; }}
  h2   {{ margin: 8px 0 6px; color: #2c3e50; font-size: 17px; }}
  .controls {{
    display: flex; flex-wrap: wrap; gap: 14px; align-items: flex-end;
    background: #fff; padding: 10px 14px; border-radius: 6px;
    box-shadow: 0 1px 4px rgba(0,0,0,.12); margin-bottom: 10px;
  }}
  .ctrl-group {{ display: flex; flex-direction: column; gap: 3px; }}
  .ctrl-group label {{ font-weight: bold; color: #555; font-size: 12px; }}
  select, input[type=text], input[type=number] {{
    padding: 5px 8px; border-radius: 4px; border: 1px solid #ccc;
    font-size: 13px;
  }}
  select              {{ min-width: 170px; }}
  input[type=text]    {{ min-width: 200px; }}
  input[type=number]  {{ width: 60px; }}
  .radio-row {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
  .radio-row label {{ font-weight: normal; color: #333; font-size: 13px; }}
  #overlap-n-row {{ display: none; align-items: center; gap: 6px;
                    font-size: 13px; color: #333; margin-top: 4px; }}
  #chart {{ background: #fff; border-radius: 6px;
             box-shadow: 0 1px 4px rgba(0,0,0,.12); }}
  #status-bar {{ font-size: 12px; color: #666; margin: 4px 0 6px;
                 padding: 4px 8px; background:#fff; border-radius:4px; }}
</style>
</head>
<body>
<h2>13F Holdings — Position Weights by Manager &amp; Filing Quarter</h2>

<div class="controls">

  <div class="ctrl-group">
    <label for="qtr-sel">Filing Quarter</label>
    <select id="qtr-sel" onchange="redraw()">
      <option value="__ALL__">All Quarters</option>
      {''.join(f'<option value="{q}">{q}</option>' for q in quarters)}
    </select>
  </div>

  <div class="ctrl-group">
    <label for="mgr-sel">Manager</label>
    <select id="mgr-sel" onchange="redraw()">
      <option value="__ALL__">All Managers</option>
      {''.join(f'<option value="{m}">{m}</option>' for m in managers)}
    </select>
  </div>

  <div class="ctrl-group">
    <label for="ftype-sel">Form Type</label>
    <select id="ftype-sel" onchange="redraw()">
      <option value="__ALL__">All Types</option>
      {''.join(f'<option value="{t}">{t}</option>' for t in form_types)}
    </select>
  </div>

  <div class="ctrl-group">
    <label for="search-box">Search Company</label>
    <input type="text" id="search-box" placeholder="e.g. Apple, AAPL…"
           oninput="redraw()" />
  </div>

  <div class="ctrl-group">
    <label>Holdings Filter</label>
    <div class="radio-row">
      <label>
        <input type="radio" name="overlap" value="all"
               checked onchange="onOverlapChange()">
        All holdings
      </label>
      <label>
        <input type="radio" name="overlap" value="common"
               onchange="onOverlapChange()">
        Held by all managers
      </label>
      <label>
        <input type="radio" name="overlap" value="topn"
               onchange="onOverlapChange()">
        Most crowded (top&nbsp;N)
      </label>
    </div>
    <div id="overlap-n-row">
      Show top <input type="number" id="top-n" value="20" min="1" max="500"
                      oninput="redraw()"> most-held securities
    </div>
  </div>

  <div class="ctrl-group">
    <label for="yaxis-sel">Y-axis metric</label>
    <select id="yaxis-sel">
      <option value="weight">Portfolio Weight (%)</option>
      <option value="value">Market Value ($000s)</option>
      <option value="shares">Number of Shares</option>
    </select>
  </div>

  <div class="ctrl-group">
    <label>Sort X-axis by</label>
    <div class="radio-row">
      <label>
        <input type="radio" name="sort-mode" value="weight"
               checked onchange="redraw()"> Weight (desc)
      </label>
      <label>
        <input type="radio" name="sort-mode" value="crowding"
               onchange="redraw()"> Crowding (# managers)
      </label>
      <label>
        <input type="radio" name="sort-mode" value="alpha"
               onchange="redraw()"> Alphabetical
      </label>
    </div>
  </div>

</div>

<div id="status-bar">Ready.</div>
<div id="chart"></div>

<script>
const ALL_DATA = {json.dumps(records)};

// ── Pre-index at load time (runs once) ─────────────────────────────────────
// All aggregation keyed by CUSIP. Display uses short_label (x-axis) and full_name (tooltip).
const BY_GROUP    = {{}};  // "Mgr — Qtr" → {{ name, data: {{cusip → {{weight,value,shares}} }} }}
const SEC_MGRS    = {{}};  // cusip → Set of filer_names
const CUSIP_SHORT = {{}};  // cusip → short_label (≤20 chars)
const CUSIP_FULL  = {{}};  // cusip → full issuer name string
const HAYSTACK    = {{}};  // cusip → lowercase search string

ALL_DATA.forEach(d => {{
  const ftype = d.form_type || "";
  const key   = d.filer_name + " — " + d.filing_quarter + (ftype ? " (" + ftype + ")" : "");
  if (!BY_GROUP[key]) BY_GROUP[key] = {{
    name: key, mgr: d.filer_name, qtr: d.filing_quarter,
    ftype: ftype, data: {{}}
  }};
  BY_GROUP[key].data[d.cusip] = {{
    weight: d.weight_pct,
    value:  d.value_x1000,
    shares: d.shares,
  }};

  if (!SEC_MGRS[d.cusip]) SEC_MGRS[d.cusip] = new Set();
  SEC_MGRS[d.cusip].add(d.filer_name);

  CUSIP_SHORT[d.cusip] = d.short_label || d.cusip;
  CUSIP_FULL[d.cusip]  = d.full_name   || d.cusip;
  HAYSTACK[d.cusip]    = (d.cusip + " " + (d.full_name || "")).toLowerCase();
}});

const ALL_CUSIPS = Object.keys(SEC_MGRS);

function getRadio(name) {{
  const el = document.querySelector(`input[name="${{name}}"]:checked`);
  return el ? el.value : null;
}}

function onOverlapChange() {{
  document.getElementById("overlap-n-row").style.display =
    (getRadio("overlap") === "topn") ? "flex" : "none";
  scheduleRedraw();
}}

let _timer = null;
function scheduleRedraw() {{
  clearTimeout(_timer);
  _timer = setTimeout(redraw, 80);
}}

let _initialized = false;

function redraw() {{
  const selQtr      = document.getElementById("qtr-sel").value;
  const selMgr      = document.getElementById("mgr-sel").value;
  const selFtype    = document.getElementById("ftype-sel").value;
  const searchRaw   = document.getElementById("search-box").value.trim().toLowerCase();
  const sortMode    = getRadio("sort-mode");
  const overlapMode = getRadio("overlap");
  const topN        = parseInt(document.getElementById("top-n").value) || 20;
  const yMetric     = document.getElementById("yaxis-sel").value;

  const YAXIS_LABEL  = {{ weight: "Portfolio Weight (%)", value:  "Market Value ($000s)", shares: "Number of Shares" }}[yMetric];
  const YTICK_SUFFIX = {{ weight: "%",  value: "", shares: "" }}[yMetric];
  const YTICK_PREFIX = {{ weight: "",   value: "$", shares: "" }}[yMetric];

  // Step 1: visible groups (manager+quarter+form_type combos)
  const visGroups = Object.values(BY_GROUP).filter(g => {{
    if (selQtr   !== "__ALL__" && g.qtr   !== selQtr)   return false;
    if (selMgr   !== "__ALL__" && g.mgr   !== selMgr)   return false;
    if (selFtype !== "__ALL__" && g.ftype !== selFtype)  return false;
    return true;
  }});

  const visManagers = new Set(visGroups.map(g => g.name.split(" — ")[0]));
  const nMgrs = visManagers.size;

  // Step 2: text-search filter (searches cusip + full issuer name)
  const searchOk = searchRaw
    ? new Set(ALL_CUSIPS.filter(c => HAYSTACK[c]?.includes(searchRaw)))
    : null;

  // Step 3: aggregate per-CUSIP stats across visible groups
  const secWeight = {{}};  // cusip → total y-value
  const secMgrVis = {{}};  // cusip → Set of visible managers

  visGroups.forEach(g => {{
    const mgr = g.name.split(" — ")[0];
    for (const [cusip, vals] of Object.entries(g.data)) {{
      if (searchOk && !searchOk.has(cusip)) continue;
      const v = vals[yMetric] ?? 0;
      secWeight[cusip] = (secWeight[cusip] || 0) + v;
      if (!secMgrVis[cusip]) secMgrVis[cusip] = new Set();
      secMgrVis[cusip].add(mgr);
    }}
  }});

  // Step 4: overlap / crowding filter (keyed by CUSIP)
  let allowedCusips = Object.keys(secWeight);
  if (overlapMode === "common") {{
    allowedCusips = allowedCusips.filter(c => secMgrVis[c].size === nMgrs);
  }} else if (overlapMode === "topn") {{
    allowedCusips.sort((a, b) =>
      secMgrVis[b].size - secMgrVis[a].size || secWeight[b] - secWeight[a]);
    allowedCusips = allowedCusips.slice(0, topN);
  }}

  const allowed = new Set(allowedCusips);

  // Step 5: sort by chosen mode
  const allCusips = allowedCusips.slice();
  if (sortMode === "weight") {{
    allCusips.sort((a, b) => secWeight[b] - secWeight[a]);
  }} else if (sortMode === "crowding") {{
    allCusips.sort((a, b) =>
      secMgrVis[b].size - secMgrVis[a].size || secWeight[b] - secWeight[a]);
  }} else {{
    allCusips.sort((a, b) =>
      (CUSIP_SHORT[a] || a).localeCompare(CUSIP_SHORT[b] || b));
  }}

  // X-axis: short_label (≤20 chars). Tooltip: full_name.
  const xLabels   = allCusips.map(c => CUSIP_SHORT[c] || c);
  const xFullName = allCusips.map(c => CUSIP_FULL[c]  || c);

  // Step 6: build traces
  const cusipIdx = {{}};
  allCusips.forEach((c, i) => cusipIdx[c] = i);
  const n = allCusips.length;

  const traces = visGroups.map(g => {{
    const y          = new Array(n).fill(null);
    const customdata = new Array(n).fill(["", 0]);
    for (const [cusip, vals] of Object.entries(g.data)) {{
      if (!allowed.has(cusip)) continue;
      const i = cusipIdx[cusip];
      if (i === undefined) continue;
      y[i]          = vals[yMetric] ?? null;
      customdata[i] = [xFullName[i], secMgrVis[cusip]?.size || 0];
    }}
    const fmtY = v => {{
      if (v === null) return "";
      if (yMetric === "weight") return v.toFixed(1) + "%";
      if (yMetric === "value")  return "$" + v.toLocaleString();
      return v.toLocaleString();
    }};
    return {{
      type: "bar",
      name: g.name,
      x: xLabels,
      y,
      customdata,
      hovertemplate:
        "<b>%{{customdata[0]}}</b><br>" +
        YAXIS_LABEL + ": %{{y:,.2f}}<br>" +
        "Held by %{{customdata[1]}} manager(s)<br>" +
        g.name + "<extra></extra>",
      text: n <= 60 ? y.map(fmtY) : [],
      textposition: "outside",
      cliponaxis: false,
    }};
  }});

  const layout = {{
    barmode: "group",
    xaxis: {{ title: "Security", tickangle: -40, automargin: true }},
    yaxis: {{
      title: YAXIS_LABEL,
      ticksuffix: YTICK_SUFFIX,
      tickprefix: YTICK_PREFIX,
    }},
    legend: {{ orientation: "h", y: -0.35 }},
    height: 620,
    margin: {{ t: 30, b: 160, l: 60, r: 20 }},
    plot_bgcolor: "#f9f9f9",
    paper_bgcolor: "#fff",
  }};

  if (!_initialized) {{
    Plotly.newPlot("chart", traces, layout, {{responsive: true}});
    // Click on a bar → add that CUSIP’s weight-over-time to the time-series panel
    document.getElementById("chart").on("plotly_click", function(data) {{
      const pt = data.points[0];
      if (!pt) return;
      const shortLabel = pt.x;
      const traceName  = pt.data.name;  // "Mgr — Qtr (FormType)"
      // Find matching cusip from ALL_DATA
      const match = ALL_DATA.find(d => CUSIP_SHORT[d.cusip] === shortLabel
                                    && (d.filer_name + " — " + d.filing_quarter +
                                        (d.form_type ? " (" + d.form_type + ")" : "")) === traceName);
      if (!match) return;
      addTimeSeries(match.cusip, match.filer_name, CUSIP_FULL[match.cusip] || shortLabel);
    }});
    _initialized = true;
  }} else {{
    Plotly.react("chart", traces, layout);
  }}

  const nSecs = allCusips.length;
  let overlapDesc = "";
  if (overlapMode === "common") overlapDesc = ` (shared by all ${{nMgrs}} manager(s))`;
  if (overlapMode === "topn")   overlapDesc = ` (top ${{topN}} most crowded)`;
  document.getElementById("status-bar").textContent =
    `Showing ${{nSecs}} securit${{nSecs===1?"y":"ies"}} across ${{traces.length}} trace(s)${{overlapDesc}}.`;
}}

document.querySelectorAll("select").forEach(el => el.addEventListener("change", scheduleRedraw));
document.querySelectorAll('input[type="text"], input[type="number"]').forEach(
  el => el.addEventListener("input", scheduleRedraw));
document.querySelectorAll('input[name="sort-mode"]').forEach(
  el => el.addEventListener("change", scheduleRedraw));
document.getElementById("yaxis-sel").addEventListener("change", () => {{
  _initialized = false;
  scheduleRedraw();
}});

redraw();
</script>
</body>
</html>"""

        tmp = tempfile.NamedTemporaryFile(
            suffix=".html", delete=False, prefix="13f_chart_",
            mode="w", encoding="utf-8")
        tmp.write(html)
        tmp.close()
        webbrowser.open(f"file:///{tmp.name.replace(chr(92), '/')}")
        self._set_status(f"Chart opened in browser: {tmp.name}")

    def _export_csv(self):
        if self.portfolio_df.empty:
            messagebox.showinfo("No data", "Fetch portfolios first.")
            return
        path = "13f_portfolios.csv"
        self.portfolio_df.to_csv(path, index=False)
        messagebox.showinfo("Exported", f"Saved to {path}")
        self._set_status(f"Exported {len(self.portfolio_df)} rows to {path}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app = App13F()
    app.mainloop()
