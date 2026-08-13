"""
Parse Omega Timing diving results-book PDFs into tidy CSVs.

Writes three combined files under data/processed/:
  scores.csv, judges.csv, judge_scores.csv
(re-running a meet replaces that MeetId's rows).

PDF discovery: if pdf_url / pdf_urls is missing but event_page is set, links
are found from live Omega (Google Chrome) then a Wayback Machine snapshot
(`python -m src --discover-pdfs` / automatic on download).
Prefers "GET THE COMPLETE RESULTS BOOK HERE" when present; older meets with
no book fall back to every per-event Result List / Detailed Results PDF.

v1 focuses on individual events (1m / 3m / 10m). Synchro and team events
use different layouts and are skipped.

Major assistance from Cursor Grok 4
"""

from __future__ import annotations

import argparse
import atexit
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pdfplumber
import numpy as np
import pandas as pd
import requests
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "meets.yaml"
RAW_PDF_DIR = PROJECT_ROOT / "data" / "raw" / "pdfs"
RAW_HTML_DIR = PROJECT_ROOT / "data" / "raw" / "html"
PROCESSED_DIR = PROJECT_ROOT / "data" / "processed"

# Combined (all-meets) outputs — filter by Meet / MeetId as needed
SCORES_CSV = PROCESSED_DIR / "scores.csv"
JUDGES_CSV = PROCESSED_DIR / "judges.csv"
JUDGE_SCORES_CSV = PROCESSED_DIR / "judge_scores.csv"

JSCORE_COLS = [f"JScore{i}" for i in range(1, 8)]
JNAME_COLS = [f"JudgeName{i}" for i in range(1, 8)]

SCORE_COLS = [
    "Meet",
    "MeetId",
    "Event",
    "Round",
    "EventDate",
    "Diver",
    "Country",
    "OverallRank",
    "DiveNo",
    "DiveCode",
    "Difficulty",
    *JSCORE_COLS,
    "PenaltyFlag",
    "DivePoints",
    "TotalPoints",
]
JUDGE_COLS = [
    "Meet",
    "MeetId",
    "Event",
    "Round",
    "EventDate",
    "Panel",
    "RoundsCovered",
    "Function",
    "JudgeNo",
    "JudgeName",
]
JOINED_COLS = SCORE_COLS + JNAME_COLS + ["Panel"]

# Dive codes: 3–4 digits + optional letter, e.g. 105B, 5152B, 5337D
DIVE_CODE_RE = re.compile(r"\b(\d{3,4}[A-Z]?)\b")
NAT_RE = re.compile(r"\b([A-Z]{3})\b")
EVENT_RE = re.compile(r"^Event\s+\d+\s+(.+)$")
# Older Omega books put the title on the next line: "Event 206" / "Men's 3m …"
EVENT_NUM_ONLY_RE = re.compile(r"^Event\s+\d+\s*$", re.IGNORECASE)
# Olympic / Rio-style: event name embedded in a venue line, round on its own line
EVENT_NAME_RE = re.compile(
    r"((?:Men['’]?s|Women['’]?s|MEN|WOMEN)\s+"
    r"(?:\d+\s*m\s+)?(?:Springboard|Platform)\b.*)$",
    re.IGNORECASE,
)
ROUND_ONLY_RE = re.compile(
    r"^(Preliminary|Preliminaries|Semifinal(?:\s+[AB])?|Semi-Final(?:\s+[AB])?|"
    r"Final|Finals|Super Final)\s*$",
    re.IGNORECASE,
)
DATE_RE = re.compile(r"^(?:[A-Z]{3}\s+)?(\d{1,2}\s+[A-Z]{3}\s+\d{4})")
PANEL_HEADER_RE = re.compile(
    r"Panel\s+([A-Z])\s*\((?:rounds?|dives?)\s+(\d+)\s*[-–­]\s*(\d+)\)",
    re.IGNORECASE,
)
JUDGE_LINE_RE = re.compile(r"^Judge\s+(\d+)\s+(.+)$", re.IGNORECASE)
JUDGE_INLINE_RE = re.compile(
    r"Judge\s+(\d+)\s+(.+?)\s+([A-Z]{3})(?=\s+Judge\s|\s*$)",
    re.IGNORECASE,
)

# code DD J1..J7 DivePoints [DiveRank Total Overall [Behind [extra]]]
# Old 2000–03 lines often omit rank columns; scores may be "10." not "10.0".
_SCORE_TOK = r"(?:\d+\.\d|10\.?)"
DIVE_LINE_RE = re.compile(
    r"^(?P<code>\d{3,4}[A-Z]?)\s+"
    r"(?P<dd>\d+\.\d)\s+"
    r"(?P<scores>(?:" + _SCORE_TOK + r"\s+){7})"
    r"(?P<pen>\*)?(?P<dive_pts>\d+\.\d{2})"
    r"(?:\s+(?P<dive_rank>=?\d+))?"
    r"(?:\s+(?P<total>\d+\.\d{2}))?"
    r"(?:\s+(?P<overall>=?\d+))?"
    r"(?:\s+(?P<behind>\d+\.\d{2}))?"
    r"(?:\s+\d+\.\d{2})?"
    r"\s*$"
)

# Compound surnames / glued NATs are handled in parse_diver_header().
SURNAME_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9'\-]*$")


def parse_diver_header(line: str) -> Optional[dict]:
    """Parse a diver-start line into surname/given/nat/optional dive rest.

    Handles:
    - Compound surnames: LINAN CANELA, CAMACHO DEL HOYO, RODRIGUEZ LEDESMA
    - Missing given on first line: BENT-ASHMEIL GBR …
    - Glued country codes: AleksandraPOL, EnyaROU
    """
    raw = line.strip()
    rank_m = re.match(r"^(\d+)\.?\s+", raw)
    rank = rank_m.group(1) if rank_m else None
    body = raw[rank_m.end() :] if rank_m else raw

    m = re.match(
        r"^(?P<name>.+?)\s+(?P<nat>[A-Z]{3})\s+"
        r"(?:(?P<yob>\d{2})\s+)?"
        r"(?P<after>\d{3,4}[A-Z]?\s+.+|DNS.*)$",
        body,
    )
    if not m:
        m = re.match(
            r"^(?P<name>.+?)(?P<nat>[A-Z]{3})\s+"
            r"(?:(?P<yob>\d{2})\s+)?"
            r"(?P<after>\d{3,4}[A-Z]?\s+.+|DNS.*)$",
            body,
        )
    if not m:
        m = re.match(r"^(?P<name>.+?)\s+(?P<nat>[A-Z]{3})\s*$", body)
    if not m:
        m = re.match(r"^(?P<name>.+?)(?P<nat>[A-Z]{3})\s*$", body)
    if not m:
        # Surname (no given) + NAT + dive already covered; NAT-only after surname:
        m = re.match(
            r"^(?P<name>[A-Z][A-Z'\-]+(?:\s+[A-Z][A-Z'\-]+)*)\s+"
            r"(?P<nat>[A-Z]{3})\s+(?P<after>\d{3,4}[A-Z]?\s+.+|DNS.*)$",
            body,
        )
    if not m:
        return None

    name_part = m.group("name").strip()
    nat = m.group("nat")
    after = (m.groupdict().get("after") or "").strip()

    # Leading ALL-CAPS tokens = surname; remainder = given name(s).
    # 2004–2010 Omega books use Title Case "Lindberg Anna" — first token is surname.
    surname_tokens: list[str] = []
    given_tokens: list[str] = []
    for tok in name_part.split():
        if given_tokens:
            given_tokens.append(tok)
        elif re.match(r"^[A-Z][A-Z0-9'\-]*$", tok) and tok.upper() == tok:
            surname_tokens.append(tok)
        else:
            given_tokens.append(tok)

    if not surname_tokens and given_tokens:
        surname_tokens = [given_tokens.pop(0)]
    if not surname_tokens:
        return None

    return {
        "rank": rank,
        "surname": " ".join(surname_tokens),
        "given": " ".join(given_tokens),
        "nat": nat,
        "rest": after,
    }

