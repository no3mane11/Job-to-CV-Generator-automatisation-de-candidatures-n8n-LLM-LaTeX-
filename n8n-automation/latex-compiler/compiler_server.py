# Serveur de compilation LaTeX -> PDF, appelé par n8n via le réseau Docker interne :
#   POST http://latex-compiler:8080/compile   body {"latex": "<code LaTeX complet>"}
#   -> 200 application/pdf (le PDF)
#   -> 422 text/plain (log pdflatex complet) si la compilation échoue
#
# Le .tex est écrit dans /app, à côté de photo.jpg, pour que \includegraphics{photo.jpg}
# le trouve. pdflatex tourne en mode non interactif, sans shell-escape, en deux passes.
#
# Lancé dans le conteneur par : uvicorn compiler_server:app --host 0.0.0.0 --port 8080

import subprocess
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

WORKDIR = Path(__file__).resolve().parent
PASS_TIMEOUT_S = 60
AUX_SUFFIXES = (".tex", ".pdf", ".log", ".aux", ".out", ".toc")

app = FastAPI(title="latex-compiler")


class CompileRequest(BaseModel):
    latex: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# Synchrone : FastAPI l'exécute dans un thread, plusieurs compilations peuvent tourner
# en parallèle grâce aux noms de fichiers uniques.
@app.post("/compile")
def compile_latex(req: CompileRequest):
    if not req.latex.strip():
        raise HTTPException(status_code=400, detail="Champ 'latex' vide")

    job = f"job_{uuid.uuid4().hex}"
    tex = WORKDIR / f"{job}.tex"
    tex.write_text(req.latex, encoding="utf-8")
    log = ""
    try:
        for pass_no in (1, 2):
            try:
                proc = subprocess.run(
                    ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape",
                     f"-jobname={job}", tex.name],
                    cwd=WORKDIR, capture_output=True, timeout=PASS_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired:
                return PlainTextResponse(f"pdflatex : délai de {PASS_TIMEOUT_S}s dépassé (passe {pass_no})",
                                         status_code=422)
            log_file = WORKDIR / f"{job}.log"
            log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() \
                else proc.stdout.decode("utf-8", "replace")
            if proc.returncode != 0:
                return PlainTextResponse(log, status_code=422)

        pdf = WORKDIR / f"{job}.pdf"
        if not pdf.exists():
            return PlainTextResponse(log or "pdflatex n'a produit aucun PDF", status_code=422)
        return Response(content=pdf.read_bytes(), media_type="application/pdf",
                        headers={"Content-Disposition": 'attachment; filename="cv.pdf"'})
    finally:
        for suffix in AUX_SUFFIXES:
            (WORKDIR / f"{job}{suffix}").unlink(missing_ok=True)
