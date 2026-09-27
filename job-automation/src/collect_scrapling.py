# Exemple d'exécution (depuis job-automation/src) :
#   python collect_scrapling.py "https://stagiaires.ma/stage-emploi-maroc/6612"
#   python collect_scrapling.py "https://ma.indeed.com/viewjob?jk=0123456789abcdef" --slug offre4
#
# Récupère UNE offre à partir de son URL :
#   Fetcher -> DynamicFetcher -> StealthyFetcher (escalade tant que la page est bloquée/vide),
#   puis JSON-LD JobPosting si présent, sinon repli sur des sélecteurs CSS.
# En ligne de commande : sauvegarde data/<slug>.txt et output/<slug>_meta.json.
# Via l'API (api_server.py /extract) : rien n'est écrit, l'offre est renvoyée en JSON.

import argparse
import html
import json
import logging
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

HTTP_TIMEOUT_S = 20
BROWSER_TIMEOUT_MS = 45_000
MIN_DESCRIPTION_CHARS = 200

BLOCK_MARKERS = (
    "just a moment",
    "cf-challenge",
    "challenge-platform",
    "additional verification required",
    "verify you are human",
    "request blocked",
    "access denied",
    "captcha",
)

TITLE_SELECTORS = (
    'h1[data-testid="jobsearch-JobInfoHeader-title"]',
    "h1.jobsearch-JobInfoHeader-title",
    "h1",
    '[class*="job-title"]',
    "title",
)
DESCRIPTION_SELECTORS = (
    "#jobDescriptionText",
    '[data-testid="jobDescriptionText"]',
    '[class*="job-description"]',
    '[class*="jobDescription"]',
    '[class*="description"]',
    "article",
    "main",
)

logging.getLogger("scrapling").setLevel(logging.WARNING)


class FetchError(Exception):
    def __init__(self, url: str, attempts: list[str]):
        self.url = url
        self.attempts = attempts
        super().__init__(f"Échec du chargement de {url} : " + " | ".join(attempts))


@dataclass
class FetchResult:
    page: object
    method: str
    attempts: list[str] = field(default_factory=list)


def looks_blocked(page) -> bool:
    status = getattr(page, "status", 200) or 200
    if status >= 400:
        return True
    body = (page.html_content or "")[:20_000].lower()
    return len(body) < 500 or any(m in body for m in BLOCK_MARKERS)


def fetch_with_escalation(
    url: str,
    *,
    is_usable: Optional[Callable] = None,
    capture_xhr: Optional[str] = None,
    before_attempt: Optional[Callable[[str], None]] = None,
) -> FetchResult:
    """Essaie Fetcher, puis DynamicFetcher, puis StealthyFetcher.

    Passe à la stratégie suivante si la requête échoue, si la page ressemble
    à un blocage, ou si `is_usable(page)` renvoie False. Lève FetchError si
    les trois échouent. Chaque stratégie est bornée par un timeout, sans retry.
    """
    browser_opts = dict(
        headless=True,
        network_idle=True,
        disable_resources=True,
        timeout=BROWSER_TIMEOUT_MS,
        retries=1,
    )
    if capture_xhr:
        browser_opts["capture_xhr"] = capture_xhr

    strategies = (
        ("Fetcher", lambda: Fetcher.get(url, stealthy_headers=True, timeout=HTTP_TIMEOUT_S, retries=1)),
        ("DynamicFetcher", lambda: DynamicFetcher.fetch(url, **browser_opts)),
        ("StealthyFetcher", lambda: StealthyFetcher.fetch(url, solve_cloudflare=True, **browser_opts)),
    )

    attempts: list[str] = []
    for name, fetch in strategies:
        if before_attempt:
            before_attempt(name)
        try:
            page = fetch()
        except Exception as e:
            attempts.append(f"{name}: {type(e).__name__}: {str(e).splitlines()[0][:150]}")
            continue
        if looks_blocked(page):
            attempts.append(f"{name}: page bloquée ou vide (HTTP {page.status})")
            continue
        if is_usable and not is_usable(page):
            attempts.append(f"{name}: HTTP {page.status} mais contenu inexploitable")
            continue
        attempts.append(f"{name}: OK (HTTP {page.status})")
        return FetchResult(page=page, method=name, attempts=attempts)

    raise FetchError(url, attempts)


# ---------- Extraction ----------

def html_to_text(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|ul|ol|tr)>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t\xa0]+", " ", l).strip() for l in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _iter_nodes(data):
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from _iter_nodes(v)
    elif isinstance(data, list):
        for v in data:
            yield from _iter_nodes(v)


def _is_type(node: dict, wanted: str) -> bool:
    t = node.get("@type")
    return t == wanted or (isinstance(t, list) and wanted in t)