SKIP_EVENT_KEYWORDS = (
    "synchronis",  # Synchronised / Synchronized
    "synchro",
    "team event",
)

HEADER_NOISE = {
    "Detailed Results",
    "Result List",
    "DETAILED GENERAL RANKING",
    "Panel of Judges",
    "Function Name",
    "NAT Dive Judge's Score DiveRound Total Overall Points",
    "RankName DD",
    "Code No. J1 J2 J3 J4 J5 J6 J7 Points Rank Points Rank Behind",
    "Note:",
    "Legend:",
    "Official Timekeeping by OMEGA",
}

# Lines that appear as page headers on every results-book page
PAGE_HEADER_RE = re.compile(
    r"^(?:European Aquatics|World Aquatics|Olympic|OMEGA|"
    r"Antalya|As of |REVISED|Adjusted timings|"
    r"\d{1,2}\s*[-–]\s*\d{1,2}\s+[A-Z]{3}\s+\d{4}|"
    r"Event\s+\d+\b|"
    r"\d{1,2}\s+[A-Z]{3}\s+\d{4}"
    r")",
    re.IGNORECASE,
)


def _is_noise_line(ln: str) -> bool:
    s = ln.strip()
    if not s:
        return True
    if s in HEADER_NOISE:
        return True
    if PAGE_HEADER_RE.match(s):
        return True
    if s.startswith("Official Timekeeping"):
        return True
    if s.startswith("DIV") and ("OMEGA" in s or "Report" in s or "-" in s[:10]):
        return True
    if "Report Created by OMEGA" in s:
        return True
    if s.startswith("For more detail"):
        return True
    if s.startswith("* 2.0 point"):
        return True
    if s.startswith("DNS Did Not"):
        return True
    if re.match(r"^Page\s+\d+", s):
        return True
    if s.startswith("Rank Name") or s.startswith("Code No"):
        return True
    if re.match(r"^NAT\b", s) and "Dive" in s:
        return True
    # Location lines like "Antalya (TUR)"
    if re.match(r"^[A-Za-z].*\([A-Z]{3}\)$", s) and len(s) < 40:
        return True
    return False


def _is_name_continuation(ln: str) -> bool:
    """True for wrapped given-name fragments like 'Linus' or 'Pablo'."""
    s = ln.strip()
    if not s or len(s) > 30:
        return False
    if not re.match(r"^[A-Za-z][A-Za-z\-'.]*$", s):
        return False
    # Reject obvious non-name tokens
    if s.lower() in {"note", "legend", "revised", "dns", "dd"}:
        return False
    return True


@dataclass
class MeetConfig:
    id: str
    name: str
    local_path: Optional[str] = None
    pdf_url: Optional[str] = None
    pdf_urls: Optional[list[str]] = None  # per-event Result Lists when no book
    location: Optional[str] = None
    dates: Optional[str] = None
    event_page: Optional[str] = None


@dataclass
class PdfPick:
    """Result of scanning an Omega event page for downloadable PDFs."""

    kind: str  # "book" (one complete results book) | "sessions" (per-event)
    urls: list[str]


@dataclass
class ParseState:
    meet_name: str = ""
    event: str = ""
    event_date: str = ""
    page_type: str = ""  # detailed | judges | other
    is_individual: bool = False


def load_meets(config_path: Path = DEFAULT_CONFIG) -> list[MeetConfig]:
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    meets = []
    for m in data.get("meets", []):
        meets.append(
            MeetConfig(
                id=m["id"],
                name=m["name"],
                local_path=m.get("local_path"),
                pdf_url=m.get("pdf_url") if isinstance(m.get("pdf_url"), str) else None,
                pdf_urls=list(m["pdf_urls"]) if m.get("pdf_urls") else None,
                location=m.get("location"),
                dates=m.get("dates"),
                event_page=m.get("event_page"),
            )
        )
    return meets


PDF_URL_RE = re.compile(
    r"https?://(?:www\.)?omegatiming\.com/File/[A-Fa-f0-9]+\.pdf",
    re.IGNORECASE,
)
FILE_PATH_RE = re.compile(r"/File/([A-Fa-f0-9]+\.pdf)", re.IGNORECASE)
ANCHOR_RE = re.compile(
    r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
    flags=re.IGNORECASE | re.DOTALL,
)
# Meet-level File codes pad with ~20 F's; per-event files have ~14.
MEET_LEVEL_BOOK_RE = re.compile(r"F{18,}(1[Ee]|03)\.pdf$", re.I)
MEET_LEVEL_00_RE = re.compile(r"F{18,}00\.pdf$", re.I)
COMPLETE_BOOK_TEXT_RE = re.compile(
    r"COMPLETE\s+RESULTS\s+(?:BOOK|LIST)|GET\s+THE\s+COMPLETE\s+RESULTS",
    re.I,
)
SESSION_RESULT_TEXT_RE = re.compile(
    r"^(?:Result List|Detailed Results)$",
    re.I,
)
HTTP = requests.Session()
HTTP.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }
)

# Live Omega hangs for curl/requests (HTTP/2 INTERNAL_ERROR or 0-byte read).
# Chrome can fetch the same URLs; first navigation is often ~30s.
OMEGA_ORIGIN = "https://www.omegatiming.com/"
OMEGA_NAV_TIMEOUT_MS = 90_000
OMEGA_PDF_TIMEOUT_MS = 120_000
OMEGA_PAUSE_S = 4.0


class OmegaBrowser:
    """Reuse one headless Chrome for Omega HTML + PDF fetches."""

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._page = None
        self._origin_ready = False

    def start(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required for live Omega downloads. "
                "Install with: pip install playwright"
            ) from exc
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(channel="chrome", headless=True)
        except Exception:
            self._browser = self._pw.chromium.launch(headless=True)
        self._page = self._browser.new_page()

    def close(self) -> None:
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._pw = self._browser = self._page = None
        self._origin_ready = False

    def _ensure_origin(self) -> None:
        if self._origin_ready:
            return
        print("  opening omegatiming.com in Chrome (often ~30s)…")
        self._page.goto(
            OMEGA_ORIGIN,
            timeout=OMEGA_NAV_TIMEOUT_MS,
            wait_until="domcontentloaded",
        )
        self._origin_ready = True

    def get_html(self, url: str) -> str:
        print(f"  loading event page in Chrome (up to {OMEGA_NAV_TIMEOUT_MS // 1000}s)…")
        self._page.goto(
            url,
            timeout=OMEGA_NAV_TIMEOUT_MS,
            wait_until="domcontentloaded",
        )
        self._page.wait_for_timeout(3000)
        self._origin_ready = True
        return self._page.content()

    def get_pdf(self, url: str) -> bytes:
        self._ensure_origin()
        time.sleep(OMEGA_PAUSE_S)
        print(f"  trying Omega (Chrome) for {url.rsplit('/', 1)[-1]}…")
        resp = self._page.request.get(url, timeout=OMEGA_PDF_TIMEOUT_MS)
        body = resp.body()
        if not body.startswith(b"%PDF"):
            raise RuntimeError(
                f"Not a PDF ({resp.headers.get('content-type')}, {len(body)} bytes)"
            )
        return body


_OMEGA_BROWSER: Optional[OmegaBrowser] = None


