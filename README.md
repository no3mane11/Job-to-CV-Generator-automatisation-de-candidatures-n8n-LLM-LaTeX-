# Job to CV Generator — automatisation de candidatures (n8n + LLM + LaTeX)

Système local qui, à partir d'une offre d'emploi (lien collé) ou d'une recherche Indeed automatique,
génère **un CV LaTeX optimisé ATS** et **une lettre de motivation personnalisée**, contrôle
automatiquement les inventions/surestimations, calcule un **score de correspondance des mots-clés**,
compile le CV en PDF et envoie le tout sur **Telegram**.

> Ce README est la référence du projet. Il est écrit pour qu'un humain **ou un autre agent/LLM**
> puisse comprendre, démarrer, maintenir et faire évoluer le système sans autre contexte.
> État décrit : **27/09/2026**.

---

## Sommaire

1. [Démarrage rapide (PC déjà installé)](#1-démarrage-rapide-pc-déjà-installé)
2. [Vue d'ensemble et architecture](#2-vue-densemble-et-architecture)
3. [Arborescence des fichiers](#3-arborescence-des-fichiers)
4. [Composants en détail](#4-composants-en-détail)
5. [Workflows n8n : chaque nœud expliqué](#5-workflows-n8n--chaque-nœud-expliqué)
6. [Prompt, format de sortie et contrôles du nœud Code](#6-prompt-format-de-sortie-et-contrôles-du-nœud-code)
7. [Fonctionnalités actuelles et limites connues](#7-fonctionnalités-actuelles-et-limites-connues)
8. [Installation complète sur un nouveau PC](#8-installation-complète-sur-un-nouveau-pc)
9. [Faire évoluer le système](#9-faire-évoluer-le-système)
10. [Règles pour modifier les workflows (agents/LLM)](#10-règles-pour-modifier-les-workflows-agentsllm)
11. [Dépannage](#11-dépannage)
12. [Sécurité et éthique](#12-sécurité-et-éthique)

---

## 1. Démarrage rapide (PC déjà installé)

À faire à chaque redémarrage du PC (≈ 2 minutes) :

1. **Lancer Docker Desktop** (attendre que l'icône indique « Engine running »).
2. **Démarrer n8n et le compilateur LaTeX** (PowerShell) :
   ```powershell
   cd F:\DellAlienware\REEL_World_Project\Automatisation\Automatiser_postule_candidature\n8n-automation
   docker compose up -d
   ```
3. **Démarrer l'API locale** (dans un terminal à laisser ouvert) :
   ```powershell
   cd F:\DellAlienware\REEL_World_Project\Automatisation\Automatiser_postule_candidature\job-automation\src
   python -m uvicorn api_server:app --host 127.0.0.1 --port 8000
   ```
4. **Ouvrir l'interface** : <http://127.0.0.1:8000/ui> (sans point ni slash à la fin).
   - Onglet **Une offre** : coller le lien d'une offre → *Générer le CV et la lettre*.
   - Onglet **Recherche Indeed** : choisir la date de publication (24 h / 3 / 7 / 14 jours), le nombre
     d'offres (1–3), les mots-clés (vide = métiers de l'IA) et le mode → *Rechercher et traiter*.
   - La page affiche les étapes en direct (✅ / ⏳ / ❌) puis « Traitement terminé : vérifie Telegram ».
5. **Vérifier le résultat dans Telegram** (message avec score ATS + lettre, puis CV PDF).

Vérifications rapides : n8n <http://localhost:5678> · API <http://127.0.0.1:8000/health> → `{"status":"ok"}`.

---

## 2. Vue d'ensemble et architecture

```
                         ┌──────────────────────────────────────────────────────────────┐
 Navigateur              │  PC Windows (hôte)                                           │
 http://127.0.0.1:8000/ui│                                                              │
        │                │  API FastAPI  127.0.0.1:8000  (job-automation/src)           │
        └───────────────►│   /ui  /ui/api/*   /extract  /cv  /projects  /search  /health│
                         │        │  ▲                          ▲                       │
                         │        │  │ API REST n8n (suivi)     │ host.docker.internal  │
                         │        ▼  │                          │ :8000                 │
                         │  ┌────────────── Docker ─────────────┴───────────────────┐   │
                         │  │ n8n  127.0.0.1:5678  (volume n8n_data)                  │   │
                         │  │  • Job to CV Generator          /webhook/generate-cv     │   │
                         │  │  • Job to CV Generator (DEMO)   /webhook/generate-cv-demo│   │
                         │  │  • Indeed IA → CV (lot)         /webhook/search-ai-jobs  │   │
                         │  │        │ http://latex-compiler:8080 (réseau interne)     │   │
                         │  │ latex-compiler (TeX Live + FastAPI) POST /compile → PDF │   │
                         │  └─────────────────────────────────────────────────────────┘   │
                         └──────────────────────────────────────────────────────────────┘
        Services externes : Indeed (scraping) · Gemini API · Groq API (secours) · Telegram Bot API
```

### Flux « une offre » (workflow *Job to CV Generator*)

```
Webhook(url) ─┬─► Extraire offre (API /extract) ─► Merge ─► Merge Projets ─► Mode de travail ─► Gemini ──(ok)──► Code ─► Mode lot ?
              ├─► Lire CV (API /cv) ──────────────┘          ▲                                   │(erreur)          ▲       │vrai  ► Réponse lot (JSON)
              └─► Lire Projets (API /projects) ──────────────┘                                   └► Groq Fallback ──┘       │faux  ► Telegram (lettre+score)
                                                                                                                            │      ► Compiler PDF ► Telegram CV PDF ► Respond to Webhook
```

### Flux « recherche » (workflow *Indeed IA → CV (lot)*)

```
Déclencheur(days,max,q,mode) ► Rechercher offres Indeed (API /search) ► Offres trouvées ► Aucune offre ?
   ├─ vrai ► Telegram aucune offre
   └─ faux ► Une offre à la fois (boucle, lot de 1) ⇄ Générer CV + lettre (webhook générateur, deliver=batch) ► Résultat offre
               └─ terminé ► Préparer envoi ► Telegram récapitulatif (1 fois)
                                          └► Telegram offre (1 par offre) ► CV disponible ? ► Compiler PDF ► Telegram CV PDF
```

Principe clé : **une seule offre par appel au LLM** (pas de saturation du contexte), les offres
d'un lot sont traitées séquentiellement, puis tout est envoyé à la fin.

---

## 3. Arborescence des fichiers

```
Automatiser_postule_candidature/
├── README.md                          ← ce document
├── job-automation/                    ← API locale Python (tourne sur l'hôte Windows)
│   ├── requirements.txt               ← dépendances (fastapi, uvicorn, pydantic, scrapling[fetchers])
│   ├── data/
│   │   ├── cv_source.tex              ← CV source officiel (LaTeX, 1 page) — lu à chaque appel de /cv
│   │   └── projects_catalog.json      ← 8 projets RÉELS du candidat (seule source autorisée de projets)
│   └── src/
│       ├── api_server.py              ← FastAPI : /extract /cv /projects /search /health + inclut l'UI
│       ├── ui_routes.py               ← routes de l'interface : /ui, /ui/api/offer|search|jobs/{id}
│       ├── ui/index.html              ← interface web (HTML/CSS/JS autonome, sans dépendance)
│       ├── collect_scrapling.py       ← récupération d'une offre (escalade Fetcher→Dynamic→Stealthy, JSON-LD/CSS)
│       ├── analyzer.py                ← extraction de technologies (liste de mots-clés) utilisée par /extract
│       └── job_collector.py           ← outil CLI autonome : recherche + extraction en lot (Indeed, stagiaires.ma)
├── n8n-automation/                    ← conteneurs Docker
│   ├── docker-compose.yml             ← services n8n (127.0.0.1:5678) + latex-compiler (réseau interne)
│   ├── .env                           ← GENERIC_TIMEZONE=Africa/Casablanca (non versionné)
│   ├── .gitignore
│   ├── README.md                      ← mémo n8n (démarrer/arrêter)
│   ├── latex-compiler/                ← image TeX Live + FastAPI : POST /compile {latex} → PDF
│   │   ├── Dockerfile
│   │   ├── compiler_server.py
│   │   └── photo.jpg                  ← n'est plus utilisée par le CV (copiée par le Dockerfile)
│   └── workflows/                     ← EXPORTS des workflows publiés (sans secrets) — pour réinstaller
│       ├── job-to-cv-generator.json
│       ├── job-to-cv-generator-demo.json
│       └── indeed-ia-cv-lot.json
└── _archive/                          ← (local uniquement, ignoré par git) anciens essais : tests
                                          Firecrawl/Scrapling/ReKrute/Google Jobs, offres de test,
                                          anciens outputs, main.py. Non utilisés.
```

Important : **la version qui s'exécute des workflows est celle stockée dans n8n** (volume Docker
`n8n_data`). Les fichiers de `n8n-automation/workflows/` sont des exports : après une modification
dans n8n, ré-exporter pour garder le dépôt à jour (voir §10).

---

## 4. Composants en détail

### 4.1 API locale FastAPI (`job-automation/src/api_server.py`)

Écoute **uniquement** sur `127.0.0.1:8000`, sans authentification (usage local strict). Depuis les
conteneurs, n8n l'appelle via `http://host.docker.internal:8000`.

| Route | Méthode | Rôle | Réponse |
|---|---|---|---|
| `/health` | GET | test de vie | `{"status":"ok"}` |
| `/cv` | GET | contenu de `data/cv_source.tex` (relu à chaque appel) | `{"cv_latex": "..."}` |
| `/projects` | GET | contenu de `data/projects_catalog.json` (relu à chaque appel) | `{"projects":[...]}` |
| `/extract` | POST `{"url"}` | récupère et extrait UNE offre (tout site : JSON-LD `JobPosting`, sinon sélecteurs CSS) | `{job_title, company, location, description, technologies}` ; 502 si échec |
| `/search` | GET `?days=1..14&max=1..3&q=` | 1 page de résultats Indeed (`ma.indeed.com`, tri pertinence, `fromage=days`), filtre les titres, garde les plus récentes | `{search_url, q, default_query, days, cards_found, matching, offers:[{url, job_title, company, location, posted, pub_date}]}` |
| `/ui` + `/ui/api/*` | — | interface web (voir 4.2) | — |

Détails de `/search` :
- `q` vide → requête IA par défaut (`DEFAULT_AI_QUERY` : AI Engineer, MLOps, Machine Learning, Data
  Scientist, LLM, Intelligence Artificielle, Deep Learning, IA) + filtre `AI_TITLE_RE` sur le titre,
  en excluant les métiers non techniques (`NON_TECH_TITLE_RE` : marketing, SEO, community, commercial…).
- `q` renseigné → le titre doit contenir **tous** les mots de `q` (ou **au moins un** si `q` contient `OR`).
- Plafond strict `SEARCH_MAX = 3`. Les offres ne sont **pas** extraites ici (chacune passe ensuite par `/extract`).
- Tri Indeed par **pertinence** (avec `sort=date`, Indeed Maroc ignore quasiment les mots-clés) puis tri
  local par `pubDate` décroissant.

`collect_scrapling.fetch_with_escalation()` : essaie `Fetcher` (HTTP) → `DynamicFetcher` (navigateur)
→ `StealthyFetcher` (navigateur furtif, Cloudflare) jusqu'à obtenir une page non bloquée et exploitable.

### 4.2 Interface web (`ui_routes.py` + `ui/index.html`)

- `POST /ui/api/offer {url, mode: "honnete"|"demo"}` → appelle `/webhook/generate-cv[-demo]?url=...`.
- `POST /ui/api/search {days: 1|3|7|14, max: 1..3, mode, q}` → appelle `/webhook/search-ai-jobs?...`.
- `GET /ui/api/jobs/{id}` → suivi : retrouve l'exécution n8n correspondante via l'**API REST n8n**
  (exécutions `status=running` incluses) et renvoie les étapes (libellés dans `STEP_LABELS`), la
  progression (`k/n` offres) et le message final.
- **Un seul traitement à la fois** (HTTP 409 sinon) pour ne saturer ni Indeed ni Gemini.
- Clé `N8N_API_KEY` lue dans l'environnement (ou registre Windows `HKCU\Environment`), **jamais**
  renvoyée au navigateur. Les jobs sont en mémoire (perdus si l'API redémarre).

### 4.3 n8n (Docker)

- Image `docker.n8n.io/n8nio/n8n:latest` (testé en **2.40.5**), port publié `127.0.0.1:5678` (local uniquement).
- Données (workflows, credentials chiffrés, compte, exécutions) dans le volume Docker **externe** `n8n_data`.
- Réglages des 3 workflows : `executionOrder: v1`, `saveExecutionProgress: true` (suivi en direct).
- **Credentials** (créés dans n8n, référencés par nom dans les workflows) :

| Nom exact | Type n8n | Contenu |
|---|---|---|
| `Gemini API Key` | Header Auth (`httpHeaderAuth`) | en-tête `x-goog-api-key` = clé Google AI Studio |
| `Groq API Key` | Header Auth (`httpHeaderAuth`) | en-tête `Authorization` = `Bearer <clé Groq>` |
| `Telegram Bot` | Telegram API (`telegramApi`) | token du bot (@BotFather) |

- Le **Chat ID Telegram** est écrit en dur dans chaque nœud Telegram (`chatId`), car n8n 2.x bloque
  `$env` dans les nœuds par défaut.

### 4.4 Compilateur LaTeX (`n8n-automation/latex-compiler`)

Conteneur FastAPI + TeX Live (Debian), **sans port publié** : accessible seulement depuis n8n sur
`http://latex-compiler:8080`. `POST /compile {"latex": "..."}` → PDF (HTTP 200) ou log d'erreur (HTTP 422).
Paquets TeX installés : latex-base, latex-recommended, latex-extra, fonts-recommended, pictures
(inclut `draftwatermark` utilisé par le mode DEMO).

### 4.5 Données du candidat

- `cv_source.tex` : CV officiel **1 page**, en anglais. Header sans photo/LinkedIn/GitHub/Portfolio
  (nom, titre, ville, téléphone, email). Mise en page figée : police 10 pt, marges 1 cm / 1,6 cm,
  `\titlespacing*{\section}{0pt}{6pt}{2pt}`, `itemsep=0pt`, `\parskip=1pt`. La section
  *Projects & Portfolio* ne contient que la ligne GitHub : les projets sont **générés à partir du catalogue**.
- `projects_catalog.json` : 8 projets réels ; chacun a `name`, `description`, `technologies`,
  `category_tags` (ex. `ai_ml`, `fullstack`, `backend`, `devops`). C'est la **seule** source de projets.
- Ces deux fichiers forment, avec le CV, le **référentiel réel** utilisé par l'anti-invention (§6).

---

## 5. Workflows n8n : chaque nœud expliqué

### 5.1 « Job to CV Generator » (honnête) — webhook `GET /webhook/generate-cv?url=<offre>[&deliver=batch]`

| # | Nœud | Type | Rôle | Entrées → Sorties |
|---|---|---|---|---|
| 1 | **Webhook** | Webhook v2, `responseMode: responseNode` | reçoit `?url=` (et `deliver=batch` optionnel) | → Extraire offre, Lire CV, Lire Projets (en parallèle) |
| 2 | **Extraire offre** | HTTP Request POST `…:8000/extract` (timeout 180 s) | scraping + extraction de l'offre | → Merge[0] ; sortie `{job_title, company, location, description, technologies}` |
| 3 | **Lire CV** | HTTP GET `…:8000/cv` | CV source LaTeX | → Merge[1] ; `{cv_latex}` |
| 4 | **Merge** | Merge v3, `combineByPosition` | fusionne offre + CV en 1 item | → Merge Projets[0] |
| 5 | **Lire Projets** | HTTP GET `…:8000/projects` | catalogue de projets | → Merge Projets[1] ; `{projects:[...]}` |
| 6 | **Merge Projets** | Merge v3, `combineByPosition` | ajoute `projects` à l'item | → Mode de travail |
| 7 | **Mode de travail** | Code (JS) | détection **déterministe** du mode de travail dans la description, traduit en anglais : `remote` (télétravail, home office, remote), `hybrid` (hybride, hybrid), `on-site` (sur site, présentiel, on-site, in person), sinon exactement `none` | → Gemini ; ajoute `work_mode` |
| 8 | **Gemini** | HTTP POST `…/models/gemini-3.5-flash-lite:generateContent`, temp. 0.2, 3 essais / 5 s, `onError: continueErrorOutput` | génère mots-clés + CV + lettre (prompt §6) | sortie succès → Code ; sortie **erreur** → Groq Fallback |
| 9 | **Groq Fallback** | HTTP POST `api.groq.com/openai/v1/chat/completions`, modèle `openai/gpt-oss-120b`, `max_completion_tokens 16000`, `reasoning_effort low`, outil `browser_search`, 2 essais | **modèle de secours** si Gemini échoue (quota, panne) — même prompt, lit `$('Mode de travail').first().json` | → Code |
| 10 | **Code** | Code (JS) | parse le format à balises, normalise le LaTeX, **anti-invention**, **ATS Keyword Match Score**, contrôles de la lettre, bloc Telegram (§6) | → Mode lot ? |
| 11 | **Mode lot ?** | IF v2 : `query.deliver == "batch"` | aiguillage appel unitaire / appel depuis le workflow de lot | vrai → Réponse lot ; faux → Telegram + Compiler PDF |
| 12 | **Réponse lot** | Respond to Webhook (JSON) | renvoie la sortie du Code + `url, job_title, company, location` **sans rien envoyer sur Telegram** | fin (mode lot) |
| 13 | **Telegram** | Telegram sendMessage (Markdown), 3 essais / 5 s | message : [alerte Groq] + titre — entreprise + URL + bloc ATS/avertissements + lettre + « généré via » | — |
| 14 | **Compiler PDF** | HTTP POST `latex-compiler:8080/compile`, réponse fichier `data` | LaTeX → PDF | → Telegram CV PDF |
| 15 | **Telegram CV PDF** | Telegram sendDocument, 3 essais / 5 s | envoie le PDF (`pdf_filename`, légende « CV adapté pour … ») | → Respond to Webhook |
| 16 | **Respond to Webhook** | Respond (texte) | répond « Envoyé sur Telegram » à l'appelant | fin |

Sortie du nœud **Code** (un item) :
`cv_latex, cover_letter, generated_by ("gemini"|"groq"), pdf_filename (Nouaman_Haimoudi_<titre>_offre.pdf),
cv_title, keywords{must_have, important, nice_to_have, domain}, keyword_scores{must_have_score,
important_score, nice_to_have_score, overall_keyword_match_score}, skill_gaps[], overclaim_warnings[],
banned_phrase_warnings[], letter_checks{words, paragraphs, warnings[]}, web_search_queries[], telegram_report`.

### 5.2 « Job to CV Generator (DEMO) » — webhook `/webhook/generate-cv-demo`

Même structure et mêmes nœuds (copie du workflow honnête), avec 4 différences :
1. **Prompt DEMO** (Gemini temp. 0.4, Groq sans `browser_search`) : supprime *Projects & Portfolio*,
   garde les 3 entreprises et les dates, **réécrit les puces d'expérience avec des projets simulés
   liés à l'offre**, mots-clés en `\textbf{}` ; Education/Certifications inchangées.
2. **Code DEMO** : score = présence des mots-clés dans le CV (sans exiger le référentiel réel) ;
   pas de contrôle anti-invention ; **filigrane injecté par le code** (`draftwatermark`,
   « CV FICTIF — DÉMONSTRATION ACADÉMIQUE — NE PAS ENVOYER », gris 0.93) ; section Projects retirée
   par le code ; fichier préfixé `DEMO_`.
3. **Telegram** : bannière « 🎓 DÉMO ACADÉMIQUE — CV FICTIF, NE PAS ENVOYER À UN RECRUTEUR » en tête.
4. Usage : démonstration académique uniquement (voir §12).

### 5.3 « Indeed IA → CV (lot) » — webhook `GET /webhook/search-ai-jobs?days=&max=&q=&mode=`

| Nœud | Type | Rôle |
|---|---|---|
| **Déclencheur** | Webhook v2, `responseMode: onReceived` | répond immédiatement « Workflow was started » ; paramètres `days` (défaut 3), `max` (défaut 3), `q`, `mode=demo` |
| **Rechercher offres Indeed** | HTTP GET `…:8000/search?days=&max=&q=` (timeout 300 s, `onError: continueRegularOutput`) | liste des offres (sans extraction) |
| **Offres trouvées** | Code | 1 item par offre (`index`, `total`) ; sinon 1 item `{none:true, text}` : « Aucune offre trouvée sur Indeed pour … dans les N derniers jours » ou « Recherche Indeed impossible : … » |
| **Aucune offre ?** | IF (`$json.none === true`) | vrai → Telegram aucune offre ; faux → boucle |
| **Telegram aucune offre** | Telegram sendMessage (texte brut) | message d'absence/erreur |
| **Une offre à la fois** | Loop Over Items (SplitInBatches v3, lot de 1) | sortie 1 « loop » → offre suivante ; sortie 0 « done » → toutes les offres traitées |
| **Générer CV + lettre** | HTTP GET `http://127.0.0.1:5678/webhook/generate-cv[-demo]?deliver=batch&url=…` (timeout 600 s, `continueRegularOutput`) | appelle le générateur en **mode lot** (réponse JSON, aucun Telegram) |
| **Résultat offre** | Code | fusionne métadonnées Indeed + résultat ; en cas d'erreur → `ok:false, error` (le lot continue) → retour à la boucle |
| **Préparer envoi** | Code | construit le récapitulatif (✅/❌, score ATS, liens) et un message par offre (bannière DEMO / alerte Groq, lien, bloc ATS, lettre ; tronqué à 4000 caractères) |
| **Telegram récapitulatif** | Telegram (Markdown), `executeOnce` | 1 seul message récapitulatif (branche placée en haut → envoyée en premier) |
| **Telegram offre** | Telegram (Markdown) | 1 message par offre |
| **CV disponible ?** | IF (`$('Préparer envoi').item.json.ok`) | seulement les offres réussies |
| **Compiler PDF** | HTTP POST `latex-compiler:8080/compile` | PDF de chaque CV |
| **Telegram CV PDF** | Telegram sendDocument | PDF, légende « CV — titre — lien Indeed » |

---

## 6. Prompt, format de sortie et contrôles du nœud Code

### 6.1 Prompt (nœuds Gemini et Groq — **identiques**)

Le prompt est une expression n8n dans `jsonBody` (`={{ JSON.stringify({...}) }}`). Il contient :
les règles (§ ci-dessous), puis l'offre (`Title/Company/Location/Description`), la **langue de la
lettre** détectée (comptage de mots FR vs EN dans la description), le **WORK MODE** (`work_mode`), le
**catalogue de projets** (JSON) et le **CV source** (LaTeX).

Règles principales (workflow honnête) :
- **Ne jamais inventer** (expérience, entreprise, projet, techno, certification, métrique, niveau…).
  Techno demandée absente du référentiel = **skill gap** (ni dans le CV ni revendiquée dans la lettre).
  La certification « AWS Certified AI Practitioner » ne prouve aucun service AWS.
- Positionnement : ingénieur (EMSI 2026) + Master 2 IA ; jamais student/junior/intern, jamais senior/lead.
- **Mots-clés toujours en anglais** (traduction des termes français), 3–8 par liste.
- CV : même préambule/mise en page, titre adapté au poste, Profile ≤ 50 mots, expérience reformulable
  (entreprises/dates/postes inchangés), 2–3 projets **du catalogue uniquement** (pas PFE-RAG pour un
  poste IA : doublon avec Qualitiz), Technical Skills 5 lignes, Education/Certifications inchangées,
  localisation honnête (« Casablanca, Morocco (open to <ville>) »), Availability selon `work_mode`,
  **1 page**, toujours en anglais.
- Lettre : langue de l'offre, 180–220 mots, 4 paragraphes (HOOK, PROOF, FIT, CLOSING), formules
  interdites (« Je vous écris pour », « Fort de mon expérience », « Je serais ravi », « I am writing to »…).

### 6.2 Format de sortie imposé (identique pour tous les modèles)

```
===KEYWORDS===
MUST_HAVE:
k1, k2, ...
IMPORTANT:
...
NICE_TO_HAVE:
...
DOMAIN_KEYWORDS:
...
===CV_LATEX===
<LaTeX complet brut>
===COVER_LETTER===
<lettre>
===END===
```
Format **texte à balises** (et non JSON) : le mode JSON de Gemini perdait des antislashs LaTeX.

### 6.3 Contrôles déterministes du nœud Code (workflow honnête)

- **Lecture** : Gemini (`candidates[0].content.parts`) ou format OpenAI (`choices[0].message.content`).
- **Normalisation LaTeX** : espaces Unicode (U+202F…) → `~`/espace, `&` nu → `\&` (sauf tableaux),
  re-découpage si le LaTeX arrive sur une seule ligne.
- **`real_skills_repository`** = texte du CV source (hors Certifications) + catalogue (noms,
  descriptions, technologies). Correspondance par jetons normalisés + synonymes (`ALIASES` : CI/CD,
  k8s, LLM, RAG…).
- **ATS Keyword Match Score** : un mot-clé est *couvert* s'il est **dans le CV généré ET dans le
  référentiel**. Scores par liste en %, puis
  `overall = 0.60 × MUST + 0.30 × IMPORTANT + 0.10 × NICE` (poids renormalisés si une liste est vide).
- **skill_gaps** : mots-clés absents du référentiel (« (certification uniquement) » si seulement dans les certifications).
- **overclaim_warnings** (jamais corrigés en silence) : mot-clé/domaine présent dans le CV mais absent
  du référentiel ; technologies d'une liste connue (AWS, EC2, Terraform, Kafka…) citées dans le CV ou
  la lettre sans source ; titre/Profile « senior/lead/manager… » ; photo ou liens revenus dans le header ;
  **chiffre suspect** (« 30 % », « reduced by 40 ») absent du référentiel.
- **banned_phrase_warnings** : formules interdites et mentions « student/étudiant/junior ».
- **letter_checks** : nombre de mots (180–220), 4 paragraphes, entreprise et intitulé présents, ville
  inattendue, fausse résidence, au moins une exigence de l'offre citée.
- **telegram_report** : bloc Markdown « 📊 ATS Keyword Match » + sections d'avertissement (affichées
  seulement si non vides).

---

## 7. Fonctionnalités actuelles et limites connues

Fonctionnel (testé de bout en bout le 27/09/2026) :
- Offre unique (lien collé) en mode honnête ou DEMO → lettre + score + PDF sur Telegram (≈ 1 min).
- Recherche Indeed paramétrable (24 h / 3 / 7 / 14 jours, 1–3 offres, mots-clés, mode) → traitement
  offre par offre → récapitulatif + message + PDF par offre (≈ 1 min par offre).
- « Aucune offre trouvée » et erreurs de recherche signalées sur Telegram.
- Repli automatique Groq si Gemini échoue, avec alerte en tête du message.
- Interface web avec suivi en direct.

Limites connues :
- **Peu d'offres IA sur Indeed Maroc** : sur 3 jours, souvent 0–1 offre → élargir à 7/14 jours ou
  changer les mots-clés. Seul `ma.indeed.com` est câblé dans `/search`.
- **Lettres souvent trop courtes** (130–170 mots) et formules interdites parfois présentes : signalées
  dans Telegram, non bloquantes.
- **Anti-invention lexical** : faux positifs possibles (« deployment », « automation ») et il ne voit pas
  une invention formulée sans mot-clé technique.
- **Recherche Web** : `google_search` (Gemini) renvoie HTTP 429 sur la clé gratuite → désactivé ;
  `browser_search` (Groq) est activé mais rarement utilisé par le modèle.
- **Groq** : format parfois incomplet (balise manquante) → l'exécution échoue ; relancer.
- Le CV généré peut dépasser 1 page si le modèle rallonge le contenu (contrôlé en test, pas en production).
- L'API doit tourner sur l'hôte (sinon « Extraire offre »/« Lire CV » échouent).

---

## 8. Installation complète sur un nouveau PC

### 8.1 Prérequis
- Windows 10/11 (ou macOS/Linux, en adaptant les chemins), **Docker Desktop**, **Python 3.10+**, Git (facultatif).
- Comptes/clés : **Google AI Studio** (clé Gemini), **Groq** (clé API), **bot Telegram** (@BotFather) et ton **Chat ID**.

### 8.2 Option A — migrer TOUT n8n depuis l'ancien PC (le plus simple)
Le volume `n8n_data` contient workflows, credentials chiffrés, clé de chiffrement et compte.
```powershell
# Ancien PC (dossier courant = destination de la sauvegarde)
docker run --rm -v n8n_data:/data -v ${PWD}:/backup alpine tar czf /backup/n8n_data.tgz -C /data .
# Nouveau PC
docker volume create n8n_data
docker run --rm -v n8n_data:/data -v ${PWD}:/backup alpine tar xzf /backup/n8n_data.tgz -C /data
```
Puis passer directement à 8.4 (étapes 1, 5, 6) : tout le reste est déjà dans le volume.

### 8.3 Option B — installation neuve
1. Copier le dossier du projet (ex. `F:\...\Automatiser_postule_candidature`).
2. Créer le volume et le fichier `.env` :
   ```powershell
   docker volume create n8n_data
   cd <projet>\n8n-automation
   Set-Content .env "GENERIC_TIMEZONE=Africa/Casablanca" -Encoding utf8
   docker compose up -d --build
   ```
3. Ouvrir <http://localhost:5678>, créer le compte propriétaire.
4. **Clé API n8n** : *Settings → n8n API → Create an API key*, puis (PowerShell, une fois) :
   `setx N8N_API_KEY "<clé>"` (rouvrir le terminal ensuite). Utilisée par l'interface pour le suivi.
5. **Credentials** (*Credentials → Add credential*), avec **exactement** ces noms :
   `Gemini API Key` (Header Auth, name `x-goog-api-key`), `Groq API Key` (Header Auth, name
   `Authorization`, value `Bearer <clé>`), `Telegram Bot` (Telegram API, token du bot).
6. **Importer les workflows** : *Workflows → Import from File* pour chacun des 3 fichiers de
   `n8n-automation/workflows/`. Dans chaque workflow importé :
   - ouvrir les nœuds Gemini, Groq Fallback et tous les nœuds Telegram → re-sélectionner le credential
     (les IDs de credentials diffèrent d'une installation à l'autre) ;
   - remplacer le `chatId` des nœuds Telegram par ton Chat ID ;
   - **Publish / Activate** le workflow (sinon les webhooks de production ne répondent pas).
7. Vérifier les noms des workflows (l'interface les retrouve par nom : `Job to CV Generator`,
   `Job to CV Generator (DEMO)`, `Indeed IA → CV (lot)`).

### 8.4 API Python
```powershell
cd <projet>\job-automation
python -m venv .venv ; .\.venv\Scripts\Activate.ps1      # facultatif mais recommandé
pip install -r requirements.txt
scrapling install                                          # télécharge les navigateurs de scraping
cd src
python -m uvicorn api_server:app --host 127.0.0.1 --port 8000
```
Tester : <http://127.0.0.1:8000/health>, <http://127.0.0.1:8000/projects>, puis <http://127.0.0.1:8000/ui>
et un premier traitement « Une offre ».

---

## 9. Faire évoluer le système

### 9.1 Changer de clé ou de modèle quand un quota est atteint
- **Nouvelle clé Gemini/Groq** : n8n → *Credentials* → `Gemini API Key` / `Groq API Key` → remplacer la
  valeur. Aucun workflow à modifier.
- **Autre modèle Gemini** : nœud **Gemini** → URL
  `https://generativelanguage.googleapis.com/v1beta/models/<modèle>:generateContent`.
- **Autre modèle Groq** : dans `jsonBody` du nœud **Groq Fallback**, changer `model: '...'` (le modèle
  doit accepter ~12–15 k tokens d'entrée et ~6 k de sortie ; retirer `reasoning_effort`/`tools` s'il ne
  les supporte pas).
- **Autre fournisseur compatible OpenAI** (OpenRouter, Mistral, OpenAI, DeepSeek…) : dupliquer le
  nœud Groq Fallback ou changer son URL (`…/chat/completions`) + un nouveau credential Header Auth.
  Le nœud Code lit déjà `choices[0].message.content` : **aucun changement de parsing**. Pour que
  `generated_by` affiche le bon nom, ajuster la ligne `generatedBy = 'groq'` dans le Code.
- **Chaîne de secours plus longue** : mettre `onError: continueErrorOutput` sur Groq Fallback et relier
  sa sortie erreur à un 3e fournisseur, lui-même relié au nœud Code.
- **Toujours garder le même prompt** dans tous les nœuds LLM et le **même format à balises** (§6.2).

### 9.2 Ajouter d'autres sites que Indeed
- **Extraction d'une offre** : `/extract` est déjà générique (JSON-LD `JobPosting`, puis sélecteurs CSS
  `TITLE_SELECTORS` / `DESCRIPTION_SELECTORS` dans `collect_scrapling.py`). Tester d'abord :
  `python collect_scrapling.py "<url d'une offre>"`. Si l'extraction échoue, ajouter des sélecteurs
  ou un parseur spécifique au domaine dans `extract_job()`.
- **Recherche** : `/search` ne gère que `ma.indeed.com` (fonction `_indeed_cards`). Pour un nouveau site :
  1. écrire une fonction `search_<site>(q, days) -> list[dict]` renvoyant le **même schéma**
     `{url, job_title, company, location, posted, pub_date}` (s'inspirer de `job_collector.SITES`, qui
     sait déjà lister stagiaires.ma via XHR ; essais ReKrute dans `_archive/rekrute-test/`) ;
  2. ajouter un paramètre `source` à `/search` (registre `{"indeed": ..., "rekrute": ...}`), ou
     fusionner plusieurs sources puis trier par date ;
  3. propager `source` : interface (`index.html` : un `<select>`), `ui_routes.SearchRequest`,
     URL du nœud **Rechercher offres Indeed** (`&source=` depuis `$json.query.source`).
- Respecter les règles de politesse : 1 page de résultats, ≤ 3 offres, pauses 3–6 s entre requêtes,
  jamais de résolution automatique de captcha.

### 9.3 Rendre le système plus paramétrable (pistes)
- **Fichier de configuration** `job-automation/config.json` lu par l'API : requête IA par défaut,
  regex de filtre, `SEARCH_MAX`, domaine Indeed (ex. `fr.indeed.com`), sites activés. Exposer
  `GET /config` pour remplir les listes de l'interface.
- **Prompts hors de n8n** : servir les prompts depuis des fichiers (`GET /prompt/honnete`) et les lire
  dans n8n par un nœud HTTP → modification du prompt sans toucher aux workflows.
- **Variables n8n** : n8n 2.x bloque `$env` ; soit ajouter `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` dans
  `docker-compose.yml` (puis `{{$env.TELEGRAM_CHAT_ID}}`, modèles, etc.), soit passer les paramètres
  par le webhook (comme `days`, `max`, `mode`).
- **Planification** : ajouter un *Schedule Trigger* au workflow de lot (ex. chaque matin 9 h) relié au
  nœud « Rechercher offres Indeed » avec des valeurs par défaut.
- **Historique** : enregistrer les offres déjà traitées (fichier JSON ou SQLite dans l'API) pour ne
  pas retraiter la même offre deux jours de suite.

---

## 10. Règles pour modifier les workflows (agents/LLM)

Modifications possibles dans l'éditeur n8n **ou** via l'API REST (`http://localhost:5678/api/v1`,
en-tête `X-N8N-API-KEY`). Pièges rencontrés sur ce projet :

1. **Brouillon ≠ version publiée (n8n 2.x)** : `PUT /workflows/{id}` ne modifie que le brouillon.
   Publier ensuite : `POST /workflows/{id}/activate` avec `{"versionId": "<versionId renvoyé par le PUT>"}`.
   Les webhooks exécutent **la version publiée** (`activeVersionId`).
2. `PUT` n'accepte que `name`, `nodes`, `connections`, `settings` (sinon erreur 400).
3. Avant toute modification : `GET` le workflow, **sauvegarder le JSON**, vérifier que
   `versionId == activeVersionId` (pas de modification manuelle en cours), modifier uniquement les
   nœuds nécessaires, conserver les IDs et connexions, relire la version publiée après coup.
4. **Tester chaque nœud Code hors ligne** (Node.js, en simulant `$input` et `$('Nom')`) **avant** de
   publier : une erreur de syntaxe JS casse le workflow en production.
5. Dans une expression `={{ ... }}`, ne jamais laisser la séquence `}}` à l'intérieur du texte.
6. Écrire le code JS/LaTeX dans des fichiers (pas via des heredocs bash, qui perdent les antislashs).
7. `$env` est bloqué dans les nœuds (d'où le `chatId` en dur) ; `waitBetweenTries` ≤ 5000 ms.
8. Les nœuds lisent des données amont par `$('Nom du nœud').first().json` ou `.item.json` :
   **renommer un nœud casse ces références** (Code, Telegram, Groq Fallback, Respond…).
9. Après modification, **ré-exporter** dans `n8n-automation/workflows/` (sans secrets : les credentials
   n'y figurent que par nom/ID).
10. Mettre à jour `STEP_LABELS` (`ui_routes.py`) si des nœuds sont ajoutés/renommés, pour le suivi.

---

## 11. Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| `{"detail":"Not Found"}` sur l'interface | URL avec un point final (`/ui.`) ou majuscules | ouvrir exactement `http://127.0.0.1:8000/ui` |
| Nœud **Extraire offre / Lire CV / Rechercher offres** en erreur (ECONNREFUSED) | API non démarrée | lancer `uvicorn` (§1, étape 3) |
| « address already in use » au lancement de l'API | une API tourne déjà sur 8000 | rien à faire, ou arrêter l'autre instance |
| `error during connect … dockerDesktopLinuxEngine` | Docker Desktop fermé | lancer Docker Desktop puis `docker compose up -d` |
| Telegram `ECONNRESET` / TLS | coupure réseau vers api.telegram.org | les nœuds réessaient 3× ; relancer si la coupure dure (VPN/pare-feu) |
| Gemini HTTP 429 | quota gratuit atteint | le repli Groq prend le relais (alerte ⚠️) ; changer de clé/modèle (§9.1) |
| « Balises … absentes de la réponse groq » | Groq a mal respecté le format | relancer ; envisager un autre modèle de secours |
| « Aucune offre trouvée » systématique | peu d'offres récentes / mots-clés trop précis | élargir à 7–14 jours, simplifier les mots-clés |
| Compiler PDF HTTP 422 | LaTeX invalide produit par le modèle | lire le log renvoyé ; relancer (le Code corrige déjà `&`, espaces Unicode) |
| L'interface reste « Démarrage du workflow… » | `N8N_API_KEY` absente ou workflow renommé | `setx N8N_API_KEY …` puis redémarrer l'API ; vérifier les noms (§8.3.7) |
| « Un traitement est déjà en cours » (409) | un job tourne | attendre la fin (ou redémarrer l'API) |

Logs utiles : `docker compose logs -f n8n`, `docker logs latex-compiler`, terminal de l'API,
n8n → *Executions* (détail nœud par nœud).

---

## 12. Sécurité et éthique

- **Secrets** : clés Gemini/Groq/Telegram uniquement dans les credentials n8n (chiffrés dans le volume) ;
  `N8N_API_KEY` en variable d'environnement utilisateur. Rien en dur dans le code ni dans les exports.
  Les clés collées dans des conversations doivent être **révoquées et régénérées**.
- Tout est local : n8n (`127.0.0.1:5678`) et l'API (`127.0.0.1:8000`) ne sont pas exposés au réseau.
- **Mode honnête = candidatures réelles** : personnalisation ≠ invention ; les écarts sont signalés,
  jamais corrigés en silence ; relire chaque lettre avant envoi (surtout si ⚠️ Groq).
- **Mode DEMO = démonstration académique uniquement** : il invente des projets d'expérience sous de vrais
  employeurs. Le filigrane « CV FICTIF », le préfixe `DEMO_` et la bannière Telegram sont des garde-fous
  **volontaires** : ne pas les retirer et ne jamais envoyer un CV DEMO à un recruteur.
- Scraping : volumes volontairement faibles (1 page, ≤ 3 offres, pauses), respecter les conditions
  d'utilisation des sites.
