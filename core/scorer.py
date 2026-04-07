# core/scorer.py
"""
Resume scorer — optimised for high user volume.

KEY OPTIMISATIONS vs original:
  1. ATS score cached per resume hash (TTL=60 min) — same resume never re-scored
  2. _ATS_PROMPT trimmed by ~300 tokens (rubric shortened, field descriptions removed)
  3. resume sent to Gemini cut to 3000 chars (was 4500)
  4. max_output_tokens for ATS reduced to 900 (was 1200)
  5. _JOB_FIT_PROMPT trimmed; resume/JD inputs reduced
  6. analyze_job_fit cached per (resume_hash, jd_hash)
  7. Re-initialises Gemini client only once (module-level singleton)
"""

import os, json, re, math, hashlib, time, threading
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


# ─────────────────────────────────────────────────────────────────────────────
# CACHES
# ─────────────────────────────────────────────────────────────────────────────

_cache_lock   = threading.Lock()
_ats_cache:  dict = {}   # resume_hash → {ts, data}
_fit_cache:  dict = {}   # resume_hash+jd_hash → {ts, data}
ATS_TTL = 3600           # 1 hour — resume doesn't change often
FIT_TTL = 1800           # 30 min

def _h(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()[:16]

def _cget(cache: dict, key: str, ttl: int):
    e = cache.get(key)
    if e and (time.time() - e["ts"]) < ttl:
        return e["data"]
    return None

def _cset(cache: dict, key: str, data):
    with _cache_lock:
        cache[key] = {"ts": time.time(), "data": data}


# ─────────────────────────────────────────────────────────────────────────────
# SHARED UTILITIES
# ─────────────────────────────────────────────────────────────────────────────

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
    return {k: v / n for k, v in c.items()}

def _cos(a: dict, b: dict) -> float:
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    dot = sum(a[k] * b[k] for k in keys)
    ma  = math.sqrt(sum(v * v for v in a.values()))
    mb  = math.sqrt(sum(v * v for v in b.values()))
    return dot / (ma * mb) if ma and mb else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# 1. RESUME ATS QUALITY SCORE
# ─────────────────────────────────────────────────────────────────────────────

# OPTIMISED: ~300 fewer tokens than original (rubric condensed, field hints removed)
_ATS_PROMPT = """\
You are a senior ATS specialist. Evaluate this resume strictly.
Most resumes score 40-75. Only exceptional resumes score above 85.
Return ONLY valid JSON, no markdown:

{{"overall":<0-100>,"technical_skills":<0-100>,"experience":<0-100>,
"keywords":<0-100>,"formatting":<0-100>,
"missing_keywords":[<up to 5 missing skills for this role>],
"bullet_point_fix":"<rewrite one weak bullet with numbers>",
"summary":"<2 actionable sentences on what to improve>",
"missing_details":[{{"category":"<Contact Info|Professional Summary|Quantified Achievements|Keywords|Skills Section|Education|Certifications|LinkedIn/GitHub|Action Verbs|Formatting>","severity":"<critical|high|medium>","title":"<6 words max>","description":"<2 sentences: what's missing and how to fix>"}}]}}

Penalise heavily: no numbers/metrics (−20), no contact info (−15), no summary (−10), paragraph blocks (−15), no skills section (−10).
Provide 4-6 items in missing_details.
Target role: {role}
RESUME:
{resume}"""

def _fallback_ats_score(resume_text: str, role: str) -> dict:
    """Heuristic ATS quality score — no API needed."""
    text  = resume_text or ""
    lower = text.lower()
    words = _tok(text)
    word_count = len(words)

    TECH_SIGNALS = [
        "python","javascript","java","sql","react","node","aws","docker",
        "kubernetes","git","html","css","typescript","golang","rust","c++",
        "machine learning","deep learning","tensorflow","pytorch","pandas",
        "excel","tableau","power bi","r programming","spark","kafka","redis",
        "mongodb","postgresql","mysql","rest api","graphql","ci/cd","linux",
        "azure","gcp","flask","django","spring","agile","scrum","jira",
    ]
    found_tech  = sum(1 for s in TECH_SIGNALS if s in lower)
    tech_score  = min(100, 25 + found_tech * 5)

    has_numbers   = len(re.findall(r'\b\d+[%x]?\b', text)) >= 5
    has_years     = bool(re.search(r'\b(20\d{2}|19\d{2})\s*[-–]\s*(20\d{2}|present|current)', lower))
    has_bullets   = text.count('•') + text.count('-') + text.count('*') >= 5
    has_titles    = bool(re.search(r'\b(engineer|developer|manager|analyst|designer|architect|lead|senior|junior)\b', lower))
    has_impact    = bool(re.search(r'\b(improved|increased|reduced|built|led|delivered|launched|managed|scaled|optimised|optimized)\b', lower))
    exp_score = min(100, 10 + (25 if has_numbers else 0) + (15 if has_years else 0) +
                   (15 if has_bullets else 0) + (15 if has_titles else 0) +
                   (15 if has_impact else 0) + (5 if word_count > 200 else 0))

    role_tokens = _tok(role) if role else []
    if role_tokens and words:
        overlap  = len(set(words) & set(role_tokens))
        kw_score = min(100, 25 + overlap * 10 + min(word_count // 25, 35))
    else:
        kw_score = min(100, 25 + min(word_count // 20, 55))

    SECTION_HEADERS = ["experience","education","skills","summary","objective",
                       "projects","certifications","achievements","contact","profile"]
    sections_found = sum(1 for h in SECTION_HEADERS if h in lower)
    has_email      = bool(re.search(r'[\w.+-]+@[\w-]+\.\w+', text))
    has_phone      = bool(re.search(r'[\+\(]?\d[\d\s\-\(\)]{8,}', text))
    reasonable_len = 200 <= word_count <= 1200
    has_dates      = len(re.findall(r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b', lower)) >= 2
    fmt_score = min(100, 10 + min(sections_found * 10, 40) + (15 if has_email else 0) +
                   (10 if has_phone else 0) + (15 if reasonable_len else 0) + (10 if has_dates else 0))

    overall = int(tech_score * 0.30 + exp_score * 0.30 + kw_score * 0.20 + fmt_score * 0.20)

    ROLE_KW_MAP = {
        "software": ["system design","rest api","microservices","unit testing","code review"],
        "data":     ["statistical analysis","data pipeline","etl","visualization","a/b testing"],
        "frontend": ["responsive design","accessibility","web performance","component library"],
        "backend":  ["database optimisation","caching","message queue","api design","load balancing"],
        "devops":   ["infrastructure as code","monitoring","deployment pipeline","container orchestration"],
        "product":  ["roadmap","stakeholder","user research","metrics","sprint planning"],
        "design":   ["user flow","wireframe","prototype","design system","usability testing"],
        "manager":  ["team leadership","budget","okr","cross-functional","performance review"],
    }
    missing = []
    for key, suggestions in ROLE_KW_MAP.items():
        if key in (role or "").lower():
            missing = [s for s in suggestions if s not in lower][:5]
            break

    missing_details = []
    if not has_numbers:
        missing_details.append({"category":"Quantified Achievements","severity":"critical",
            "title":"No metrics or numbers found",
            "description":"Your resume contains no quantified results. ATS systems rank resumes with measurable impact 3× higher. Add specific numbers to at least 5 bullet points, e.g. 'Reduced API latency by 40%'."})
    if not has_impact:
        missing_details.append({"category":"Action Verbs","severity":"high",
            "title":"Weak or missing action verbs",
            "description":"Bullet points lack strong action verbs. Replace passive phrases with power verbs: Led, Built, Optimised, Delivered, Reduced, Increased."})
    if not bool(re.search(r'\b(summary|profile|objective|about)\b', lower)):
        missing_details.append({"category":"Professional Summary","severity":"high",
            "title":"No professional summary section",
            "description":"A 3–4 line professional summary is the first thing ATS and recruiters parse. State your role, years of experience, top skills, and career goal."})
    if not has_email:
        missing_details.append({"category":"Contact Info","severity":"critical",
            "title":"Missing email address",
            "description":"No email address detected — recruiters cannot contact you. Add your email at the top along with phone and LinkedIn URL."})
    if sections_found < 3:
        missing_details.append({"category":"Formatting","severity":"high",
            "title":"Missing standard resume sections",
            "description":f"Only {sections_found} section(s) detected. Add explicit headers in ALL CAPS: EXPERIENCE, EDUCATION, SKILLS, PROJECTS, CERTIFICATIONS."})
    if not has_bullets and word_count > 100:
        missing_details.append({"category":"Formatting","severity":"high",
            "title":"No bullet points — dense paragraph text",
            "description":"ATS parsers struggle with dense prose. Convert every responsibility into a concise bullet point starting with an action verb."})

    issues = []
    if not has_numbers:  issues.append("add quantified achievements")
    if not has_bullets:  issues.append("use bullet points for experience")
    if sections_found < 3: issues.append("add clearly labelled sections")
    if not has_email:    issues.append("include contact information")

    summary = (f"To improve your ATS score, {issues[0]}. " +
               (f"Also {issues[1]}." if len(issues) > 1 else "Focus on strong action verbs.")) \
              if issues else \
              "Your resume has good structure and measurable achievements. Consider adding more role-specific keywords."

    return {
        "overall": overall, "technical_skills": tech_score, "experience": exp_score,
        "keywords": kw_score, "formatting": fmt_score,
        "missing_keywords": missing,
        "bullet_point_fix": (
            "Replace task-based bullets like 'Responsible for data analysis' with "
            "impact-based ones: 'Analysed 2M+ rows of sales data, identifying trends that increased revenue by 18% QoQ.'"
        ),
        "summary": summary,
        "missing_details": missing_details,
    }


def score_resume_ats(resume_text: str, role: str = "") -> dict:
    """
    ATS quality score for the /resume page.
    CACHED per resume hash (TTL=1 hour) — Gemini called at most once per unique resume.
    Falls back to heuristic if Gemini unavailable or quota exhausted.
    """
    if not resume_text:
        return {"overall":0,"technical_skills":0,"experience":0,"keywords":0,
                "formatting":0,"missing_keywords":[],"bullet_point_fix":"","summary":"","missing_details":[]}

    # ── Cache check ────────────────────────────────────────────────────────────
    cache_key = _h(resume_text) + ":" + (role or "")[:30]
    cached = _cget(_ats_cache, cache_key, ATS_TTL)
    if cached is not None:
        print("📦 ATS score cache hit")
        return cached

    if _client:
        try:
            from google.genai import types as gtypes
            prompt = _ATS_PROMPT.format(
                role   = role or "general professional",
                resume = resume_text[:3000],   # was 4500 — saves ~375 tokens/call
            )
            resp = _client.models.generate_content(
                model    = "gemini-2.0-flash",
                contents = prompt,
                config   = gtypes.GenerateContentConfig(
                    response_mime_type = "application/json",
                    max_output_tokens  = 900,   # was 1200
                    temperature        = 0.1,
                ),
            )
            raw  = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.text.strip())
            data = json.loads(raw)

            def clamp(v):
                try: return max(0, min(100, int(v)))
                except: return 0

            raw_md = data.get("missing_details", [])
            missing_details = [
                {"category": item.get("category","General"), "severity": item.get("severity","medium"),
                 "title": item.get("title",""), "description": item.get("description","")}
                for item in raw_md
                if isinstance(item, dict) and item.get("title") and item.get("description")
            ]

            result = {
                "overall":          clamp(data.get("overall", 0)),
                "technical_skills": clamp(data.get("technical_skills", 0)),
                "experience":       clamp(data.get("experience", 0)),
                "keywords":         clamp(data.get("keywords", 0)),
                "formatting":       clamp(data.get("formatting", 0)),
                "missing_keywords": data.get("missing_keywords", [])[:5],
                "bullet_point_fix": data.get("bullet_point_fix", ""),
                "summary":          data.get("summary", ""),
                "missing_details":  missing_details,
            }
            _cset(_ats_cache, cache_key, result)
            return result

        except Exception as e:
            print(f"⚠️  score_resume_ats Gemini error: {e} — using fallback")

    result = _fallback_ats_score(resume_text, role)
    _cset(_ats_cache, cache_key, result)   # cache fallback too
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 2. JOB-MATCH SCORE  (pure python, no API)
# ─────────────────────────────────────────────────────────────────────────────

def score_resume_against_job(resume_text: str, job_text: str) -> int:
    if not resume_text or not job_text:
        return 0
    try:
        sim = _cos(_tf(_tok(resume_text[:3000])), _tf(_tok(job_text[:1000])))
        return int(min(100, sim * 220))
    except Exception:
        return 0


# ─────────────────────────────────────────────────────────────────────────────
# 3. JOB-SPECIFIC DEEP ANALYSIS
# ─────────────────────────────────────────────────────────────────────────────

# OPTIMISED: ~150 tokens shorter than original
_JOB_FIT_PROMPT = """\
You are a senior ATS recruiter. Analyse RESUME vs JOB DESCRIPTION.
Return ONLY valid JSON, no markdown:
{{"match_score":<0-100>,"missing_keywords":[<strings>],"bullet_point_fix":"<concrete rewrite>","summary":"<2 actionable sentences>"}}
RESUME:{resume}
JOB:{jd}"""

def analyze_job_fit(resume_text: str, job_description: str) -> dict:
    """
    Rich analysis for job-specific fit.
    CACHED per (resume_hash, jd_hash) — repeated clicks don't re-call Gemini.
    """
    if not resume_text or not job_description:
        return {"error": "Missing input data."}

    # ── Cache check ────────────────────────────────────────────────────────────
    cache_key = _h(resume_text) + ":" + _h(job_description)
    cached = _cget(_fit_cache, cache_key, FIT_TTL)
    if cached is not None:
        return cached

    if not _client:
        result = {
            "match_score":      score_resume_against_job(resume_text, job_description),
            "missing_keywords": [],
            "bullet_point_fix": "Add quantified results to at least 3 bullet points.",
            "summary":          "Gemini not configured. Score estimated via keyword analysis.",
        }
        _cset(_fit_cache, cache_key, result)
        return result

    prompt = _JOB_FIT_PROMPT.format(
        resume = resume_text[:3000],       # was 4000
        jd     = job_description[:1500],   # was 2000
    )
    try:
        from google.genai import types as gtypes
        resp = _client.models.generate_content(
            model    = "gemini-2.0-flash",
            contents = prompt,
            config   = gtypes.GenerateContentConfig(
                response_mime_type = "application/json",
                max_output_tokens  = 400,   # unchanged — already tight
                temperature        = 0.1,
            ),
        )
        raw  = re.sub(r"^```(?:json)?\s*|\s*```$", "", resp.text.strip())
        data = json.loads(raw)
        result = {
            "match_score":      max(0, min(100, int(data.get("match_score", 0)))),
            "missing_keywords": data.get("missing_keywords", [])[:6],
            "bullet_point_fix": data.get("bullet_point_fix", ""),
            "summary":          data.get("summary", ""),
        }
        _cset(_fit_cache, cache_key, result)
        return result

    except Exception as e:
        print(f"❌ analyze_job_fit error: {e}")
        result = {
            "match_score":      score_resume_against_job(resume_text, job_description),
            "missing_keywords": [],
            "bullet_point_fix": "",
            "summary":          f"Analysis failed: {str(e)[:100]}",
        }
        _cset(_fit_cache, cache_key, result)
        return result