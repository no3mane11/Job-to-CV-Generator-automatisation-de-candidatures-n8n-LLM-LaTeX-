# Exemple d'exécution (depuis job-automation/src) :
#   python job_collector.py --site indeed --query "AI Engineer" --location "Casablanca" --max 5
#   python job_collector.py --site stagiaires --query "Software Engineer" --max 5
#   python job_collector.py --site indeed --query "Data Scientist" --max 15 --allow-more-than-10
#
# Sortie réelle (23/09/2026, --max 2, 55s au total) :
#   🔎 Recherche indeed : https://ma.indeed.com/jobs?q=AI+Engineer&l=Casablanca
#   → 2 offre(s) retenue(s) sur 16 trouvée(s) (DynamicFetcher)
#   [1/2] https://ma.indeed.com/viewjob?jk=ff6b3ada366eedc8
#         pause 5.2s avant Fetcher
#         pause 4.0s avant DynamicFetcher
#         ✅ AI Engineer — DynamicFetcher, JSON-LD, 2623 caractères
#   [2/2] https://ma.indeed.com/viewjob?jk=d52b5272f979ff88
#         ...
#         ✅ Ingénieur en Intelligence Artificielle — DynamicFetcher, JSON-LD, 3350 caractères
#   (puis le contenu de output/collection_report.md)
#
# Code de sortie : 0 si au moins une offre est extraite, 1 sinon.
#
# Un run = une recherche = un lot borné : une seule page de résultats, pas de
# pagination, pas de boucle, aucune saisie clavier. Les offres sont extraites
# avec collect_scrapling.collect() et sauvegardées dans data/<slug>.txt et
# output/<slug>_meta.json.

import argparse
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional
from urllib.parse import quote, urlencode

from collect_scrapling import OUTPUT_DIR, FetchError, collect, fetch_with_escalation, slugify

DEFAULT_MAX = 5
SOFT_MAX = 10
PAUSE_RANGE_S = (3.0, 6.0)
INDEED_DOMAIN = "ma.indeed.com"


# ---------- Sites ----------

def _indeed_search_url(query: str, location: str) -> str:
    params = {"q": query}
    if location:
        params["l"] = location
    return f"https://{INDEED_DOMAIN}/jobs?{urlencode(params)}"


def _indeed_links(page) -> list[str]:
    keys = [el.attrib.get("data-jk") for el in page.css("[data-jk]")]
    if not any(keys):
        # Repli : les cartes sont aussi décrites dans le JSON "mosaic" embarqué.
        keys = re.findall(r'"jobkey"\s*:\s*"([0-9a-f]{16})"', page.html_content or "")
    return [f"https://{INDEED_DOMAIN}/viewjob?jk={k}" for k in keys if k]


def _stagiaires_search_url(query: str, location: str) -> str:
    # Le site annonce ?search= dans son JSON-LD, mais seul ?q= filtre réellement.
    return f"https://stagiaires.ma/stage-emploi-maroc?q={quote(query)}"


def _stagiaires_links(page) -> list[str]:
    # Les résultats filtrés sont chargés en JS depuis api.stagiaires.ma, et les
    # cartes n'ont pas de <a href>. Le JSON-LD rendu côté serveur ignore la
    # recherche : on lit donc les réponses XHR capturées par le navigateur.
    responses = [r for r in getattr(page, "captured_xhr", []) if "annonce" in r.url]
    search_responses = [r for r in responses if "/titre/" in r.url or "q=" in r.url]
    ids = []
    for resp in search_responses or responses:
        try:
            data = resp.json()
        except Exception:
            continue
        for node in _iter_dicts(data):
            if node.get("id") and any(k in node for k in ("titre", "title", "intitule")):
                ids.append(str(node["id"]))
    return [f"https://stagiaires.ma/stage-emploi-maroc/{i}" for i in ids]


def _iter_dicts(data):
    if isinstance(data, dict):
        yield data
        for v in data.values():
            yield from _iter_dicts(v)
    elif isinstance(data, list):
        for v in data:
            yield from _iter_dicts(v)


@dataclass(frozen=True)
class Site:
    build_search_url: Callable[[str, str], str]
    extract_links: Callable[[object], list[str]]
    capture_xhr: Optional[str] = None
    supports_location: bool = True


