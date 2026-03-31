# core/jobs_service.py
"""
Job fetching + AI enrichment — NO sklearn dependency.
Fallback scorer uses pure Python (collections.Counter) so it works
in any Docker image without scikit-learn.
"""

import os, json, re, time, math, requests
from collections import Counter
from datetime import datetime, timedelta, UTC

# ── env ──────────────────────────────────────────────────────────────────────
ADZUNA_APP_ID  = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

_gemini_client = None
if GEMINI_API_KEY:
    try:
        from google import genai
        from google.genai import types as gtypes
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("✅ Gemini client initialised")
    except Exception as e:
        print(f"⚠️  Gemini init failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 1. PURE-PYTHON FALLBACK SCORER  (no sklearn required)
# ═══════════════════════════════════════════════════════════════════════════════

_STOPWORDS = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "by","from","is","are","was","were","be","been","being","have","has",
    "had","do","does","did","will","would","shall","should","may","might",
    "must","can","could","not","no","nor","so","yet","both","either",
    "neither","each","few","more","most","other","some","such","than",
    "too","very","just","about","above","after","before","between","into",
    "through","during","including","until","against","among","throughout",
    "during","across","following","up","out","as","if","then","than","when",
    "where","why","how","all","both","any","same","so","we","our","i","you",
    "he","she","they","it","its","this","that","these","those",
}

def _tokenize(text: str) -> list:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s\+\#]", " ", text)
    tokens = text.split()
    return [t for t in tokens if t not in _STOPWORDS and len(t) > 1]

def _bigrams(tokens: list) -> list:
    return [f"{tokens[i]}_{tokens[i+1]}" for i in range(len(tokens) - 1)]

def _tf(tokens: list) -> dict:
    c = Counter(tokens)
    total = max(len(tokens), 1)
    return {k: v / total for k, v in c.items()}

def _cosine(vec_a: dict, vec_b: dict) -> float:
    keys = set(vec_a) & set(vec_b)
    if not keys:
        return 0.0
    dot = sum(vec_a[k] * vec_b[k] for k in keys)
    mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
    mag_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)

