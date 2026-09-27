# Interface web locale : http://127.0.0.1:8000/ui
# Lance les workflows n8n sans URL à encoder (une offre collée, ou une recherche Indeed paramétrable)
# et suit leur exécution étape par étape via l'API REST n8n.
#
# La clé N8N_API_KEY est lue dans l'environnement (ou le registre Windows HKCU\Environment) et n'est
# jamais renvoyée au navigateur. Un seul traitement à la fois, pour ne pas saturer Indeed ni Gemini.

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

N8N_URL = "http://localhost:5678"
WORKFLOW_NAMES = {
    "offer": "Job to CV Generator",
    "offer_demo": "Job to CV Generator (DEMO)",
    "search": "Indeed IA → CV (lot)",
}
UI_FILE = Path(__file__).resolve().parent / "ui" / "index.html"

# Libellés affichés pour chaque nœud (les nœuds absents de la liste ne sont pas montrés).
STEP_LABELS = {
    "Webhook": "Réception de la demande",
    "Déclencheur": "Réception de la demande",
    "Rechercher offres Indeed": "Recherche des offres sur Indeed",
    "Offres trouvées": "Sélection des offres",
    "Telegram aucune offre": "Envoi « aucune offre » sur Telegram",
    "Générer CV + lettre": "Génération CV + lettre (offre par offre)",
    "Extraire offre": "Extraction de l'offre",
    "Lire CV": "Lecture du CV source",
    "Lire Projets": "Lecture du catalogue de projets",
    "Mode de travail": "Détection du mode de travail",
    "Gemini": "Génération CV + lettre (Gemini)",
    "Groq Fallback": "Génération de secours (Groq)",
    "Code": "Contrôle ATS et anti-invention",
    "Préparer envoi": "Préparation de l'envoi",
    "Telegram récapitulatif": "Envoi du récapitulatif Telegram",
    "Telegram offre": "Envoi des lettres sur Telegram",
    "Telegram": "Envoi de la lettre sur Telegram",
    "Compiler PDF": "Compilation du CV en PDF",
    "Telegram CV PDF": "Envoi du CV PDF sur Telegram",
    "Respond to Webhook": "Fin du traitement",
}

router = APIRouter()
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
_workflow_ids: dict[str, str] = {}


class OfferRequest(BaseModel):
    url: str
    mode: Literal["honnete", "demo"] = "honnete"


class SearchRequest(BaseModel):
    days: Literal[1, 3, 7, 14] = 3
    max: int = Field(3, ge=1, le=3)
    mode: Literal["honnete", "demo"] = "honnete"
    q: str = Field("", max_length=300)


def _api_key() -> str:
    key = os.environ.get("N8N_API_KEY")
    if not key and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, "N8N_API_KEY")[0]
        except OSError:
            key = None
    if not key:
        raise HTTPException(500, detail={"error": "N8N_API_KEY introuvable dans l'environnement"})
    return key


