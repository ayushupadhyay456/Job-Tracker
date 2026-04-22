# core/jobs_service.py
"""
Job fetching + AI enrichment — 6 sources, optimised for high user volume.

FREE SOURCES (no API key needed):
  ┌─────────────┬────────────────────────────────────────┬──────────┐
  │ Source      │ Focus                                  │ Limit    │
  ├─────────────┼────────────────────────────────────────┼──────────┤
  │ Remotive    │ Remote tech/software jobs              │ 15/req   │
  │ Arbeitnow   │ Europe + remote, direct from ATS       │ 25/page  │
  │ Jobicy      │ Remote jobs, keyword search            │ 20/req   │
  │ Himalayas   │ Remote, salary data often included     │ 20/req   │
  │ RemoteOK    │ Remote tech, tags-based search         │ ~20/req  │
  └─────────────┴────────────────────────────────────────┴──────────┘

KEY-REQUIRED SOURCE:
  └─ Adzuna  — Best for India + local jobs (needs ADZUNA_APP_ID / KEY in .env)

OPTIMISATIONS:
  1. Role-level job cache (TTL=10 min) — 1000 users same role → 1 fetch
  2. All 6 sources fetched in PARALLEL threads — no sequential waiting
  3. Resume hash → Gemini score cache (TTL=30 min)
  4. use_gemini=False default — pure-python scoring, zero quota cost
  5. Compact Gemini prompt: desc 300 chars, resume 2000 chars
"""

import os, json, re, time, math, hashlib, threading, requests
from collections import Counter
from datetime import datetime, timedelta, UTC
from concurrent.futures import ThreadPoolExecutor, as_completed

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
# 0. IN-MEMORY CACHES
# ═══════════════════════════════════════════════════════════════════════════════

_cache_lock     = threading.Lock()
_job_cache:     dict = {}   # role → {ts, data}
_score_cache:   dict = {}   # resume_hash:n → {ts, data}
JOB_CACHE_TTL   = 600       # 10 min
SCORE_CACHE_TTL = 1800      # 30 min

def _resume_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()[:16]

def _cache_get(cache: dict, key: str, ttl: int):
    e = cache.get(key)
    if e and (time.time() - e["ts"]) < ttl:
        return e["data"]
    return None

def _cache_set(cache: dict, key: str, data):
    with _cache_lock:
        cache[key] = {"ts": time.time(), "data": data}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. PURE-PYTHON SCORER  (zero API cost)
# ═══════════════════════════════════════════════════════════════════════════════

_STOPWORDS = {
    "a","an","the","and","or","but","in","on","at","to","for","of","with",
    "by","from","is","are","was","were","be","been","being","have","has",
    "had","do","does","did","will","would","shall","should","may","might",
    "must","can","could","not","no","nor","so","yet","both","either",
    "neither","each","few","more","most","other","some","such","than",
    "too","very","just","about","above","after","before","between","into",
    "through","during","including","until","against","among","throughout",
    "across","following","up","out","as","if","then","when","where","why",
    "how","all","any","same","we","our","i","you","he","she","they","it",
    "its","this","that","these","those",
}

def _tokenize(text: str) -> list:
    text = re.sub(r"[^a-z0-9\s\+\#]", " ", text.lower())
    return [t for t in text.split() if t not in _STOPWORDS and len(t) > 1]

def _bigrams(tokens: list) -> list:
    return [f"{tokens[i]}_{tokens[i+1]}" for i in range(len(tokens) - 1)]

def _tf(tokens: list) -> dict:
    c = Counter(tokens)
    total = max(len(tokens), 1)
    return {k: v / total for k, v in c.items()}

def _cosine(a: dict, b: dict) -> float:
    keys = set(a) & set(b)
    if not keys:
        return 0.0
    dot   = sum(a[k] * b[k] for k in keys)
    mag_a = math.sqrt(sum(v * v for v in a.values()))
    mag_b = math.sqrt(sum(v * v for v in b.values()))
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0