def _pure_python_score(resume_text: str, job_text: str) -> int:
    """
    TF cosine similarity using only stdlib.
    Uses unigrams + bigrams for better phrase matching (e.g. 'machine learning').
    Returns 0-100.
    """
    if not resume_text or not job_text:
        return 0
    try:
        rt = _tokenize(resume_text[:4000])
        jt = _tokenize(job_text[:2000])
        # combine unigrams + bigrams
        r_feats = rt + _bigrams(rt)
        j_feats = jt + _bigrams(jt)
        sim = _cosine(_tf(r_feats), _tf(j_feats))
        # scale: raw cosine ~0.1–0.4 for decent matches; *220 maps that to 22–88
        return int(min(100, sim * 220))
    except Exception as e:
        print(f"⚠️  Pure-python scorer error: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. JOB FETCHERS
# ═══════════════════════════════════════════════════════════════════════════════

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")

def _parse_closing(created_raw: str):
    if not created_raw:
        return None, False
    try:
        dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        closing = (dt + timedelta(days=30)).date()
        days_left = (closing - datetime.now(UTC).date()).days
        return closing, 0 <= days_left <= 7
    except Exception:
        return None, False

def _stub(**kw) -> dict:
    return {
        "id":            kw.get("id", ""),
        "title":         kw.get("title", ""),
        "company":       kw.get("company", "Unknown"),
        "location":      kw.get("location", "Remote"),
        "description":   kw.get("description", ""),
        "url":           kw.get("url", "#"),
        "salary":        kw.get("salary"),
        "closing_date":  kw.get("closing_date"),
        "closing_soon":  kw.get("closing_soon", False),
        "source":        kw.get("source", ""),
        "match_score":   0,
        "resume_tweaks": [],
        "saved":         False,
    }


def fetch_adzuna_jobs(query: str = "Software Engineer", country: str = "in") -> list:
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        print("⚠️  Adzuna keys missing — skipping.")
        return []
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    params = {
        "app_id": ADZUNA_APP_ID, "app_key": ADZUNA_APP_KEY,
        "results_per_page": 20, "what": query, "content-type": "application/json",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        print(f"✅ Adzuna {r.status_code} — '{query}'")
        if r.status_code != 200:
            return []
        jobs = []
        for res in r.json().get("results", []):
            closing, soon = _parse_closing(res.get("created"))
            jobs.append(_stub(
                id          = res.get("id", ""),
                title       = res.get("title", ""),
                company     = (res.get("company") or {}).get("display_name", "Unknown"),
                location    = (res.get("location") or {}).get("display_name", "Remote"),
                description = res.get("description", ""),
                url         = res.get("redirect_url", "#"),
                salary      = res.get("salary_max"),
                closing_date= closing,
                closing_soon= soon,
                source      = "Adzuna",
            ))
        print(f"   → {len(jobs)} Adzuna jobs")
        return jobs
    except Exception as e:
        print(f"❌ Adzuna error: {e}")
        return []


def fetch_remotive_jobs(query: str = "Software Engineer") -> list:
    """Free, no API key needed."""
    try:
        r = requests.get(
            "https://remotive.com/api/remote-jobs",
            params={"search": query, "limit": 15},
            timeout=10,
        )
        if r.status_code != 200:
            return []
        jobs = []
        for j in r.json().get("jobs", []):
            jobs.append(_stub(
                id          = str(j.get("id", "")),
                title       = j.get("title", ""),
                company     = j.get("company_name", "Unknown"),
                location    = "Remote",
                description = _strip_html(j.get("description", "")),
                url         = j.get("url", "#"),
                salary      = j.get("salary", ""),
                source      = "Remotive",
            ))
        print(f"   → {len(jobs)} Remotive jobs")
        return jobs
    except Exception as e:
        print(f"❌ Remotive error: {e}")
        return []


def fetch_all_jobs(role: str) -> list:
    all_jobs = []
    all_jobs += fetch_adzuna_jobs(query=role)
    all_jobs += fetch_remotive_jobs(query=role)

    # Deduplicate by title+company
    seen, unique = set(), []
    for j in all_jobs:
        key = (j["title"].lower().strip(), j["company"].lower().strip())
        if key not in seen:
            seen.add(key)
            unique.append(j)
    print(f"✅ {len(unique)} unique jobs after dedup")
    return unique


# ═══════════════════════════════════════════════════════════════════════════════
# 3. GEMINI BATCH ENRICHMENT
# ═══════════════════════════════════════════════════════════════════════════════

_BATCH_PROMPT = """\
You are a senior technical recruiter and ATS expert.

Given a candidate RESUME and a numbered list of JOBS, return ONLY a valid JSON
array (no markdown, no extra text). Each element maps to the job at the same index:

[
  {
    "match_score": <integer 0-100>,
    "resume_tweaks": ["<tip1 under 12 words>", "<tip2>", "<tip3>"]
  },
  ...
]

Scoring:
  80-100 = near-perfect fit
  60-79  = strong fit, minor gaps
  40-59  = moderate fit
  20-39  = partial fit, significant gaps
  0-19   = poor fit

Be realistic — most resumes score 35-65 against a random posting.
Return EXACTLY as many objects as jobs listed, in the same order.
"""

def _gemini_batch(resume_text: str, jobs: list) -> list:
    """One Gemini call for all jobs. Returns list same length as jobs."""
    empty = [{"match_score": 0, "resume_tweaks": []} for _ in jobs]
    if not _gemini_client or not resume_text or not jobs:
        return empty

    job_blocks = []
    for i, j in enumerate(jobs):
        desc = (j.get("description") or "")[:600].replace("\n", " ")
        job_blocks.append(f"[{i}] {j.get('title','')} @ {j.get('company','')} — {desc}")

    prompt = (
        f"{_BATCH_PROMPT}\n\n"
        f"RESUME:\n{resume_text[:3000]}\n\n"
        f"JOBS:\n" + "\n".join(job_blocks)
    )

    try:
        from google.genai import types as gtypes
        resp = _gemini_client.models.generate_content(
            model    = "gemini-1.5-flash",
            contents = prompt,
            config   = gtypes.GenerateContentConfig(
                response_mime_type = "application/json",
                max_output_tokens  = 1024,
                temperature        = 0.15,
            ),
        )
        raw  = resp.text.strip()
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("Expected JSON array")

        results = []
        for item in data[:len(jobs)]:
            results.append({
                "match_score":   max(0, min(100, int(item.get("match_score", 0)))),
                "resume_tweaks": [str(t) for t in item.get("resume_tweaks", [])[:3]],
            })
        while len(results) < len(jobs):
            results.append({"match_score": 0, "resume_tweaks": []})
        return results

    except Exception as e:
        print(f"⚠️  Gemini batch error: {e}")
        return empty


# ═══════════════════════════════════════════════════════════════════════════════
# 4. MAIN PUBLIC FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def get_jobs_for_user(role: str, resume_text: str, top_n: int = 20) -> list:
    """
    Full pipeline: fetch → score (Gemini batch OR pure-python) → sort → cap.

    Usage in app.py:
        from core.jobs_service import get_jobs_for_user
        jobs = get_jobs_for_user(role=current_user.inferred_role, resume_text=current_user.resume_text or "")
    """
    print(f"\n🔍 Fetching jobs for: '{role}'")
    jobs = fetch_all_jobs(role)
    if not jobs:
        return []

    jobs = jobs[:top_n]

    if resume_text and _gemini_client:
        print(f"🤖 Gemini batch scoring {len(jobs)} jobs…")
        t0 = time.time()
        results = _gemini_batch(resume_text, jobs)
        print(f"   ✅ Done in {time.time()-t0:.1f}s")
        for job, res in zip(jobs, results):
            job["match_score"]   = res["match_score"]
            job["resume_tweaks"] = res["resume_tweaks"]
            # If Gemini returned 0 for this item, fill with pure-python score
            if job["match_score"] == 0:
                job["match_score"] = _pure_python_score(
                    resume_text, f"{job['title']} {job['description']}"
                )
    else:
        print("ℹ️  No Gemini — using pure-python scorer")
        for job in jobs:
            job["match_score"] = _pure_python_score(
                resume_text, f"{job['title']} {job['description']}"
            ) if resume_text else 0

    jobs.sort(key=lambda j: j["match_score"], reverse=True)
    top = jobs[0]["match_score"] if jobs else 0
    print(f"✅ Returning {len(jobs)} jobs | top score: {top}%")
    return jobs


# ── Legacy alias (keeps old call sites working) ──────────────────────────────
def enrich_jobs_with_ai(jobs: list, resume_text: str) -> list:
    if not jobs:
        return []
    results = _gemini_batch(resume_text, jobs)
    for job, res in zip(jobs, results):
        job["match_score"]   = res["match_score"]
        job["resume_tweaks"] = res["resume_tweaks"]
        if job["match_score"] == 0 and resume_text:
            job["match_score"] = _pure_python_score(
                resume_text, f"{job['title']} {job['description']}"
            )
    jobs.sort(key=lambda j: j["match_score"], reverse=True)
    return jobs