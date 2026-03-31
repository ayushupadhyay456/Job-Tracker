import requests
import time

REMOTEOK_API = "https://remoteok.com/api"

_cache = {"jobs": [], "fetched_at": 0}
CACHE_TTL = 300  # 5 minutes


def fetch_jobs(tag: str = None, limit: int = 50) -> list[dict]:
    """Fetch jobs from RemoteOK API with simple in-memory caching."""
    now = time.time()
    if now - _cache["fetched_at"] < CACHE_TTL and _cache["jobs"]:
        jobs = _cache["jobs"]
    else:
        try:
            resp = requests.get(
                REMOTEOK_API,
                headers={"User-Agent": "JobMatchApp/1.0"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            # First item is a legal notice dict, skip it
            jobs = [j for j in data if isinstance(j, dict) and j.get("id")]
            _cache["jobs"] = jobs
            _cache["fetched_at"] = now
        except Exception as e:
            print(f"RemoteOK fetch error: {e}")
            return []

    if tag:
        tag_lower = tag.lower()
        jobs = [
            j for j in jobs
            if tag_lower in " ".join(j.get("tags", [])).lower()
            or tag_lower in (j.get("position", "") + j.get("company", "")).lower()
        ]

    return jobs[:limit]


def normalize_job(raw: dict) -> dict:
    """Flatten a RemoteOK job record into a clean dict for the frontend."""
    tags = raw.get("tags") or []
    return {
        "id":          str(raw.get("id", "")),
        "title":       raw.get("position", "Unknown Role"),
        "company":     raw.get("company", "Unknown Company"),
        "logo":        raw.get("company_logo", ""),
        "url":         raw.get("url", f"https://remoteok.com/remote-jobs/{raw.get('id','')}"),
        "tags":        tags[:8],
        "salary":      raw.get("salary", ""),
        "location":    raw.get("location", "Worldwide"),
        "date":        raw.get("date", ""),
        "description": _strip_html(raw.get("description", "")),
    }


def _strip_html(text: str) -> str:
    """Rudimentary HTML tag stripper (no external deps)."""
    import re
    clean = re.sub(r"<[^>]+>", " ", text or "")
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean[:2000]