def get_omega_browser() -> OmegaBrowser:
    global _OMEGA_BROWSER
    if _OMEGA_BROWSER is None:
        browser = OmegaBrowser()
        browser.start()
        _OMEGA_BROWSER = browser
        atexit.register(close_omega_browser)
    return _OMEGA_BROWSER


def close_omega_browser() -> None:
    global _OMEGA_BROWSER
    if _OMEGA_BROWSER is not None:
        _OMEGA_BROWSER.close()
        _OMEGA_BROWSER = None


def _normalize_omega_href(href: str) -> Optional[str]:
    href = href.strip()
    href = re.sub(r"^https?://web\.archive\.org/web/\d+(?:id_)?/", "", href)
    if href.startswith("/"):
        href = "https://www.omegatiming.com" + href
    if not href.lower().endswith(".pdf"):
        return None
    return href


def _extract_pdf_urls(html: str) -> list[str]:
    found = set(PDF_URL_RE.findall(html))
    for name in FILE_PATH_RE.findall(html):
        found.add(f"https://www.omegatiming.com/File/{name}")
    return sorted(found)


def _iter_pdf_anchors(html: str) -> list[tuple[str, str]]:
    """Return (pdf_url, anchor_text) pairs from an Omega event page."""
    out: list[tuple[str, str]] = []
    for m in ANCHOR_RE.finditer(html):
        href = _normalize_omega_href(m.group(1))
        if not href:
            continue
        text = " ".join(re.sub(r"<[^>]+>", "", m.group(2)).split())
        out.append((href, text))
    return out