def find_jsonld(page, wanted_type: str) -> list[dict]:
    found = []
    for script in page.css('script[type="application/ld+json"]'):
        raw = script.text or script.get_all_text()
        try:
            data = json.loads(raw, strict=False)
        except (ValueError, TypeError):
            continue
        found.extend(n for n in _iter_nodes(data) if _is_type(n, wanted_type))
    return found


def _name(value) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    if isinstance(value, list):
        return ", ".join(filter(None, (_name(v) for v in value)))
    return str(value or "")


def _location(value) -> str:
    places = value if isinstance(value, list) else [value]
    out = []
    for p in places:
        addr = p.get("address", {}) if isinstance(p, dict) else {}
        if isinstance(addr, dict):
            out.append(", ".join(filter(None, (addr.get("addressLocality"), _name(addr.get("addressCountry"))))))
    return " / ".join(filter(None, out))


def _first_text(page, selectors, separator=" ") -> str:
    for sel in selectors:
        for el in page.css(sel):
            text = el.get_all_text(separator=separator, strip=True)
            if text:
                return text.strip()
    return ""


def extract_job(page) -> dict:
    """JSON-LD JobPosting si présent, sinon repli CSS. Ne lève pas d'exception."""
    postings = find_jsonld(page, "JobPosting")
    if postings:
        page_url = str(page.url).rstrip("/")
        posting = next((p for p in postings if str(p.get("url", "")).rstrip("/") == page_url), postings[0])
        description = html_to_text(posting.get("description", ""))
        if len(description) >= MIN_DESCRIPTION_CHARS:
            return {
                "title": html.unescape(str(posting.get("title") or "")).strip(),
                "company": _name(posting.get("hiringOrganization")),
                "location": _location(posting.get("jobLocation")),
                "date_posted": str(posting.get("datePosted") or ""),
                "description": description,
                "jsonld_found": True,
                "extraction": "json-ld",
            }

    return {
        "title": _first_text(page, TITLE_SELECTORS),
        "company": "",
        "location": "",
        "date_posted": "",
        "description": _first_text(page, DESCRIPTION_SELECTORS, separator="\n"),
        "jsonld_found": bool(postings),
        "extraction": "css",
    }


def has_job_content(page) -> bool:
    job = extract_job(page)
    return bool(job["title"]) and len(job["description"]) >= MIN_DESCRIPTION_CHARS


# ---------- Sauvegarde ----------

def slugify(text: str, max_len: int = 60) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "offre"


def save_job(job: dict, slug: str, url: str, method: str) -> tuple[Path, Path]:
    DATA_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)

    # Le titre reste en 1re ligne : analyzer.extract_job_title() lit la première ligne.
    header = [job["title"]] + [x for x in (job["company"], job["location"]) if x]
    txt_path = DATA_DIR / f"{slug}.txt"
    txt_path.write_text("\n".join(header) + "\n\n" + job["description"] + "\n", encoding="utf-8")

    meta = {
        "url": url,
        "title": job["title"],
        "company": job["company"],
        "location": job["location"],
        "date_posted": job["date_posted"],
        "fetch_method": method,
        "jsonld_found": job["jsonld_found"],
        "extraction": job["extraction"],
        "text_length": len(job["description"]),
        "txt_path": str(txt_path.relative_to(ROOT)),
        "collected_at": datetime.now().isoformat(timespec="seconds"),
    }
    meta_path = OUTPUT_DIR / f"{slug}_meta.json"
    meta_path.write_text(json.dumps(meta, indent=4, ensure_ascii=False), encoding="utf-8")
    return txt_path, meta_path


def collect(url: str, slug: Optional[str] = None, slug_prefix: str = "",
            before_attempt: Optional[Callable[[str], None]] = None) -> dict:
    """Pipeline complet pour une URL : fetch avec escalade, extraction, sauvegarde."""
    result = fetch_with_escalation(url, is_usable=has_job_content, before_attempt=before_attempt)
    job = extract_job(result.page)
    slug = slug or "-".join(filter(None, (slug_prefix, slugify(job["title"]))))
    txt_path, meta_path = save_job(job, slug, url, result.method)
    return {**job, "url": url, "slug": slug, "fetch_method": result.method,
            "attempts": result.attempts, "txt_path": txt_path, "meta_path": meta_path}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Extrait une offre d'emploi depuis son URL.")
    parser.add_argument("url")
    parser.add_argument("--slug", help="Nom de fichier (défaut : dérivé du titre)")
    args = parser.parse_args()

    start = time.time()
    try:
        info = collect(args.url, args.slug)
    except FetchError as e:
        print("❌ " + str(e))
        sys.exit(1)
    print(f"✅ {info['title']} — {info['fetch_method']}, "
          f"{'JSON-LD' if info['jsonld_found'] else 'CSS'}, {len(info['description'])} caractères "
          f"({time.time() - start:.0f}s)")
    print(f"   {info['txt_path']}\n   {info['meta_path']}")


if __name__ == "__main__":
    main()