def _pure_python_score(resume_text: str, job_text: str) -> int:
    if not resume_text or not job_text:
        return 0
    try:
        rt  = _tokenize(resume_text[:3000])
        jt  = _tokenize(job_text[:1000])
        sim = _cosine(_tf(rt + _bigrams(rt)), _tf(jt + _bigrams(jt)))
        return int(min(100, sim * 220))
    except Exception:
        return 0


# ═══════════════════════════════════════════════════════════════════════════════
# 2. SHARED HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()

def _parse_closing(created_raw: str):
    if not created_raw:
        return None, False
    try:
        dt        = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        closing   = (dt + timedelta(days=30)).date()
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

# ── Keyword maps for source-specific filtering / tagging ─────────────────────

_JOBICY_INDUSTRY_MAP = {
    "software engineer":    "dev",
    "software developer":   "dev",
    "frontend developer":   "dev",
    "backend developer":    "dev",
    "full stack developer": "dev",
    "fullstack developer":  "dev",
    "devops engineer":      "dev",
    "cloud engineer":       "dev",
    "security engineer":    "dev",
    "mobile developer":     "dev",
    "data scientist":       "data-science",
    "data analyst":         "data-science",
    "machine learning":     "data-science",
    "product manager":      "management",
    "ux designer":          "design-multimedia",
    "ui designer":          "design-multimedia",
    "marketing":            "marketing",
    "seo":                  "seo",
}

def _jobicy_industry(role: str) -> str:
    role_lower = role.lower()
    for kw, industry in _JOBICY_INDUSTRY_MAP.items():
        if kw in role_lower:
            return industry
    return "dev"

_REMOTEOK_TAG_MAP = {
    "software engineer":    "software",
    "software developer":   "software",
    "frontend developer":   "frontend",
    "backend developer":    "backend",
    "full stack developer": "fullstack",
    "fullstack developer":  "fullstack",
    "devops engineer":      "devops",
    "data scientist":       "datascience",
    "data analyst":         "analytics",
    "machine learning":     "machinelearning",
    "product manager":      "product",
    "ux designer":          "uxdesign",
    "cloud engineer":       "cloud",
    "security engineer":    "security",
    "mobile developer":     "mobile",
    "react":                "react",
    "python":               "python",
    "node.js":              "nodejs",
    "golang":               "golang",
    "rust":                 "rust",
}

def _remoteok_tag(role: str) -> str:
    role_lower = role.lower()
    for kw, tag in _REMOTEOK_TAG_MAP.items():
        if kw in role_lower:
            return tag
    return role_lower.split()[0] if role_lower.split() else "software"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. JOB FETCHERS  — one per source
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_adzuna(query: str = "Software Engineer", country: str = "in") -> list:
    """
    Requires ADZUNA_APP_ID + ADZUNA_APP_KEY env vars.
    Best source for India + worldwide local jobs.
    Docs: https://developer.adzuna.com/
    """
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return []
    url    = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    params = {
        "app_id":           ADZUNA_APP_ID,
        "app_key":          ADZUNA_APP_KEY,
        "results_per_page": 20,
        "what":             query,
        "content-type":     "application/json",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        print(f"  [Adzuna] {r.status_code} — '{query}'")
        if r.status_code != 200:
            return []
        jobs = []
        for res in r.json().get("results", []):
            closing, soon = _parse_closing(res.get("created"))
            jobs.append(_stub(
                id           = str(res.get("id", "")),
                title        = res.get("title", ""),
                company      = (res.get("company") or {}).get("display_name", "Unknown"),
                location     = (res.get("location") or {}).get("display_name", "Remote"),
                description  = res.get("description", ""),
                url          = res.get("redirect_url", "#"),
                salary       = res.get("salary_max"),
                closing_date = closing,
                closing_soon = soon,
                source       = "Adzuna",
            ))
        print(f"  [Adzuna] → {len(jobs)} jobs")
        return jobs
    except Exception as e:
        print(f"  [Adzuna] ❌ {e}")
        return []


def _fetch_remotive(query: str = "Software Engineer") -> list:
    """
    Free, no key. Remote tech roles.
    Docs: https://remotive.com/api/remote-jobs
    """
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
                salary      = j.get("salary", "") or None,
                source      = "Remotive",
            ))
        print(f"  [Remotive] → {len(jobs)} jobs")
        return jobs
    except Exception as e:
        print(f"  [Remotive] ❌ {e}")
        return []


