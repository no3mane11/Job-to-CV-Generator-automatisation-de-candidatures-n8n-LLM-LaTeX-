# Compilateur LaTeX -> PDF pour n8n (réseau Docker interne uniquement).
# TeX Live Debian, limité aux collections nécessaires au CV :
#   latex-base        : fontenc, graphicx, hyperref, helvet (psnfss)
#   latex-recommended : geometry, xcolor, parskip, enumitem
#   latex-extra       : titlesec
#   fonts-recommended : polices Helvetica T1 (uhvr8t) pour helvet
#   pictures          : tikz / pgf
FROM python:3.12-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        texlive-latex-base \
        texlive-latex-recommended \
        texlive-latex-extra \
        texlive-fonts-recommended \
        texlive-pictures \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.30.*"

WORKDIR /app
COPY compiler_server.py photo.jpg ./

RUN useradd --create-home --uid 1000 latex && chown -R latex:latex /app
USER latex

EXPOSE 8080
CMD ["uvicorn", "compiler_server:app", "--host", "0.0.0.0", "--port", "8080"]
