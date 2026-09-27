# API HTTP locale pour n8n : extraction d'une offre à partir de son URL.
# Réutilise collect_scrapling (escalade Fetcher -> DynamicFetcher -> StealthyFetcher,
# JSON-LD puis repli CSS) et analyzer (technologies). Rien n'est écrit sur disque :
# l'offre est renvoyée directement en JSON.
#
# Écoute uniquement sur 127.0.0.1:8000, sans authentification (usage local strict).
# Depuis n8n (conteneur Docker), l'URL à appeler est :
#   http://host.docker.internal:8000/extract
# (dans le conteneur, 127.0.0.1 désigne le conteneur lui-même, pas ce PC).
#
# Exemple d'appel :
#   curl -X POST http://127.0.0.1:8000/extract -H "Content-Type: application/json" \
#        -d '{"url": "https://www.rekrute.com/offre-emploi-...-186772.html"}'
#   -> 200 {"job_title": "...", "company": "...", "location": "...",
#           "description": "...", "technologies": ["Java", "SQL", "Git"]}
#   -> 502 {"detail": {"error": "Échec du chargement de la page", "url": "...", "attempts": [...]}}
#
# Une extraction peut prendre de quelques secondes (Fetcher) à ~2 minutes (escalade
# jusqu'au navigateur furtif) : régler le timeout du nœud HTTP Request de n8n en conséquence.
#
# Recherche (GET /search?days=3&max=3[&q=...]) : une page de résultats Indeed (tri par pertinence) des N derniers
# jours ; sans q : métiers de l'IA, avec q : titres contenant les mots de q. 3 offres au maximum, les plus récentes,
# renvoyées sans extraction : {"offers": [{"url", "job_title", "company", "location", "posted"}]}.
#
# Lancer le serveur (depuis job-automation/src) :
#   uvicorn api_server:app --host 127.0.0.1 --port 8000

import json
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from analyzer import extract_technologies  # noqa: E402
from collect_scrapling import FetchError, extract_job, fetch_with_escalation, has_job_content  # noqa: E402
from ui_routes import router as ui_router  # noqa: E402

HOST = "127.0.0.1"
PORT = 8000
INDEED_DOMAIN = "ma.indeed.com"
SEARCH_MAX = 3  # plafond strict d'offres renvoyées par /search
DEFAULT_AI_QUERY = ('"AI Engineer" OR MLOps OR "Machine Learning" OR "Data Scientist" OR LLM OR '
                    '"Intelligence Artificielle" OR "Deep Learning" OR "IA"')
# Filtre de pertinence sur le titre : la recherche Indeed avec OR ramène aussi des offres hors sujet.
AI_TITLE_RE = re.compile(
    r"\b(ai|ia|ml|mlops|llm|llms|genai|gen ai|nlp|machine learning|deep learning|data scien\w*|"
    r"intelligence artificielle|artificial intelligence|computer vision|vision par ordinateur)\b", re.I)
# Métiers non techniques dont le titre mentionne souvent « AI » (ex. « Stage Marketing Digital, SEO / AI »).
NON_TECH_TITLE_RE = re.compile(
    r"\b(marketing|seo|geo|content|contenu|community|communication|commercial\w*|sales|vente\w*|recrut\w*|"
    r"formateur|formatrice|rh|ressources humaines)\b", re.I)
CV_PATH = Path(__file__).resolve().parent.parent / "data" / "cv_source.tex"
PROJECTS_PATH = Path(__file__).resolve().parent.parent / "data" / "projects_catalog.json"

log = logging.getLogger("uvicorn.error")
app = FastAPI(title="job-automation API", version="1.0",
              description="Extraction d'offres d'emploi pour n8n (usage local).")
app.include_router(ui_router)  # interface web : http://127.0.0.1:8000/ui


class ExtractRequest(BaseModel):
    url: str


class ExtractResponse(BaseModel):
    job_title: str
    company: str
    location: str
    description: str
    technologies: list[str]


def bad_gateway(error: str, url: str, **extra) -> HTTPException:
    log.warning("Extraction échouée pour %s : %s %s", url, error, extra or "")
    return HTTPException(status_code=502, detail={"error": error, "url": url, **extra})


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/cv")
def cv() -> dict:
    """CV source LaTeX (data/cv_source.tex), relu à chaque appel."""
    try:
        return {"cv_latex": CV_PATH.read_text(encoding="utf-8")}
    except OSError as e:
        log.error("Lecture du CV impossible (%s) : %s", CV_PATH, e)
        raise HTTPException(status_code=500, detail={"error": f"CV introuvable ou illisible : {CV_PATH.name}"})