def _fetch_arbeitnow(query: str = "Software Engineer") -> list:
    """
    Free, no key. Pulls live data direct from ATS (Greenhouse, Lever, etc.).
    Europe + remote focus. Includes tech tags per listing.
    Docs: https://www.arbeitnow.com/blog/job-board-api
    Endpoint: GET https://www.arbeitnow.com/api/job-board-api
    Params: search=<keyword>, remote=true, page=<n>
    """
    try:
        r = requests.get(
            "https://www.arbeitnow.com/api/job-board-api",
            params={"search": query, "remote": "true", "page": 1},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  [Arbeitnow] ❌ HTTP {r.status_code}")
            return []
        jobs = []
        for j in r.json().get("data", [])[:25]:
            tags_str = ", ".join(j.get("tags", []))
            desc     = _strip_html(j.get("description", ""))
            if tags_str:
                desc = f"Skills: {tags_str}. {desc}"
            jobs.append(_stub(
                id          = str(j.get("slug", j.get("title", ""))),
                title       = j.get("title", ""),
                company     = j.get("company_name", "Unknown"),
                location    = j.get("location", "Remote"),
                description = desc,
                url         = j.get("url", "#"),
                salary      = None,
                source      = "Arbeitnow",
            ))
        print(f"  [Arbeitnow] → {len(jobs)} jobs")
        return jobs
    except Exception as e:
        print(f"  [Arbeitnow] ❌ {e}")
        return []


def _fetch_jobicy(query: str = "Software Engineer") -> list:
    """
    Free, no key. Remote-only, strong salary data for US/EU roles.
    Note: 6-hour publication delay by Jobicy design.
    Docs: https://github.com/Jobicy/remote-jobs-api
    Endpoint: GET https://jobicy.com/api/v2/remote-jobs
    Params: count, tag (keyword), industry (category slug)
    """
    industry = _jobicy_industry(query)
    try:
        r = requests.get(
            "https://jobicy.com/api/v2/remote-jobs",
            params={"count": 20, "tag": query, "industry": industry},
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  [Jobicy] ❌ HTTP {r.status_code}")
            return []
        jobs = []
        for j in r.json().get("jobs", []):
            sal = None
            if j.get("annualSalaryMin"):
                cur = j.get("salaryCurrency", "USD")
                lo  = j.get("annualSalaryMin", "")
                hi  = j.get("annualSalaryMax", "")
                sal = f"{cur} {lo}–{hi}/yr" if hi else f"{cur} {lo}/yr"

            jobs.append(_stub(
                id          = str(j.get("id", "")),
                title       = j.get("jobTitle", ""),
                company     = j.get("companyName", "Unknown"),
                location    = j.get("jobGeo", "Remote"),
                description = _strip_html(
                    j.get("jobDescription") or j.get("jobExcerpt", "")
                ),
                url         = j.get("url", "#"),
                salary      = sal,
                source      = "Jobicy",
            ))
        print(f"  [Jobicy] → {len(jobs)} jobs")
        return jobs
    except Exception as e:
        print(f"  [Jobicy] ❌ {e}")
        return []


def _fetch_himalayas(query: str = "Software Engineer") -> list:
    """
    Free, no key. Remote-only, structured salary data, global scope.
    API does NOT support keyword search — client-side token filter applied.
    Attribution required: always label source as 'Himalayas'.
    Docs: https://himalayas.app/api
    Endpoint: GET https://himalayas.app/jobs/api?limit=20&offset=0
    Rate limit: modest — cached 10 min to avoid 429s.
    """
    try:
        r = requests.get(
            "https://himalayas.app/jobs/api",
            params={"limit": 20, "offset": 0},
            timeout=12,
        )
        if r.status_code == 429:
            print("  [Himalayas] ⚠️  Rate-limited — skipping this cycle")
            return []
        if r.status_code != 200:
            print(f"  [Himalayas] ❌ HTTP {r.status_code}")
            return []

        query_tokens = set(_tokenize(query))
        # Require meaningful tokens only (strip very short ones like "ml")
        meaningful_query = {t for t in query_tokens if len(t) > 2}
        jobs = []
        for j in r.json().get("jobs", []):
            title_tokens = set(_tokenize(j.get("title", "")))
            cats         = " ".join(
                j.get("category", []) + j.get("parentCategories", [])
            )
            cat_tokens = set(_tokenize(cats))
            desc_tokens = set(_tokenize((j.get("description") or j.get("excerpt", ""))[:500]))

            # Accept job if title OR category has meaningful overlap with the role query.
            # Description tokens are used only as a last resort (weaker signal).
            title_overlap = meaningful_query & title_tokens
            cat_overlap   = meaningful_query & cat_tokens
            desc_overlap  = meaningful_query & desc_tokens

            # Require at least 1 meaningful token match in title/category.
            # Description alone is not enough — avoids vague catch-alls.
            if not title_overlap and not cat_overlap:
                # Last chance: description must have ≥2 overlapping tokens
                if len(desc_overlap) < 2:
                    continue

            sal = None
            if j.get("minSalary"):
                cur = j.get("currency", "USD")
                lo  = int(j.get("minSalary", 0))
                hi  = int(j.get("maxSalary", 0)) if j.get("maxSalary") else 0
                sal = (
                    f"{cur} {lo:,}–{hi:,}/yr"
                    if hi else f"{cur} {lo:,}/yr"
                )

            desc = _strip_html(j.get("description", j.get("excerpt", "")))
            jobs.append(_stub(
                id          = str(j.get("guid", j.get("id", ""))),
                title       = j.get("title", ""),
                company     = j.get("companyName", "Unknown"),
                location    = (
                    ", ".join(j.get("locationRestrictions", [])) or "Remote"
                ),
                description = desc,
                url         = j.get("applicationLink", "#"),
                salary      = sal,
                source      = "Himalayas",
            ))
        print(f"  [Himalayas] → {len(jobs)} jobs (after keyword filter)")
        return jobs
    except Exception as e:
        print(f"  [Himalayas] ❌ {e}")
        return []


def _fetch_remoteok(query: str = "Software Engineer") -> list:
    """
    Free, no key. Remote-only, tech-focused. Often has salary data.
    IMPORTANT: User-Agent must NOT contain 'bot' or 'google'.
    Endpoint: GET https://remoteok.com/api?tag=<tag>
    Docs: https://remoteok.com/api
    """
    tag = _remoteok_tag(query)
    try:
        r = requests.get(
            f"https://remoteok.com/api?tag={tag}",
            headers={
                "User-Agent": (
                    "JobMatch/1.0 (job aggregator; contact@jobmatch.ai)"
                )
            },
            timeout=12,
        )
        if r.status_code != 200:
            print(f"  [RemoteOK] ❌ HTTP {r.status_code}")
            return []

        # First element is API metadata dict — skip it
        job_list = [
            item for item in r.json()
            if isinstance(item, dict) and item.get("id")
        ]

        jobs = []
        for j in job_list[:20]:
            sal = None
            if j.get("salary_min"):
                lo  = int(j.get("salary_min", 0))
                hi  = int(j.get("salary_max", 0)) if j.get("salary_max") else 0
                sal = (
                    f"USD {lo:,}–{hi:,}/yr"
                    if hi else f"USD {lo:,}/yr"
                )

            tags_str = ", ".join(j.get("tags", []))
            desc     = _strip_html(j.get("description", ""))
            if tags_str:
                desc = f"Skills: {tags_str}. {desc}"

            closing, soon = _parse_closing(j.get("date", ""))
            jobs.append(_stub(
                id           = str(j.get("id", "")),
                title        = j.get("position", ""),
                company      = j.get("company", "Unknown"),
                location     = "Remote",
                description  = desc,
                url          = j.get("url", "#"),
                salary       = sal,
                closing_date = closing,
                closing_soon = soon,
                source       = "RemoteOK",
            ))
        print(f"  [RemoteOK] → {len(jobs)} jobs")
        return jobs
    except Exception as e:
        print(f"  [RemoteOK] ❌ {e}")
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# 4. PARALLEL FETCH + DEDUP
# ═══════════════════════════════════════════════════════════════════════════════

_FETCHERS = {
    "Adzuna":    _fetch_adzuna,
    "Remotive":  _fetch_remotive,
    "Arbeitnow": _fetch_arbeitnow,
    "Jobicy":    _fetch_jobicy,
    "Himalayas": _fetch_himalayas,
    "RemoteOK":  _fetch_remoteok,
}


def _role_title_is_relevant(job_title: str, role: str) -> bool:
    """
    Hard gate: returns True only if the job title is plausibly related to the
    inferred role.  Uses a two-pass strategy:

    Pass 1 — exact role-family match via a small whitelist so that e.g.
             "Senior Backend Developer" passes for role "Backend Developer".
    Pass 2 — at least one *significant* word (>3 chars) from the role query
             appears somewhere in the job title.

    A job that fails both passes is discarded before it ever reaches scoring,
    eliminating "Head of Private Trips", "Sales Associate", etc.
    """
    title_lower = job_title.lower().strip()
    role_lower  = role.lower().strip()

    # --- Pass 1: role-family keyword families ---
    # Map every known role to a small set of title keywords that are acceptable.
    ROLE_TITLE_FAMILIES: dict[str, list[str]] = {
        "machine learning engineer": ["machine learning", "ml engineer", "mlops", "ai engineer"],
        "data scientist":            ["data scientist", "data science", "ml", "machine learning"],
        "data analyst":              ["data analyst", "analytics", "business analyst", "bi analyst"],
        "devops engineer":           ["devops", "sre", "site reliability", "platform engineer", "infrastructure"],
        "backend developer":         ["backend", "back-end", "back end", "server", "api developer", "software engineer", "software developer"],
        "frontend developer":        ["frontend", "front-end", "front end", "ui developer", "react developer", "vue developer"],
        "full stack developer":      ["full stack", "fullstack", "full-stack", "software engineer", "software developer"],
        "mobile developer":          ["mobile", "android", "ios", "flutter", "react native"],
        "security engineer":         ["security", "cybersecurity", "appsec", "infosec", "penetration"],
        "cloud engineer":            ["cloud", "aws", "gcp", "azure", "infrastructure", "devops"],
        "product manager":           ["product manager", "product management", "product owner", "program manager"],
        "ux designer":               ["ux", "ui/ux", "product designer", "interaction designer", "user experience"],
        "software engineer":         ["software engineer", "software developer", "sde", "swe", "backend", "frontend",
                                      "full stack", "fullstack", "platform engineer", "application developer"],
    }
    families = ROLE_TITLE_FAMILIES.get(role_lower)
    if families:
        if any(kw in title_lower for kw in families):
            return True
        # Role is known but title matches nothing in the family → reject.
        # (Pass 2 below won't rescue it — a "Travel Manager" shouldn't match
        #  "Product Manager" just because both contain "manager".)
        return False

    # --- Pass 2: fallback for unrecognised / custom roles ---
    # Accept if at least one meaningful word from the role appears in the title.
    significant = {w for w in role_lower.split() if len(w) > 3}
    return bool(significant & set(title_lower.split()))


def _fetch_all_jobs(role: str) -> list:
    """
    Fetch from all 6 sources IN PARALLEL, dedup by (title, company),
    then apply a hard role-relevance title filter.

    Cached per role for JOB_CACHE_TTL — 1000 users same role → 1 fetch.
    """
    cached = _cache_get(_job_cache, role, JOB_CACHE_TTL)
    if cached is not None:
        print(f"📦 Job cache hit for '{role}' ({len(cached)} jobs)")
        return [j.copy() for j in cached]

    print(f"\n🔍 Fetching '{role}' from {len(_FETCHERS)} sources in parallel…")
    t0       = time.time()
    all_jobs: list = []

    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fn, role): name
            for name, fn in _FETCHERS.items()
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                all_jobs.extend(future.result())
            except Exception as e:
                print(f"  [{name}] ❌ thread error: {e}")

    # ── Step 1: dedup by normalised (title, company) ──────────────────────────
    seen:   set  = set()
    unique: list = []
    for j in all_jobs:
        key = (
            re.sub(r"\s+", " ", j["title"].lower().strip()),
            re.sub(r"\s+", " ", j["company"].lower().strip()),
        )
        if key not in seen:
            seen.add(key)
            unique.append(j)

    # ── Step 2: hard role-relevance filter on job title ───────────────────────
    # This is the primary fix: eliminate irrelevant jobs (e.g. "Head of Private
    # Trips") *before* they enter the scored result set.
    before_filter = len(unique)
    unique = [j for j in unique if _role_title_is_relevant(j["title"], role)]
    print(
        f"  🎯 Role filter: kept {len(unique)}/{before_filter} "
        f"(dropped {before_filter - len(unique)} irrelevant titles)"
    )

    print(
        f"✅ {len(unique)} unique / {len(all_jobs)} total "
        f"in {time.time()-t0:.1f}s"
    )
    _cache_set(_job_cache, role, unique)
    return [j.copy() for j in unique]


# ═══════════════════════════════════════════════════════════════════════════════
# 5. GEMINI BATCH SCORING  (optional, cached)
# ═══════════════════════════════════════════════════════════════════════════════

_BATCH_PROMPT = """\
You are an ATS expert. Score RESUME against each JOB and return ONLY a JSON array.
Each element (same order as jobs):
[{"match_score":<0-100>,"resume_tweaks":["<tip ≤10 words>","...","..."]},...]
Be realistic: most scores fall 35-65. Return exactly as many objects as jobs.\
"""


def _gemini_batch(resume_text: str, jobs: list) -> list:
    """One Gemini call scores all jobs. Cached 30 min per unique resume."""
    empty = [{"match_score": 0, "resume_tweaks": []} for _ in jobs]
    if not _gemini_client or not resume_text or not jobs:
        return empty

    cache_key = _resume_hash(resume_text) + f":{len(jobs)}"
    cached    = _cache_get(_score_cache, cache_key, SCORE_CACHE_TTL)
    if cached is not None:
        print(f"📦 Score cache hit ({len(cached)} scores)")
        return cached

    job_blocks = [
        f"[{i}] {j.get('title','')} @ {j.get('company','')} — "
        f"{(j.get('description') or '')[:300].replace(chr(10),' ')}"
        for i, j in enumerate(jobs)
    ]
    prompt = (
        f"{_BATCH_PROMPT}\n\n"
        f"RESUME:\n{resume_text[:2000]}\n\n"
        "JOBS:\n" + "\n".join(job_blocks)
    )

    try:
        from google.genai import types as gtypes
        resp = _gemini_client.models.generate_content(
            model    = "gemini-2.0-flash",
            contents = prompt,
            config   = gtypes.GenerateContentConfig(
                response_mime_type = "application/json",
                max_output_tokens  = 800,
                temperature        = 0.1,
            ),
        )
        data = json.loads(resp.text.strip())
        if not isinstance(data, list):
            raise ValueError("Expected JSON array")

        results = [
            {
                "match_score":   max(0, min(100, int(
                    item.get("match_score", 0)
                ))),
                "resume_tweaks": [
                    str(t) for t in item.get("resume_tweaks", [])[:3]
                ],
            }
            for item in data[:len(jobs)]
        ]
        while len(results) < len(jobs):
            results.append({"match_score": 0, "resume_tweaks": []})

        _cache_set(_score_cache, cache_key, results)
        return results

    except Exception as e:
        print(f"⚠️  Gemini batch error: {e}")
        return empty


# ═══════════════════════════════════════════════════════════════════════════════
# 6. MAIN PUBLIC FUNCTION
# ═══════════════════════════════════════════════════════════════════════════════

def get_jobs_for_user(
    role:        str,
    resume_text: str,
    top_n:       int  = 30,
    use_gemini:  bool = False,
) -> list:
    """
    Full pipeline: parallel fetch (cached) → score → sort → cap.

    use_gemini=False (default): TF cosine, zero Gemini quota.
    use_gemini=True: one Gemini call per unique resume (cached 30 min).

    Sources active by default (free, no key):
        Remotive, Arbeitnow, Jobicy, Himalayas, RemoteOK

    Also active if env vars set:
        Adzuna (ADZUNA_APP_ID + ADZUNA_APP_KEY) — best for India/local
    """
    jobs = _fetch_all_jobs(role)
    if not jobs:
        return []

    # Cap BEFORE scoring so Gemini token budget isn't wasted on excess jobs.
    jobs = jobs[:top_n]

    if resume_text and use_gemini and _gemini_client:
        print(f"🤖 Gemini batch scoring {len(jobs)} jobs…")
        t0      = time.time()
        results = _gemini_batch(resume_text, jobs)
        print(f"   ✅ Done in {time.time()-t0:.1f}s")
        for job, res in zip(jobs, results):
            job["match_score"]   = res["match_score"]
            job["resume_tweaks"] = res["resume_tweaks"]
            if job["match_score"] == 0:
                job["match_score"] = _pure_python_score(
                    resume_text, f"{job['title']} {job['description']}"
                )
    else:
        for job in jobs:
            job["match_score"] = (
                _pure_python_score(
                    resume_text,
                    f"{job['title']} {job['description']}"
                )
                if resume_text else 0
            )

    jobs.sort(key=lambda j: j["match_score"], reverse=True)

    # Drop any job whose match_score is 0 — these were killed by the industry
    # shield in scorer.py (e.g. "Head of Private Trips") and should never
    # appear in the user-facing results list.
    jobs = [j for j in jobs if j["match_score"] > 0]

    if not jobs:
        print("⚠️  All jobs scored 0 — returning empty list (role mismatch likely)")
        return []

    print(
        f"✅ Returning {len(jobs)} jobs | "
        f"top score: {jobs[0]['match_score']}%"
    )
    return jobs


# ═══════════════════════════════════════════════════════════════════════════════
# 7. UTILITIES & LEGACY ALIASES
# ═══════════════════════════════════════════════════════════════════════════════

def enrich_jobs_with_ai(jobs: list, resume_text: str) -> list:
    """Legacy alias — enriches a pre-fetched list with Gemini scores."""
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


def cache_stats() -> dict:
    """
    Diagnostic helper — expose via a debug/admin route to inspect cache.
    Example route:
        @app.route('/debug/cache')
        def debug_cache():
            from core.jobs_service import cache_stats
            return jsonify(cache_stats())
    """
    now = time.time()
    return {
        "job_cache_entries":   len(_job_cache),
        "score_cache_entries": len(_score_cache),
        "job_cache": [
            {"role": k, "age_s": round(now - v["ts"]), "count": len(v["data"])}
            for k, v in _job_cache.items()
        ],
        "score_cache": [
            {"key": k, "age_s": round(now - v["ts"])}
            for k, v in _score_cache.items()
        ],
    }


# Keep old import paths working
fetch_adzuna_jobs   = _fetch_adzuna
fetch_remotive_jobs = _fetch_remotive
fetch_all_jobs      = _fetch_all_jobs