SITES = {
    "indeed": Site(_indeed_search_url, _indeed_links),
    "stagiaires": Site(_stagiaires_search_url, _stagiaires_links,
                       capture_xhr=r"api\.stagiaires\.ma", supports_location=False),
}


def unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def offer_id(url: str) -> str:
    m = re.search(r"jk=([0-9a-f]+)", url) or re.search(r"/(\d+)/?$", url)
    return m.group(1) if m else slugify(url)[-12:]


# ---------- Rapport ----------

@dataclass
class OfferResult:
    url: str
    ok: bool = False
    title: str = ""
    fetch_method: str = ""
    jsonld_found: Optional[bool] = None
    text_length: int = 0
    txt_path: str = ""
    error: str = ""


@dataclass
class Report:
    site: str
    query: str
    location: str
    requested: int
    command: str
    started_at: datetime = field(default_factory=datetime.now)
    search_url: str = ""
    search_method: str = ""
    search_attempts: list[str] = field(default_factory=list)
    links_found: int = 0
    offers: list[OfferResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    aborted: str = ""

    def to_markdown(self) -> str:
        ok = sum(o.ok for o in self.offers)
        duration = (datetime.now() - self.started_at).total_seconds()
        lines = [
            "# Rapport de collecte",
            "",
            f"- **Date** : {self.started_at:%Y-%m-%d %H:%M:%S} (durée {duration:.0f}s)",
            f"- **Commande** : `{self.command}`",
            f"- **Site** : {self.site} — **Requête** : {self.query}"
            + (f" — **Lieu** : {self.location}" if self.location else ""),
            f"- **URL de recherche** : {self.search_url or '—'}",
            f"- **Page de résultats chargée via** : {self.search_method or 'échec'}",
            f"- **Offres trouvées sur la page** : {self.links_found}",
            f"- **Offres demandées (--max)** : {self.requested}",
            f"- **Offres traitées** : {len(self.offers)} — **succès** : {ok} — **échecs** : {len(self.offers) - ok}",
        ]
        if self.aborted:
            lines += ["", f"> ⛔ **Arrêt** : {self.aborted}"]

        lines += ["", "## Offres", ""]
        if self.offers:
            lines += [
                "| # | Titre | Méthode de fetch | JSON-LD | Longueur texte | Statut | Fichier |",
                "|---|---|---|---|---|---|---|",
            ]
            for i, o in enumerate(self.offers, 1):
                jsonld = "—" if o.jsonld_found is None else ("oui" if o.jsonld_found else "non")
                title = (o.title or "?").replace("|", "/")
                status = "✅ succès" if o.ok else "❌ échec"
                lines.append(
                    f"| {i} | [{title}]({o.url}) | {o.fetch_method or '—'} | {jsonld} "
                    f"| {o.text_length} | {status} | {o.txt_path or '—'} |"
                )
        else:
            lines.append("_Aucune offre traitée._")

        lines += ["", "## Chargement de la page de résultats", ""]
        lines += [f"- {a}" for a in self.search_attempts] or ["- —"]

        lines += ["", "## Erreurs", ""]
        lines += [f"- {e}" for e in self.errors] or ["_Aucune erreur._"]
        return "\n".join(lines) + "\n"


# ---------- Exécution ----------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recherche des offres puis extrait chacune d'elles.")
    parser.add_argument("--site", required=True, choices=sorted(SITES))
    parser.add_argument("--query", required=True, help='Mot-clé, ex. "AI Engineer"')
    parser.add_argument("--location", default="", help='Ville, ex. "Casablanca" (indeed uniquement)')
    parser.add_argument("--max", type=int, default=DEFAULT_MAX,
                        help=f"Nombre max d'offres (défaut {DEFAULT_MAX}, plafond {SOFT_MAX})")
    parser.add_argument("--allow-more-than-10", action="store_true",
                        help=f"Autorise explicitement --max > {SOFT_MAX}")
    args = parser.parse_args(argv)
    if args.max < 1:
        parser.error("--max doit être >= 1")
    if args.max > SOFT_MAX and not args.allow_more_than_10:
        parser.error(f"--max {args.max} dépasse {SOFT_MAX} : ajoutez --allow-more-than-10 pour le confirmer")
    return args


def polite_pause(label: str) -> None:
    delay = random.uniform(*PAUSE_RANGE_S)
    print(f"      pause {delay:.1f}s avant {label}", flush=True)
    time.sleep(delay)


def run(args: argparse.Namespace, report: Report) -> None:
    """Remplit `report` au fil de l'eau, pour qu'un arrêt en cours garde le partiel."""
    site = SITES[args.site]
    if args.location and not site.supports_location:
        report.errors.append(f"--location ignoré : non pris en charge pour {args.site}")

    # 1-2) Page de résultats avec escalade ; "utilisable" = au moins un lien d'offre.
    report.search_url = site.build_search_url(args.query, args.location)
    print(f"🔎 Recherche {args.site} : {report.search_url}", flush=True)
    try:
        result = fetch_with_escalation(
            report.search_url,
            is_usable=lambda page: bool(site.extract_links(page)),
            capture_xhr=site.capture_xhr,
        )
    except FetchError as e:
        report.search_attempts = e.attempts
        report.aborted = "impossible de charger une page de résultats exploitable (aucune offre extraite)."
        report.errors.append(str(e))
        return

    # 3) Liens individuels, plafonnés à --max, sans pagination.
    report.search_method = result.method
    report.search_attempts = result.attempts
    links = unique(site.extract_links(result.page))
    report.links_found = len(links)
    links = links[:args.max]
    print(f"→ {len(links)} offre(s) retenue(s) sur {report.links_found} trouvée(s) ({result.method})", flush=True)
    if len(links) < args.max:
        report.errors.append(f"Seulement {len(links)} offre(s) trouvée(s) pour {args.max} demandée(s).")

    # 4) Détail de chaque offre, avec pause aléatoire avant chaque requête.
    for i, url in enumerate(links, 1):
        print(f"[{i}/{len(links)}] {url}", flush=True)
        offer = OfferResult(url=url)
        report.offers.append(offer)
        try:
            # Préfixe site + id : évite les collisions entre offres au même titre.
            info = collect(url, slug_prefix=f"{args.site}-{offer_id(url)}", before_attempt=polite_pause)
        except FetchError as e:
            offer.error = str(e)
            report.errors.append(f"Offre {i} ({url}) : " + " | ".join(e.attempts))
            print("      ❌ échec", flush=True)
            continue
        except Exception as e:
            offer.error = f"{type(e).__name__}: {e}"
            report.errors.append(f"Offre {i} ({url}) : erreur inattendue {offer.error}")
            print(f"      ❌ {offer.error}", flush=True)
            continue

        report.errors += [f"Offre {i} (récupérée ensuite) — {a}" for a in info["attempts"] if ": OK" not in a]
        offer.ok = True
        offer.title = info["title"]
        offer.fetch_method = info["fetch_method"]
        offer.jsonld_found = info["jsonld_found"]
        offer.text_length = len(info["description"])
        offer.txt_path = f"data/{info['txt_path'].name}"
        print(f"      ✅ {offer.title} — {offer.fetch_method}, "
              f"{'JSON-LD' if offer.jsonld_found else 'CSS'}, {offer.text_length} caractères", flush=True)


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    command = "python job_collector.py " + " ".join(f'"{a}"' if " " in a else a for a in (argv or sys.argv[1:]))

    report = Report(args.site, args.query, args.location, args.max, command)
    try:
        run(args, report)
    except KeyboardInterrupt:
        report.aborted = "interrompu par l'utilisateur (Ctrl+C) — rapport partiel."
    except Exception as e:
        report.aborted = f"erreur bloquante : {type(e).__name__}: {e}"
        report.errors.append(report.aborted)

    # 5) Rapport : écrit puis affiché.
    OUTPUT_DIR.mkdir(exist_ok=True)
    report_path = OUTPUT_DIR / "collection_report.md"
    markdown = report.to_markdown()
    report_path.write_text(markdown, encoding="utf-8")
    print("\n" + "=" * 70 + f"\n{report_path}\n" + "=" * 70)
    print(markdown)
    return 0 if any(o.ok for o in report.offers) else 1


if __name__ == "__main__":
    sys.exit(main())