def _unique(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def pick_result_pdfs(html: str) -> PdfPick:
    """Choose PDFs from an Omega event page.

    Always prefer a complete results book when the page has one
    (``GET THE COMPLETE RESULTS BOOK HERE``). Older meets often have no book
    — only per-event Start List + Result List links. In that case return every
    Result List / Detailed Results PDF, never Start / Dive / Entry lists.

    File-suffix conventions are *not* stable across eras:
      modern book     …FFFF1E.pdf (or …03.pdf)
      modern entry    …00 / …01.pdf
      old Start List  …00.pdf
      old Result List …01.pdf
    so anchor text is the source of truth.
    """
    anchors = _iter_pdf_anchors(html)

    books = [url for url, text in anchors if COMPLETE_BOOK_TEXT_RE.search(text)]
    if books:
        return PdfPick("book", _unique(books)[:1])

    sessions = [
        url
        for url, text in anchors
        if SESSION_RESULT_TEXT_RE.match(text.strip())
    ]
    if sessions:
        return PdfPick("sessions", _unique(sessions))

    # Label-less HTML (rare): only accept a meet-level …1E / …03 book.
    # Do not grab …00.pdf — on old pages that is a Start List.
    pdfs = _extract_pdf_urls(html)
    meet_books = [u for u in pdfs if MEET_LEVEL_BOOK_RE.search(u)]
    if meet_books:
        return PdfPick("book", [meet_books[0]])
    meet_00 = [u for u in pdfs if MEET_LEVEL_00_RE.search(u)]
    if len(meet_00) == 1:
        return PdfPick("book", meet_00)
    return PdfPick("book", [])


def _pick_results_book_url(html: str, pdfs: list[str] | None = None) -> Optional[str]:
    """Back-compat: complete book URL, or None if the page is session-only."""
    pick = pick_result_pdfs(html)
    if pick.kind == "book" and pick.urls:
        return pick.urls[0]
    return None


def _wayback_snapshots(url: str, limit: int = 12) -> list[str]:
    """Return Wayback timestamps (newest first) for a URL.

    Tries an exact match first, then a soft match (Wayback sometimes
    indexes with trailing query params or http vs https differences).
    """
    timestamps: list[str] = []
    candidates = [url]
    if url.startswith("https://"):
        candidates.append("http://" + url[len("https://") :])
    for candidate in candidates:
        for params in (
            {
                "url": candidate,
                "output": "json",
                "fl": "timestamp,statuscode",
                "filter": "statuscode:200",
                "limit": limit,
            },
            {
                "url": candidate,
                "matchType": "prefix",
                "output": "json",
                "fl": "timestamp,statuscode",
                "filter": "statuscode:200",
                "limit": limit,
            },
        ):
            try:
                resp = HTTP.get(
                    "https://web.archive.org/cdx/search/cdx",
                    params=params,
                    timeout=12,
                )
                resp.raise_for_status()
                rows = resp.json()
                if rows and len(rows) >= 2:
                    timestamps.extend(row[0] for row in rows[1:])
                    return sorted(set(timestamps), reverse=True)
            except (requests.RequestException, ValueError, json.JSONDecodeError):
                continue
    return sorted(set(timestamps), reverse=True)


def _pick_from_event_html(html: str) -> Optional[PdfPick]:
    pick = pick_result_pdfs(html)
    if pick.urls:
        return pick
    return None


def discover_result_pdfs(event_page: str, *, try_omega: bool = True) -> PdfPick:
    """Find the complete results book, or per-event Result List PDFs.

    Live Omega is tried first (via Chrome). curl/requests hang on these
    pages; Wayback is the fallback when Chrome cannot load them.
    """
    last_err: Optional[Exception] = None

    if try_omega:
        try:
            html = get_omega_browser().get_html(event_page)
            picked = _pick_from_event_html(html)
            if picked:
                return picked
        except Exception as exc:  # noqa: BLE001 — fall back to Wayback
            last_err = exc
            print(f"  Omega page failed: {exc}")

    snapshots = _wayback_snapshots(event_page)
    for ts in snapshots[:6]:
        arch = f"https://web.archive.org/web/{ts}id_/{event_page}"
        try:
            time.sleep(0.5)
            resp = HTTP.get(arch, timeout=60)
            if not resp.ok:
                continue
            picked = _pick_from_event_html(resp.text)
            if picked:
                return picked
        except requests.RequestException as exc:
            last_err = exc
            continue

    if not snapshots:
        raise FileNotFoundError(
            f"No results PDFs at {event_page} and no Wayback snapshot."
            + (f" Last error: {last_err}" if last_err else "")
        )
    raise FileNotFoundError(
        f"Could not find results PDFs for {event_page}"
        + (f" ({last_err})" if last_err else "")
    )


def discover_pdf_url(event_page: str) -> str:
    """Find a single results-book URL, or the first session Result List."""
    pick = discover_result_pdfs(event_page)
    if not pick.urls:
        raise FileNotFoundError(f"Could not find results PDFs for {event_page}")
    return pick.urls[0]


COMPETITION_ID_RE = re.compile(
    r'competitionId"\s+content="([A-Fa-f0-9]+)"', re.I
)


def _file_stem(url: str) -> str:
    m = re.search(r"/File/([A-Fa-f0-9]+)\.pdf", url, re.I)
    return m.group(1).upper() if m else ""


def _meet_file_stems(meet: MeetConfig) -> list[str]:
    urls = list(meet.pdf_urls or [])
    if meet.pdf_url:
        urls.append(meet.pdf_url)
    return [s for u in urls if (s := _file_stem(u))]


def _html_title(html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", html, flags=re.I | re.S)
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


def match_html_to_meet(html: str, meets: list[MeetConfig]) -> Optional[MeetConfig]:
    """Map saved Omega HTML to a meet via competitionId, then page title."""
    cid_m = COMPETITION_ID_RE.search(html)
    if cid_m:
        cid = cid_m.group(1).upper().rstrip("F")
        best: Optional[MeetConfig] = None
        best_n = 0
        for meet in meets:
            for stem in _meet_file_stems(meet):
                n = 0
                for a, b in zip(cid, stem):
                    if a != b:
                        break
                    n += 1
                if n >= 8 and n > best_n:
                    best, best_n = meet, n
            slug = re.search(r"/([0-9A-Fa-f]{8,})-live-results", meet.event_page or "")
            if slug:
                code = slug.group(1).upper()
                n = 0
                for a, b in zip(cid, code):
                    if a != b:
                        break
                    n += 1
                if n >= 8 and n > best_n:
                    best, best_n = meet, n
        if best:
            return best

    title = _html_title(html).lower()
    named = [m for m in meets if m.name.lower() in title]
    if len(named) == 1:
        return named[0]
    return None


def ingest_html_file(
    path: Path,
    meets: list[MeetConfig],
    *,
    config_path: Path = DEFAULT_CONFIG,
    copy_cache: bool = True,
) -> tuple[MeetConfig, PdfPick]:
    html = path.read_text(encoding="utf-8", errors="replace")
    meet = match_html_to_meet(html, meets)
    if meet is None:
        raise FileNotFoundError(f"Could not match HTML to a meet: {path.name}")
    pick = pick_result_pdfs(html)
    if not pick.urls:
        raise FileNotFoundError(f"No Result List / results-book links in {path.name}")
    write_result_pdfs_to_config(meet.id, pick, config_path=config_path)
    _apply_pick(meet, pick)
    if copy_cache:
        RAW_HTML_DIR.mkdir(parents=True, exist_ok=True)
        dest = RAW_HTML_DIR / f"{meet.id}.html"
        dest.write_text(html, encoding="utf-8")
    return meet, pick


def save_event_page_html_chrome(event_page: str, dest: Path, *, wait_s: float = 8) -> str:
    """Load an Omega event page in Google Chrome and return the page HTML."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = event_page.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Google Chrome"
  activate
  if (count of windows) = 0 then make new window
  set URL of active tab of front window to "{url}"
end tell
delay {wait_s}
tell application "Google Chrome"
  execute active tab of front window javascript "document.documentElement.outerHTML"
end tell
'''
    proc = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=wait_s + 30,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "osascript failed").strip()
        raise RuntimeError(f"Chrome could not load {event_page}: {err}")
    html = proc.stdout
    if not html or len(html) < 200:
        raise RuntimeError(f"Chrome returned empty HTML for {event_page}")
    dest.write_text(html, encoding="utf-8")
    return html


def _pdf_urls_yaml(urls: list[str], indent: str = "    ") -> list[str]:
    block = [f"{indent}pdf_urls:\n"]
    block.extend(f"{indent}  - {u}\n" for u in urls)
    return block


def write_result_pdfs_to_config(
    meet_id: str,
    pick: PdfPick,
    config_path: Path = DEFAULT_CONFIG,
) -> None:
    """Write pdf_url (book) or pdf_urls (sessions) without wiping YAML comments."""
    if not pick.urls:
        raise ValueError("No PDF URLs to write")
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    out: list[str] = []
    i = 0
    in_meet = False
    wrote = False
    as_book = pick.kind == "book" or len(pick.urls) == 1

    def emit_block() -> None:
        nonlocal wrote
        if as_book:
            out.append(f"    pdf_url: {pick.urls[0]}\n")
        else:
            out.extend(_pdf_urls_yaml(pick.urls))
        wrote = True

    while i < len(lines):
        line = lines[i]
        if re.match(rf"^\s*-\s*id:\s*{re.escape(meet_id)}\s*$", line):
            in_meet = True
            wrote = False
            out.append(line)
            i += 1
            continue
        if in_meet and re.match(r"^\s*-\s*id:\s*", line):
            if not wrote:
                emit_block()
            in_meet = False
            out.append(line)
            i += 1
            continue
        if in_meet and re.match(r"^\s*pdf_urls:\s*", line):
            i += 1
            # Only skip indented list items (6+ spaces). `  - id:` is a new meet.
            while i < len(lines) and re.match(r"^      - \S", lines[i]):
                i += 1
            if not as_book and not wrote:
                emit_block()
            continue
        if in_meet and re.match(r"^\s*# ---", line):
            if not wrote:
                emit_block()
            in_meet = False
            out.append(line)
            i += 1
            continue
        if in_meet and re.match(r"^\s*pdf_url:\s*", line):
            if as_book:
                indent = re.match(r"^(\s*)", line).group(1)
                out.append(f"{indent}pdf_url: {pick.urls[0]}\n")
                wrote = True
            # sessions: drop the old single pdf_url
            i += 1
            continue
        if in_meet and re.match(r"^\s*#\s*pdf_url:\s*", line):
            if as_book:
                indent = re.match(r"^(\s*)", line).group(1)
                out.append(f"{indent}pdf_url: {pick.urls[0]}\n")
                wrote = True
                i += 1
                continue
        out.append(line)
        i += 1
    if in_meet and not wrote:
        emit_block()
        wrote = True
    if not wrote:
        raise KeyError(f"Meet id not in config: {meet_id}")
    config_path.write_text("".join(out), encoding="utf-8")


def write_pdf_url_to_config(
    meet_id: str, pdf_url: str, config_path: Path = DEFAULT_CONFIG
) -> None:
    """Insert or replace pdf_url for meet_id without wiping YAML comments."""
    write_result_pdfs_to_config(
        meet_id, PdfPick("book", [pdf_url]), config_path=config_path
    )


def _alternate_pdf_urls(pdf_url: str) -> list[str]:
    """Meet-level books use …1E / …03 / …00; try siblings if one isn't archived."""
    m = re.search(r"(https?://(?:www\.)?omegatiming\.com/File/[A-Fa-f0-9]+?)(1[Ee]|00|03)(\.pdf)$", pdf_url, re.I)
    if not m:
        return [pdf_url]
    base, cur, ext = m.group(1), m.group(2).upper(), m.group(3)
    ordered = []
    for suf in (cur, "1E", "03", "00"):
        u = f"{base}{suf}{ext}"
        if u not in ordered:
            ordered.append(u)
    return ordered


def _download_from_wayback(pdf_url: str) -> Optional[bytes]:
    """Return PDF bytes from a Wayback snapshot, or None if none work."""
    snapshots = _wayback_snapshots(pdf_url, limit=8)
    if not snapshots:
        return None
    for ts in snapshots[:5]:
        arch = f"https://web.archive.org/web/{ts}id_/{pdf_url}"
        try:
            print(f"  trying Wayback {ts}…")
            time.sleep(0.4)  # be polite to archive.org
            resp = HTTP.get(arch, timeout=120, allow_redirects=True)
            resp.raise_for_status()
            if resp.content.startswith(b"%PDF"):
                return resp.content
            print(f"  Wayback response not a PDF ({resp.headers.get('content-type')})")
        except requests.RequestException as exc:
            print(f"  Wayback {ts} failed: {exc}")
    return None


def download_pdf_bytes(
    pdf_url: str,
    *,
    try_omega: bool = True,
    event_page: Optional[str] = None,
    allow_alternates: bool = True,
) -> tuple[bytes, str]:
    """Download a results-book PDF via live Omega (Chrome), then Wayback.

    Returns ``(pdf_bytes, url_used)``. curl/requests hang on Omega File
    URLs; Chrome can fetch them after the origin page is loaded.

    ``allow_alternates`` tries sibling …1E / …03 / …00 suffixes. That is only
    safe for a meet-level book — old per-event Result Lists end in …01.pdf,
    where …00.pdf is the Start List.
    """
    candidates = _alternate_pdf_urls(pdf_url) if allow_alternates else [pdf_url]
    last_err: Optional[Exception] = None

    if try_omega:
        omega_candidates = candidates if allow_alternates else [pdf_url]
        # Prefer the configured URL; only try suffix alternates if it isn't a PDF.
        for url in omega_candidates:
            try:
                data = get_omega_browser().get_pdf(url)
                return data, url
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                print(f"  Omega failed: {exc}")
                if "Not a PDF" not in str(exc):
                    break

    for url in candidates:
        print(f"  looking up Wayback for {url.rsplit('/', 1)[-1]}…")
        data = _download_from_wayback(url)
        if data is not None:
            return data, url
        print("  no usable Wayback snapshot")

    raise RuntimeError(
        f"Failed to download {pdf_url}"
        + (f": {last_err}" if last_err else "")
    )


def configured_pdf_urls(meet: MeetConfig) -> list[str]:
    if meet.pdf_urls:
        return list(meet.pdf_urls)
    if meet.pdf_url:
        return [meet.pdf_url]
    return []


def _session_cache_dir(meet_id: str) -> Path:
    return RAW_PDF_DIR / meet_id


def cached_pdf_paths(meet: MeetConfig) -> list[Path]:
    if meet.local_path:
        local = (PROJECT_ROOT / meet.local_path).resolve()
        if local.exists():
            return [local]
    sess = _session_cache_dir(meet.id)
    if sess.is_dir():
        pdfs = sorted(p for p in sess.glob("*.pdf") if p.is_file())
        if pdfs:
            return pdfs
    if meet.pdf_urls:
        return []
    book = RAW_PDF_DIR / f"{meet.id}.pdf"
    if book.exists():
        return [book]
    return []


def _apply_pick(meet: MeetConfig, pick: PdfPick) -> None:
    if pick.kind == "book" or len(pick.urls) == 1:
        meet.pdf_url = pick.urls[0]
        meet.pdf_urls = None
    else:
        meet.pdf_url = None
        meet.pdf_urls = list(pick.urls)


def resolve_pdfs(
    meet: MeetConfig,
    force_download: bool = False,
    *,
    discover: bool = True,
    persist_discovered: bool = True,
    config_path: Path = DEFAULT_CONFIG,
    try_omega: bool = True,
) -> list[Path]:
    """Return cached / downloaded PDF path(s) for a meet.

    One complete results book is stored as ``data/raw/pdfs/{id}.pdf``.
    Per-event Result Lists (no book on the event page) go in
    ``data/raw/pdfs/{id}/01.pdf``, ``02.pdf``, …
    """
    RAW_PDF_DIR.mkdir(parents=True, exist_ok=True)

    if not force_download:
        cached = cached_pdf_paths(meet)
        if cached:
            return cached

    pick: Optional[PdfPick] = None
    urls = configured_pdf_urls(meet)
    should_discover = discover and bool(meet.event_page) and not urls
    if should_discover:
        print("Discovering result PDFs from event page…")
        try:
            pick = discover_result_pdfs(meet.event_page, try_omega=try_omega)
            urls = pick.urls
            _apply_pick(meet, pick)
            if pick.kind == "book" or len(urls) == 1:
                print(f"  complete results book: {urls[0]}")
            else:
                print(f"  no complete book; {len(urls)} session Result List PDFs")
            if persist_discovered:
                try:
                    write_result_pdfs_to_config(meet.id, pick, config_path=config_path)
                    print(f"  saved to {config_path.name}")
                except Exception as exc:  # noqa: BLE001 — non-fatal
                    print(f"  (could not update meets.yaml: {exc})")
        except FileNotFoundError as exc:
            if not urls:
                raise
            print(f"  discovery failed ({exc}); using config URL(s)")

    if not urls:
        raise FileNotFoundError(
            f"No PDF for meet '{meet.id}'. Set pdf_url / pdf_urls or event_page in meets.yaml."
        )

    is_sessions = bool(pick and pick.kind == "sessions" and len(urls) > 1)
    if pick is None:
        is_sessions = bool(meet.pdf_urls) and len(urls) > 1

    if is_sessions:
        dest_dir = _session_cache_dir(meet.id)
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        failures: list[str] = []
        for i, url in enumerate(urls, 1):
            dest = dest_dir / f"{i:02d}.pdf"
            if dest.exists() and not force_download:
                print(f"  cached {dest.name}")
                paths.append(dest)
                continue
            print(f"Downloading [{i}/{len(urls)}] {url} -> {dest}")
            try:
                data, _used = download_pdf_bytes(
                    url, try_omega=try_omega, allow_alternates=False
                )
                dest.write_bytes(data)
                paths.append(dest)
            except Exception as exc:  # noqa: BLE001
                print(f"  FAILED — {exc}", file=sys.stderr)
                failures.append(url)
            time.sleep(0.6)
        if not paths:
            raise RuntimeError(
                f"No session PDFs downloaded for '{meet.id}'"
                + (f" ({len(failures)} failed)" if failures else "")
            )
        if failures:
            print(
                f"  warning: {len(failures)}/{len(urls)} session PDFs failed",
                file=sys.stderr,
            )
        return paths

    cached = RAW_PDF_DIR / f"{meet.id}.pdf"
    pdf_url = urls[0]
    print(f"Downloading {pdf_url} -> {cached}")
    data, used_url = download_pdf_bytes(
        pdf_url, try_omega=try_omega, allow_alternates=True
    )
    cached.write_bytes(data)
    if used_url != pdf_url and persist_discovered:
        meet.pdf_url = used_url
        try:
            write_pdf_url_to_config(meet.id, used_url, config_path=config_path)
            print(f"  updated pdf_url -> {used_url}")
        except Exception as exc:  # noqa: BLE001
            print(f"  (could not update meets.yaml: {exc})")
    return [cached]


def resolve_pdf(
    meet: MeetConfig,
    force_download: bool = False,
    *,
    discover: bool = True,
    persist_discovered: bool = True,
    config_path: Path = DEFAULT_CONFIG,
    try_omega: bool = True,
) -> Path:
    return resolve_pdfs(
        meet,
        force_download=force_download,
        discover=discover,
        persist_discovered=persist_discovered,
        config_path=config_path,
        try_omega=try_omega,
    )[0]


def is_individual_event(event: str) -> bool:
    lower = event.lower()
    return not any(k in lower for k in SKIP_EVENT_KEYWORDS)


def parse_session_headers(lines: list[str]) -> tuple[str, str, str]:
    """Return (meet_name, event, event_date) from page header lines.

    Supports:
    - modern: ``Event 2 Men's 10m Platform Preliminary``
    - older: ``Event 206`` / next line ``Men's 3m Springboard Semifinal A``
    - Olympic/Rio-style: event name embedded in a venue line + round line
    """
    meet_name = lines[0].strip() if lines else ""
    event = ""
    event_date = ""
    head = [ln.strip() for ln in lines[:18] if ln.strip()]
    skip_titles = {
        "Detailed Results",
        "Result List",
        "DETAILED GENERAL RANKING",
        "Panel of Judges",
        "Start List",
        "Dive List",
        "Résultats détaillés",
        "Panel des juges",
    }
    event_base = ""
    round_name = ""
    for i, ln in enumerate(head):
        m = EVENT_RE.match(ln)
        if m:
            event = m.group(1).strip()
        elif EVENT_NUM_ONLY_RE.match(ln) and i + 1 < len(head):
            nxt = head[i + 1]
            if nxt not in skip_titles and not DATE_RE.match(nxt):
                event = nxt
        else:
            em = EVENT_NAME_RE.search(ln)
            if em and not event_base:
                event_base = em.group(1).strip()
            rm = ROUND_ONLY_RE.match(ln)
            if rm and not round_name:
                round_name = rm.group(1).strip()
        m = DATE_RE.match(ln)
        if m and not event_date:
            event_date = m.group(1).strip()

    if not event and event_base:
        event = f"{event_base} {round_name}".strip() if round_name else event_base
    return meet_name, event, event_date


def classify_page(text: str) -> str:
    """Classify by section title near the top of the page.

    Important: Detailed Results footers often *mention* "Panel of Judges" in a
    note, so a naive substring check mis-labels those pages as judges.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines[:20]:
        if ln in ("Detailed Results", "Result List", "DETAILED GENERAL RANKING"):
            return "detailed"
        if ln == "Panel of Judges":
            return "judges"
    # Fallback for unusual layouts
    if re.search(
        r"(?m)^(?:Detailed Results|Result List|DETAILED GENERAL RANKING)$", text
    ):
        return "detailed"
    if re.search(r"(?m)^Panel of Judges$", text):
        return "judges"
    return "other"


def _score_tokens(s: str) -> list[float]:
    return [float(x) for x in s.split()]


def parse_dive_fragment(fragment: str) -> Optional[dict]:
    """Parse a dive line fragment; return None if incomplete / DNS-only."""
    fragment = fragment.strip()
    if not fragment or fragment.upper().endswith("DNS") or fragment.upper() == "DNS":
        return None
    if "DNS" in fragment.upper():
        return None

    m = DIVE_LINE_RE.match(fragment)
    if not m:
        return None

    scores = _score_tokens(m.group("scores"))
    total_raw = m.group("total")
    overall_raw = m.group("overall") or ""
    return {
        "DiveCode": m.group("code"),
        "Difficulty": float(m.group("dd")),
        "scores": scores,
        "PenaltyFlag": bool(m.group("pen")),
        "DivePoints": float(m.group("dive_pts")),
        "DiveRank": m.group("dive_rank") or "",
        "TotalPoints": float(total_raw) if total_raw else None,
        "OverallRankRaw": overall_raw,
    }


def _looks_like_dive_start(s: str) -> bool:
    return bool(re.match(r"^\d{3,4}[A-Z]?\s+\d+\.\d\b", s.strip()))


def parse_detailed_results_pages(
    pages_text: list[tuple[str, str, str, list[str]]],
    meet_id: str,
) -> list[dict]:
    """
    pages_text: list of (meet_name, event, event_date, body_lines) for consecutive
    Detailed Results pages of the same session (caller groups by event).

    Dives are buffered per diver so wrapped given names (e.g. "Linus") are
    applied before rows are emitted.
    """
    rows: list[dict] = []
    current_diver: Optional[dict] = None
    pending_dives: list[tuple[str, str, str, dict]] = []
    dns = False

    def flush_diver():
        nonlocal current_diver, pending_dives, dns
        if current_diver and pending_dives and not dns:
            known_totals = [
                d["TotalPoints"]
                for _, _, _, d in pending_dives
                if d["TotalPoints"] is not None
            ]
            fill_total = known_totals[-1] if known_totals else None
            rank_hint = current_diver.get("RankHint") or ""
            for dive_no, (meet_name, event, event_date, dive) in enumerate(
                pending_dives, start=1
            ):
                overall = re.sub(r"^=", "", dive["OverallRankRaw"] or "")
                if not overall:
                    overall = re.sub(r"^=", "", str(rank_hint))
                total = dive["TotalPoints"] if dive["TotalPoints"] is not None else fill_total
                row = {
                    "Meet": meet_name,
                    "MeetId": meet_id,
                    "Event": event,
                    "Round": _round_from_event(event),
                    "EventDate": event_date,
                    "Diver": current_diver["Diver"],
                    "Country": current_diver["Country"],
                    "OverallRank": int(overall) if overall.isdigit() else overall,
                    "DiveNo": dive_no,
                    "DiveCode": dive["DiveCode"],
                    "Difficulty": dive["Difficulty"],
                }
                for i, score in enumerate(dive["scores"], start=1):
                    row[f"JScore{i}"] = score
                row["PenaltyFlag"] = dive["PenaltyFlag"]
                row["DivePoints"] = dive["DivePoints"]
                row["TotalPoints"] = total
                rows.append(row)
        current_diver = None
        pending_dives = []
        dns = False

    def queue_dive(meet_name: str, event: str, event_date: str, dive: dict):
        pending_dives.append((meet_name, event, event_date, dive))

    for meet_name, event, event_date, lines in pages_text:
        for ln in lines:
            if _is_noise_line(ln):
                continue
            if re.match(r"^(?:Judge|Referee|Assistant|PANEL)\b", ln.strip(), re.I):
                continue

            # Continuation: wrapped given name, then maybe dive
            if current_diver and not _looks_like_dive_start(ln) and not parse_diver_header(ln):
                dive_m = re.search(
                    r"(\d{3,4}[A-Z]?\s+\d+\.\d\s+.+)$", ln.strip()
                )
                if dive_m:
                    name_prefix = ln.strip()[: dive_m.start(1)].strip()
                    if (
                        name_prefix
                        and not name_prefix[0].isdigit()
                        and _is_name_continuation(name_prefix)
                    ):
                        current_diver["Diver"] = (
                            f"{current_diver['Diver']} {name_prefix}"
                        ).strip()
                    if dns:
                        continue
                    dive = parse_dive_fragment(dive_m.group(1))
                    if dive:
                        queue_dive(meet_name, event, event_date, dive)
                    continue
                # Pure name wrap with no dive on this line
                if _is_name_continuation(ln):
                    current_diver["Diver"] = f"{current_diver['Diver']} {ln.strip()}".strip()
                    continue

            # Bare dive continuation for current diver
            if current_diver and _looks_like_dive_start(ln):
                if dns:
                    continue
                dive = parse_dive_fragment(ln.strip())
                if dive:
                    queue_dive(meet_name, event, event_date, dive)
                continue

            # New diver
            header = parse_diver_header(ln)
            if header:
                flush_diver()
                given = header["given"]
                diver_name = f"{header['surname']} {given}".strip()
                current_diver = {
                    "Diver": diver_name,
                    "Country": header["nat"],
                    "RankHint": header["rank"],
                }
                rest = header["rest"]
                if "DNS" in rest.upper():
                    dns = True
                    continue
                if rest:
                    dive = parse_dive_fragment(rest)
                    if dive:
                        queue_dive(meet_name, event, event_date, dive)
                continue

    flush_diver()
    return rows


def _round_from_event(event: str) -> str:
    lower = event.lower()
    if "preliminary" in lower or "preliminar" in lower:
        return "Preliminary"
    if "semi" in lower:
        return "Semifinal"
    if "final" in lower:
        return "Final"
    return "Unknown"


def parse_panel_of_judges(
    meet_name: str,
    event: str,
    event_date: str,
    lines: list[str],
    meet_id: str,
) -> list[dict]:
    rows: list[dict] = []
    panel = "single"
    rounds_covered = ""
    panel_rounds: dict[str, str] = {}
    default_rounds = "1-6"

    def emit(judge_no: int, name: str, which_panel: str, rounds: str) -> None:
        name = name.strip()
        if not name:
            return
        rows.append(
            {
                "Meet": meet_name,
                "MeetId": meet_id,
                "Event": event,
                "Round": _round_from_event(event),
                "EventDate": event_date,
                "Panel": which_panel,
                "RoundsCovered": rounds or default_rounds,
                "Function": f"Judge {judge_no}",
                "JudgeNo": f"J{judge_no}",
                "JudgeName": name,
            }
        )

    for ln in lines:
        s = ln.strip()
        headers = PANEL_HEADER_RE.findall(s)
        if headers:
            for letter, a, b in headers:
                panel = letter.upper()
                rounds_covered = f"{a}-{b}"
                panel_rounds[panel] = rounds_covered
            continue
        if re.match(r"^PANEL\s+A\s+PANEL\s+B", s, re.I):
            panel = "A"
            continue
        inline = JUDGE_INLINE_RE.findall(s)
        if len(inline) >= 2:
            for i, (num, name, _nat) in enumerate(inline):
                which = "A" if i == 0 else "B"
                emit(int(num), f"{name} {_nat}", which, panel_rounds.get(which, default_rounds))
            continue
        if inline:
            num, name, nat = inline[0]
            emit(
                int(num),
                f"{name} {nat}",
                panel,
                panel_rounds.get(panel, rounds_covered or default_rounds),
            )
            continue
        if _is_noise_line(s) and not s.startswith("Panel"):
            continue
        jm = JUDGE_LINE_RE.match(s)
        if jm:
            emit(
                int(jm.group(1)),
                jm.group(2).strip(),
                panel,
                panel_rounds.get(panel, rounds_covered or default_rounds),
            )
    return rows


def extract_body_lines(text: str) -> list[str]:
    lines = [ln.rstrip() for ln in text.splitlines()]
    # Drop trailing footer-ish lines but keep content; filtering happens later
    return [ln for ln in lines if ln.strip()]


def group_detailed_sessions(
    page_records: list[dict],
) -> list[list[dict]]:
    """Group consecutive individual Detailed Results pages by Event."""
    sessions: list[list[dict]] = []
    current: list[dict] = []
    current_key = None
    for rec in page_records:
        if rec["page_type"] != "detailed" or not rec["is_individual"]:
            if current:
                sessions.append(current)
                current = []
                current_key = None
            continue
        key = (rec["event"], rec["event_date"])
        if current_key is None:
            current_key = key
            current = [rec]
        elif key == current_key:
            current.append(rec)
        else:
            sessions.append(current)
            current = [rec]
            current_key = key
    if current:
        sessions.append(current)
    return sessions


def parse_pdf(pdf_path: Path, meet: MeetConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    page_records: list[dict] = []
    judge_rows: list[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = extract_body_lines(text)
            meet_name, event, event_date = parse_session_headers(lines)
            # Prefer config meet name for consistency
            meet_name = meet.name or meet_name
            page_type = classify_page(text)
            individual = is_individual_event(event) if event else False

            if page_type == "judges" and individual:
                body = lines
                judge_rows.extend(
                    parse_panel_of_judges(
                        meet_name, event, event_date, body, meet.id
                    )
                )
            elif page_type == "detailed":
                page_records.append(
                    {
                        "meet_name": meet_name,
                        "event": event,
                        "event_date": event_date,
                        "page_type": page_type,
                        "is_individual": individual,
                        "lines": lines,
                    }
                )
                if individual:
                    judge_rows.extend(
                        parse_panel_of_judges(
                            meet_name, event, event_date, lines, meet.id
                        )
                    )

    score_rows: list[dict] = []
    for session in group_detailed_sessions(page_records):
        pages_text = [
            (r["meet_name"], r["event"], r["event_date"], r["lines"]) for r in session
        ]
        score_rows.extend(parse_detailed_results_pages(pages_text, meet.id))

    scores_df = pd.DataFrame(score_rows)
    judges_df = pd.DataFrame(judge_rows)
    return scores_df, judges_df


def rounds_covered_to_set(spec: str) -> set[int]:
    if not spec or "-" not in spec:
        return set()
    a, b = spec.split("-", 1)
    return set(range(int(a), int(b) + 1))


def upsert_csv(path: Path, new_df: pd.DataFrame, meet_id: str) -> None:
    """Replace rows for meet_id in a combined CSV, then rewrite the file."""
    if path.exists():
        old = pd.read_csv(path, dtype={"MeetId": str}, low_memory=False)
        if "MeetId" in old.columns:
            old = old[old["MeetId"].astype(str) != str(meet_id)]
        combined = pd.concat([old, new_df], ignore_index=True)
    else:
        combined = new_df
    tmp = path.with_suffix(path.suffix + ".tmp")
    combined.to_csv(tmp, index=False)
    tmp.replace(path)


def join_judge_names(scores: pd.DataFrame, judges: pd.DataFrame) -> pd.DataFrame:
    """Attach JudgeName1..7 (and Panel) to each wide dive row."""
    out = scores.copy()
    for col in JNAME_COLS:
        out[col] = pd.NA
    out["Panel"] = pd.NA

    if scores.empty or judges.empty:
        return out

    # Map (Event, DiveNo, JudgeNo) -> (JudgeName, Panel)
    lookup: dict[tuple, tuple] = {}
    for _, j in judges.iterrows():
        for dive_no in sorted(rounds_covered_to_set(str(j["RoundsCovered"]))):
            key = (j["Event"], dive_no, j["JudgeNo"])
            lookup[key] = (j["JudgeName"], j["Panel"])

    panels = []
    name_cols = {c: [] for c in JNAME_COLS}
    for _, row in out.iterrows():
        panel_vals = set()
        for i in range(1, 8):
            key = (row["Event"], row["DiveNo"], f"J{i}")
            if key in lookup:
                name, panel = lookup[key]
                name_cols[f"JudgeName{i}"].append(name)
                panel_vals.add(panel)
            else:
                name_cols[f"JudgeName{i}"].append(pd.NA)
        if len(panel_vals) == 1:
            panels.append(next(iter(panel_vals)))
        elif not panel_vals:
            panels.append(pd.NA)
        else:
            panels.append("/".join(sorted(panel_vals)))

    for c in JNAME_COLS:
        out[c] = name_cols[c]
    out["Panel"] = panels
    return out


def validate(scores: pd.DataFrame, judges: pd.DataFrame) -> None:
    print("\n=== Validation ===")
    if scores.empty:
        print("WARNING: no score rows parsed.")
        return

    n = len(scores)
    print(f"Score rows (one per dive): {n:,}")
    print(f"Events: {scores['Event'].nunique()}")
    print(f"Divers: {scores['Diver'].nunique()}")
    print(f"Judge panels rows: {len(judges):,}")

    # Half-point grid check across JScore1..7
    stacked = scores[JSCORE_COLS].to_numpy().ravel()
    ok = [
        (x == 0.0) or (abs(x * 2 - round(x * 2)) < 1e-9 and 0 <= x <= 10)
        for x in stacked
    ]
    print(f"JScores on half-point grid [0,10]: {np.mean(ok):.1%}")

    # Spot-check DivePoints ≈ sum(middle 3 of 7) × DD (FINA / World Aquatics)
    def row_fina(row):
        s = sorted(float(row[c]) for c in JSCORE_COLS)
        return sum(s[2:5]) * float(row["Difficulty"])

    check = scores.copy()
    check["calc"] = check.apply(row_fina, axis=1)
    check = check[~check["PenaltyFlag"].astype(bool)]
    if not check.empty:
        err = (check["calc"] - check["DivePoints"]).abs()
        print(
            f"DivePoints vs sum(middle 3)×DD (non-penalty): "
            f"median |err|={err.median():.4f}, "
            f"share <0.05={((err < 0.05).mean()):.1%} (n={len(check)})"
        )

    score_events = set(scores["Event"].unique())
    judge_events = set(judges["Event"].unique()) if not judges.empty else set()
    missing_panels = score_events - judge_events
    if missing_panels:
        print(f"WARNING: Detailed Results without Panel of Judges: {sorted(missing_panels)}")
    else:
        print("All scored events have a Panel of Judges page.")

    print("\nRows by event:")
    print(scores.groupby("Event").size().sort_values(ascending=False).to_string())


def export_meet(meet: MeetConfig, scores: pd.DataFrame, judges: pd.DataFrame) -> None:
    """Upsert this meet into the combined scores / judges / judge_scores CSVs."""
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)

    scores = scores[[c for c in SCORE_COLS if c in scores.columns]]
    judges = judges[[c for c in JUDGE_COLS if c in judges.columns]]
    joined = join_judge_names(scores, judges)
    joined = joined[[c for c in JOINED_COLS if c in joined.columns]]

    upsert_csv(SCORES_CSV, scores, meet.id)
    upsert_csv(JUDGES_CSV, judges, meet.id)
    upsert_csv(JUDGE_SCORES_CSV, joined, meet.id)

    print(f"\nUpserted meet '{meet.id}' into:")
    print(f"  {SCORES_CSV}")
    print(f"  {JUDGES_CSV}")
    print(f"  {JUDGE_SCORES_CSV}")


def run_meet(
    meet: MeetConfig,
    force_download: bool = False,
    config_path: Path = DEFAULT_CONFIG,
    try_omega: bool = True,
) -> None:
    print(f"\n======= {meet.id}: {meet.name} =======")
    pdf_paths = resolve_pdfs(
        meet,
        force_download=force_download,
        config_path=config_path,
        try_omega=try_omega,
    )
    score_parts: list[pd.DataFrame] = []
    judge_parts: list[pd.DataFrame] = []
    for pdf_path in pdf_paths:
        print(f"PDF: {pdf_path}")
        scores, judges = parse_pdf(pdf_path, meet)
        if not scores.empty:
            score_parts.append(scores)
        if not judges.empty:
            judge_parts.append(judges)
    scores = (
        pd.concat(score_parts, ignore_index=True) if score_parts else pd.DataFrame()
    )
    judges = (
        pd.concat(judge_parts, ignore_index=True) if judge_parts else pd.DataFrame()
    )
    validate(scores, judges)
    export_meet(meet, scores, judges)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Parse Omega Timing diving results books.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to meets.yaml",
    )
    parser.add_argument(
        "--meet",
        type=str,
        default=None,
        help="Meet id to parse (default: all)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Parse all meets in the config",
    )
    parser.add_argument(
        "--force-download",
        action="store_true",
        help="Re-download PDF even if cached",
    )
    parser.add_argument(
        "--discover-pdfs",
        action="store_true",
        help="Find complete results book or per-event Result List PDFs (Omega, then Wayback)",
    )
    parser.add_argument(
        "--download-pdfs",
        action="store_true",
        help="Only download PDFs into data/raw/pdfs/ (no parse)",
    )
    parser.add_argument(
        "--from-html",
        type=Path,
        default=None,
        help="Extract Result List / book links from a saved Omega HTML file or folder",
    )
    parser.add_argument(
        "--save-html",
        action="store_true",
        help="Open each event_page in Google Chrome, save page source, extract PDF links",
    )
    parser.add_argument(
        "--skip-omega",
        action="store_true",
        help="Do not hit live omegatiming.com (Wayback only)",
    )
    parser.add_argument(
        "--try-omega",
        action="store_true",
        help=argparse.SUPPRESS,  # deprecated: Omega is now the default
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    try_omega = not args.skip_omega

    meets = load_meets(args.config)
    if not meets:
        print("No meets in config.", file=sys.stderr)
        return 1

    if args.meet:
        selected = [m for m in meets if m.id == args.meet]
        if not selected:
            print(f"Unknown meet id: {args.meet}", file=sys.stderr)
            print("Available:", ", ".join(m.id for m in meets), file=sys.stderr)
            return 1
    else:
        selected = meets  # default: all

    def _report_pick(meet: MeetConfig, pick: PdfPick) -> None:
        if pick.kind == "book" or len(pick.urls) == 1:
            print(f"  complete results book: {pick.urls[0]}")
        else:
            print(f"  no complete book; {len(pick.urls)} session Result List PDFs")
            for u in pick.urls:
                print(f"    {u}")

    if args.from_html:
        html_path = args.from_html.expanduser()
        files = (
            sorted(html_path.glob("*.html"))
            if html_path.is_dir()
            else [html_path]
        )
        if not files:
            print(f"No HTML files in {html_path}", file=sys.stderr)
            return 1
        failures = 0
        for path in files:
            try:
                meet, pick = ingest_html_file(
                    path, meets, config_path=args.config
                )
                print(f"{path.name} -> {meet.id}")
                _report_pick(meet, pick)
            except Exception as exc:  # noqa: BLE001
                print(f"{path.name}: FAILED — {exc}", file=sys.stderr)
                failures += 1
        return 1 if failures else 0

    if args.save_html:
        failures = 0
        for meet in selected:
            if meet.pdf_urls and not args.force_download:
                print(f"{meet.id}: already has {len(meet.pdf_urls)} session pdf_urls")
                continue
            if not meet.event_page:
                print(f"{meet.id}: missing event_page", file=sys.stderr)
                continue
            dest = RAW_HTML_DIR / f"{meet.id}.html"
            print(f"{meet.id}: saving page source via Chrome…")
            try:
                html = save_event_page_html_chrome(meet.event_page, dest)
                pick = pick_result_pdfs(html)
                if not pick.urls:
                    raise FileNotFoundError("no Result List / results-book links in page")
                write_result_pdfs_to_config(meet.id, pick, config_path=args.config)
                _apply_pick(meet, pick)
                print(f"  saved {dest}")
                _report_pick(meet, pick)
            except Exception as exc:  # noqa: BLE001
                print(f"{meet.id}: FAILED — {exc}", file=sys.stderr)
                failures += 1
        return 1 if failures else 0

    if args.discover_pdfs:
        for meet in selected:
            if meet.pdf_urls:
                print(f"{meet.id}: already has {len(meet.pdf_urls)} session pdf_urls")
                continue
            if not meet.event_page:
                print(f"{meet.id}: missing event_page", file=sys.stderr)
                continue
            print(f"{meet.id}: discovering…")
            try:
                pick = discover_result_pdfs(meet.event_page, try_omega=try_omega)
            except Exception as exc:  # noqa: BLE001
                print(f"{meet.id}: FAILED — {exc}", file=sys.stderr)
                continue
            write_result_pdfs_to_config(meet.id, pick, config_path=args.config)
            if pick.kind == "book" or len(pick.urls) == 1:
                print(f"  complete results book: {pick.urls[0]}")
            else:
                print(f"  no complete book; {len(pick.urls)} session Result List PDFs")
                for u in pick.urls:
                    print(f"    {u}")
        return 0

    if args.download_pdfs:
        failures = []
        for meet in selected:
            try:
                paths = resolve_pdfs(
                    meet,
                    force_download=args.force_download,
                    config_path=args.config,
                    try_omega=try_omega,
                )
                for path in paths:
                    print(f"{meet.id}: {path} ({path.stat().st_size:,} bytes)")
            except Exception as exc:  # noqa: BLE001
                print(f"{meet.id}: FAILED — {exc}", file=sys.stderr)
                failures.append(meet.id)
            time.sleep(OMEGA_PAUSE_S if try_omega else 0.6)
        if failures:
            print(
                f"\n{len(failures)} failed"
                + ("" if try_omega else " (re-run without --skip-omega to try live Omega)")
                + ": "
                + ", ".join(failures),
                file=sys.stderr,
            )
        return 1 if failures else 0

    parse_failures = []
    for meet in selected:
        try:
            run_meet(
                meet,
                force_download=args.force_download,
                config_path=args.config,
                try_omega=try_omega,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{meet.id}: FAILED — {exc}", file=sys.stderr)
            parse_failures.append(meet.id)
    return 1 if parse_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