def _n8n_api(path: str) -> dict:
    req = urllib.request.Request(f"{N8N_URL}/api/v1{path}", headers={"X-N8N-API-KEY": _api_key(),
                                                                     "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise HTTPException(502, detail={"error": f"API n8n : HTTP {e.code}"})
    except urllib.error.URLError as e:
        raise HTTPException(502, detail={"error": f"n8n injoignable ({e.reason}) : Docker est-il lancé ?"})


def _workflow_id(kind: str) -> str:
    if kind not in _workflow_ids:
        name = WORKFLOW_NAMES[kind]
        match = [w for w in _n8n_api("/workflows?limit=250").get("data", []) if w.get("name") == name]
        if len(match) != 1:
            raise HTTPException(500, detail={"error": f"Workflow « {name} » introuvable dans n8n"})
        _workflow_ids[kind] = match[0]["id"]
    return _workflow_ids[kind]


def _call_webhook(job: dict, url: str) -> None:
    """Appelle le webhook n8n (bloquant, dans un thread) et note le résultat HTTP."""
    try:
        with urllib.request.urlopen(url, timeout=900) as r:
            job["webhook"] = {"ok": True, "status": r.status}
    except urllib.error.HTTPError as e:
        job["webhook"] = {"ok": False, "status": e.code, "error": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:  # n8n arrêté, délai dépassé...
        job["webhook"] = {"ok": False, "status": None, "error": f"{type(e).__name__}: {e}"}


def _start(kind: str, webhook_url: str, summary: str) -> dict:
    workflow_id = _workflow_id(kind)
    with LOCK:
        if any(j["state"] == "running" for j in JOBS.values()):
            raise HTTPException(409, detail={"error": "Un traitement est déjà en cours : attends qu'il se termine."})
        job = {"id": uuid.uuid4().hex[:8], "kind": kind, "summary": summary, "workflow_id": workflow_id,
               "started": time.time(), "state": "running", "execution_id": None, "webhook": None}
        JOBS[job["id"]] = job
    threading.Thread(target=_call_webhook, args=(job, webhook_url), daemon=True).start()
    return {"job_id": job["id"]}


@router.get("/ui", include_in_schema=False)
def ui() -> FileResponse:
    return FileResponse(UI_FILE, media_type="text/html; charset=utf-8")


@router.post("/ui/api/offer")
def run_offer(req: OfferRequest) -> dict:
    url = req.url.strip()
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise HTTPException(422, detail={"error": "Colle un lien complet d'offre (https://...)"})
    kind = "offer_demo" if req.mode == "demo" else "offer"
    path = "generate-cv-demo" if req.mode == "demo" else "generate-cv"
    webhook = f"{N8N_URL}/webhook/{path}?" + urllib.parse.urlencode({"url": url})
    return _start(kind, webhook, f"Offre : {url}")


@router.post("/ui/api/search")
def run_search(req: SearchRequest) -> dict:
    params = {"days": req.days, "max": req.max}
    if req.mode == "demo":
        params["mode"] = "demo"
    if req.q.strip():
        params["q"] = req.q.strip()
    webhook = f"{N8N_URL}/webhook/search-ai-jobs?" + urllib.parse.urlencode(params)
    return _start("search", webhook, f"Recherche Indeed : {req.days} j, {req.max} offre(s) max")


def _find_execution(job: dict) -> dict | None:
    if not job["execution_id"]:
        # Par défaut l'API n8n n'inclut pas les exécutions en cours : on les demande explicitement.
        listing = [e for status in ("running", "")
                   for e in _n8n_api(f"/executions?workflowId={job['workflow_id']}&limit=10"
                                     + (f"&status={status}" if status else "")).get("data", [])]
        started = [e for e in listing
                   if datetime.fromisoformat(e["startedAt"].replace("Z", "+00:00")).timestamp() >= job["started"] - 5]
        if not started:
            return None
        job["execution_id"] = min(started, key=lambda e: e["startedAt"])["id"]
    return _n8n_api(f"/executions/{job['execution_id']}?includeData=true")


def _steps(run_data: dict, running: bool) -> list[dict]:
    steps = []
    for name, runs in run_data.items():
        if name not in STEP_LABELS or not runs:
            continue
        failed = next((r["error"] for r in runs if r.get("error")), None)
        steps.append({"node": name, "label": STEP_LABELS[name], "start": min(r.get("startTime", 0) for r in runs),
                      "runs": len(runs), "status": "error" if failed else "success",
                      "error": (failed or {}).get("message", "") if failed else ""})
    steps.sort(key=lambda s: s["start"])
    if running and steps and steps[-1]["status"] == "success":
        steps[-1]["status"] = "running"
    return steps


@router.get("/ui/api/jobs/{job_id}")
def job_status(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, detail={"error": "Traitement inconnu (l'API a peut-être redémarré)"})
    out = {"id": job["id"], "summary": job["summary"], "elapsed": round(time.time() - job["started"]),
           "state": job["state"], "steps": [], "message": "", "progress": None, "execution_id": job["execution_id"]}
    webhook = job["webhook"]
    if webhook and not webhook["ok"] and webhook["status"] in (None, 404):
        job["state"] = out["state"] = "error"
        out["message"] = f"Impossible de lancer le workflow : {webhook.get('error') or 'webhook introuvable'}"
        return out

    execution = _find_execution(job)
    if not execution:
        out["message"] = "Démarrage du workflow…"
        return out
    out["execution_id"] = job["execution_id"]
    result = (execution.get("data") or {}).get("resultData") or {}
    run_data = result.get("runData") or {}
    status = execution.get("status")
    running = status in ("running", "new", "waiting")
    out["steps"] = _steps(run_data, running)

    if job["kind"] == "search":
        found = run_data.get("Offres trouvées", [{}])[0].get("data", {}).get("main", [[]])[0]
        total = 0 if not found or found[0]["json"].get("none") else len(found)
        done = len(run_data.get("Résultat offre", []))
        if total:
            out["progress"] = {"done": done, "total": total}
        if "Telegram aucune offre" in run_data:
            out["message"] = found[0]["json"].get("text", "Aucune offre trouvée.") if found else "Aucune offre trouvée."

    if running:
        return out
    if status == "success":
        job["state"] = out["state"] = "success"
        out["message"] = out["message"] or "Traitement terminé avec succès : vérifie Telegram."
    else:
        job["state"] = out["state"] = "error"
        error = result.get("error") or {}
        out["message"] = (f"Échec au nœud « {result.get('lastNodeExecuted', '?')} » : "
                          f"{error.get('message', status)}")
    return out
