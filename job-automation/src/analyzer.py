import re

COMMON_TECHNOLOGIES = [
    "Python", "Java", "C++", "JavaScript", "SQL",
    "Machine Learning", "Deep Learning", "TensorFlow", "PyTorch",
    "Docker", "Kubernetes", "AWS", "Azure", "GCP",
    "REST APIs", "Git", "React", "Node.js", "NLP",
]

def extract_job_title(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[0] if lines else "Unknown"

def extract_skills(text: str) -> list[str]:
    lines = text.splitlines()
    items, capture = [], False
    for line in lines:
        stripped = line.strip()
        if re.match(r"^Requirements\s*:?\s*$", stripped, re.IGNORECASE):
            capture = True
            continue
        if capture:
            if stripped.startswith(("-", "*")):
                items.append(stripped.lstrip("-* ").strip())
            elif stripped == "":
                continue
            else:
                capture = False
    return items

def extract_technologies(text: str) -> list[str]:
    return [t for t in COMMON_TECHNOLOGIES if re.search(rf"\b{re.escape(t)}\b", text, re.IGNORECASE)]

def analyze_job(text: str) -> dict:
    return {
        "job_title": extract_job_title(text),
        "skills": extract_skills(text),
        "technologies": extract_technologies(text),
    }