@app.get("/projects")
def projects() -> dict:
    """Catalogue des projets (data/projects_catalog.json), renvoyé tel quel, relu à chaque appel."""
    try:
        return json.loads(PROJECTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.error("Lecture du catalogue impossible (%s) : %s", PROJECTS_PATH, e)
        raise HTTPException(status_code=500,
                            detail={"error": f"Catalogue introuvable ou JSON invalide : {PROJECTS_PATH.name}"})


def _indeed_cards(html: str) -> list[dict]:
    """Cartes d'offres du JSON « mosaic » embarqué dans la page de résultats Indeed."""
    m = re.search(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*(\{.*?\});\s*window',
                  html or "", re.S)
    if not m:
        return []
    try:
        return json.loads(m.group(1))["metaData"]["mosaicProviderJobCardsModel"]["results"]
    except (ValueError, KeyError, TypeError):
        return []


def _query_terms(q: str) -> list[str]:
    """Mots significatifs d'une requête Indeed (sans guillemets ni opérateurs), en minuscules et sans accents."""
    words = re.findall(r"[\w+#.-]+", _fold(q))
    return [w for w in words if len(w) >= 2 and w not in {"or", "and", "not", "ou", "et", "de", "des", "en", "le", "la"}]


def _fold(text: str) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", text or "") if unicodedata.category(c) != "Mn").lower()


@app.get("/search")
def search(q: str = "", days: int = Query(3, ge=1, le=14),
           limit: int = Query(SEARCH_MAX, alias="max", ge=1)) -> dict:
    """Une seule page de résultats Indeed (pas de pagination), offres publiées depuis `days` jours, triées de la
    plus récente à la plus ancienne, plafonnées à 3. Filtre sur le titre : métiers de l'IA si `q` est vide
    (requête IA par défaut), sinon au moins un des mots de `q`. Les offres ne sont pas extraites ici : chacune
    est ensuite traitée séparément par /extract."""
    limit = min(limit, SEARCH_MAX)
    default_query = not q.strip()
    q = DEFAULT_AI_QUERY if default_query else q.strip()
    terms = _query_terms(q)
    # Tri Indeed par pertinence (sort=date renvoie toutes les offres récentes, mots-clés quasi ignorés) ;
    # les offres pertinentes sont ensuite triées ici de la plus récente à la plus ancienne.
    url = f"https://{INDEED_DOMAIN}/jobs?" + urlencode({"q": q, "fromage": days})
    try:
        result = fetch_with_escalation(url, is_usable=lambda page: bool(_indeed_cards(page.html_content))
                                       or "jobsearch-NoResult" in (page.html_content or ""))
    except FetchError as e:
        raise bad_gateway("Échec du chargement de la page de recherche Indeed", url, attempts=e.attempts)
    except Exception as e:
        log.exception("Erreur inattendue pendant la recherche %s", url)
        raise bad_gateway(f"Erreur inattendue pendant la recherche : {type(e).__name__}: {e}", url)

    cards = _indeed_cards(result.page.html_content)
    cutoff_ms = (time.time() - days * 86400) * 1000
    offers, seen = [], set()
    for c in sorted(cards, key=lambda c: c.get("pubDate") or 0, reverse=True):
        key, title = c.get("jobkey"), (c.get("title") or c.get("displayTitle") or "").strip()
        if not key or key in seen or (c.get("pubDate") and c["pubDate"] < cutoff_ms):
            continue
        if default_query and (not AI_TITLE_RE.search(title) or NON_TECH_TITLE_RE.search(title)):
            continue
        if not default_query:
            present = [bool(re.search(r"(?<!\w)" + re.escape(t) + r"(?!\w)", _fold(title))) for t in terms]
            # « Data Engineer » : tous les mots ; « MLOps OR "Data Scientist" » : au moins un.
            if not (any(present) if re.search(r"\bor\b", q, re.I) else all(present)):
                continue
        seen.add(key)
        offers.append({"url": f"https://{INDEED_DOMAIN}/viewjob?jk={key}", "job_title": title,
                       "company": c.get("company", ""), "location": c.get("formattedLocation", ""),
                       "posted": c.get("formattedRelativeTime", ""), "pub_date": c.get("pubDate")})
    log.info("Recherche Indeed « %s » (%s, %d j) : %d cartes, %d offre(s) retenue(s)", q, result.method, days,
             len(cards), len(offers))
    return {"search_url": url, "q": q, "default_query": default_query, "days": days, "cards_found": len(cards),
            "matching": len(offers), "offers": offers[:limit]}


# Endpoint synchrone : FastAPI l'exécute dans un thread séparé, ce qui permet
# d'utiliser l'API Playwright synchrone de Scrapling sans bloquer le serveur.
@app.post("/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest) -> ExtractResponse:
    url = req.url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(status_code=422, detail={"error": "URL invalide : http(s)://... attendu", "url": url})

    try:
        result = fetch_with_escalation(url, is_usable=has_job_content)
    except FetchError as e:
        raise bad_gateway("Échec du chargement de la page (bloquée, vide ou sans offre exploitable)",
                          url, attempts=e.attempts)
    except Exception as e:
        log.exception("Erreur inattendue pendant le chargement de %s", url)
        raise bad_gateway(f"Erreur inattendue pendant le chargement : {type(e).__name__}: {e}", url)

    try:
        job = extract_job(result.page)
    except Exception as e:
        log.exception("Erreur inattendue pendant l'extraction de %s", url)
        raise bad_gateway(f"Erreur inattendue pendant l'extraction : {type(e).__name__}: {e}", url,
                          fetch_method=result.method)

    if not job["title"] or not job["description"]:
        raise bad_gateway("Page chargée mais titre ou description introuvable", url, fetch_method=result.method)

    log.info("Extrait via %s (%s) : %s", result.method, job["extraction"], job["title"])
    return ExtractResponse(
        job_title=job["title"],
        company=job["company"],
        location=job["location"],
        description=job["description"],
        technologies=extract_technologies(job["description"]),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
