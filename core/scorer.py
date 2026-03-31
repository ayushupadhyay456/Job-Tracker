# core/scorer.py
"""
Resume analyser for PathHire.
Uses Gemini for rich analysis; falls back to pure-Python cosine when unavailable.
No sklearn dependency.
"""

import os, json, re, math
from collections import Counter

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

_client = None
if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types as gtypes
        _client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"⚠️  scorer.py: Gemini init failed: {e}")


# ── Pure-Python scorer (no external deps) ────────────────────────────────────

_STOP = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "by","from","is","are","was","were","be","been","have","has","had",
    "do","does","will","would","should","may","might","can","could","not",
    "no","we","our","i","you","he","she","they","it","its","this","that",
}

def _tok(text: str) -> list:
    t = re.sub(r"[^a-z0-9\s\+\#]", " ", text.lower())
    return [w for w in t.split() if w not in _STOP and len(w) > 1]

def _tf(tokens: list) -> dict:
    c = Counter(tokens + [f"{tokens[i]}_{tokens[i+1]}" for i in range(len(tokens)-1)])
    n = max(len(tokens), 1)
    return {k: v/n for k, v in c.items()}

def _cos(a: dict, b: dict) -> float:
    keys = set(a) & set(b)
    if not keys: return 0.0
    dot = sum(a[k]*b[k] for k in keys)
    ma  = math.sqrt(sum(v*v for v in a.values()))
    mb  = math.sqrt(sum(v*v for v in b.values()))
    return dot/(ma*mb) if ma and mb else 0.0

def score_resume_against_job(resume_text: str, job_text: str) -> int:
    """Fast local scorer — no API. Returns 0-100."""
    if not resume_text or not job_text:
        return 0
    try:
        sim = _cos(_tf(_tok(resume_text[:4000])), _tf(_tok(job_text[:2000])))
        return int(min(100, sim * 220))
    except Exception:
        return 0


# ── Gemini deep analysis ──────────────────────────────────────────────────────

_PROMPT = """\
You are a senior technical recruiter specialising in ATS screening.

Analyse the RESUME against the JOB DESCRIPTION.
Return ONLY valid JSON — no markdown, no extra text:

{{
  "match_score":       <integer 0-100>,
  "missing_keywords":  [<string>, ...],
  "bullet_point_fix":  "<one concrete bullet-point rewrite>",
  "summary":           "<exactly 2 sentences of actionable advice>"
}}

RESUME:
{resume}

JOB DESCRIPTION:
{jd}
"""

def analyze_job_fit(resume_text: str, job_description: str) -> dict:
    """
    Rich Gemini analysis used on the Resume page.
    Falls back to pure-python score if Gemini unavailable.
    """
    if not resume_text or not job_description:
        return {"error": "Missing input data."}

    if not _client:
        return {
            "match_score":      score_resume_against_job(resume_text, job_description),
            "missing_keywords": [],
            "bullet_point_fix": "Add quantified results to at least 3 bullet points.",
            "summary":          "Gemini not configured. Score estimated via keyword analysis.",
        }

    prompt = _PROMPT.format(resume=resume_text[:4000], jd=job_description[:2000])

    try:
        from google.genai import types as gtypes
        resp = _client.models.generate_content(
            model    = "gemini-1.5-flash",
            contents = prompt,
            config   = gtypes.GenerateContentConfig(
                response_mime_type = "application/json",
                max_output_tokens  = 512,
                temperature        = 0.1,
            ),
        )
        raw  = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.text.strip())
        data = json.loads(raw)
        return {
            "match_score":      max(0, min(100, int(data.get("match_score", 0)))),
            "missing_keywords": data.get("missing_keywords", [])[:8],
            "bullet_point_fix": data.get("bullet_point_fix", ""),
            "summary":          data.get("summary", ""),
        }
    except Exception as e:
        print(f"❌ analyze_job_fit error: {e}")
        return {
            "match_score":      score_resume_against_job(resume_text, job_description),
            "missing_keywords": [],
            "bullet_point_fix": "",
            "summary":          f"Analysis failed: {str(e)[:100]}",
        }