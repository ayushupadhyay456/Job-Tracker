"""
core/resume_parser.py

Extracts plain text from PDF and parses structured resume data.
"""

from pypdf import PdfReader
import io
import re


def extract_text_from_pdf(file_bytes: bytes) -> str:
    """Extract plain text from a PDF byte stream."""
    reader = PdfReader(io.BytesIO(file_bytes))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text.strip())
    return "\n\n".join(pages)


def truncate_resume(text: str, max_chars: int = 5000) -> str:
    """Keep the most informative part of the resume for AI scoring."""
    return text[:max_chars]


# ── Role inference ────────────────────────────────────────────────────────────
# Maps keywords found in resume → clean role string for Adzuna query
ROLE_KEYWORD_MAP = [
    (["machine learning", "deep learning", "pytorch", "tensorflow", "mlops", "model training", "model deployment"],
     "Machine Learning Engineer"),
    (["data scientist", "data science", "pandas", "numpy", "scikit", "statistical model", "feature engineering"],
     "Data Scientist"),
    (["data analyst", "tableau", "power bi", "looker", "business intelligence", "sql analyst"],
     "Data Analyst"),
    (["devops", "kubernetes", "ci/cd", "terraform", "ansible", "jenkins", "helm", "site reliability"],
     "DevOps Engineer"),
    (["backend", "node.js", "django", "flask", "fastapi", "spring", "rails", "rest api", "microservices", "server-side"],
     "Backend Developer"),
    (["frontend", "react", "vue", "angular", "next.js", "tailwind", "css", "html", "ui developer"],
     "Frontend Developer"),
    (["full stack", "fullstack", "full-stack"],
     "Full Stack Developer"),
    (["android", "kotlin", "ios", "swift", "flutter", "react native", "mobile app"],
     "Mobile Developer"),
    (["security", "penetration", "cybersecurity", "soc", "siem", "appsec", "infosec"],
     "Security Engineer"),
    (["cloud", "aws", "gcp", "azure", "serverless", "cloud infrastructure"],
     "Cloud Engineer"),
    (["product manager", "product management", "roadmap", "stakeholder", "product owner"],
     "Product Manager"),
    (["ui/ux", "ux designer", "figma", "user research", "wireframe", "interaction design"],
     "UX Designer"),
    (["software engineer", "software developer", "sde", "swe"],
     "Software Engineer"),
]

def infer_role_from_resume(text: str) -> str:
    """
    Infer the most relevant job role from resume text using weighted keyword
    scoring.  Every matching keyword adds 1 point to its candidate role; the
    role with the highest score wins.  Falls back to 'Software Engineer' when
    there is a tie or no match.
    """
    text_lower = text.lower()
    scores: dict[str, int] = {}
    for keywords, role in ROLE_KEYWORD_MAP:
        hit_count = sum(1 for kw in keywords if kw in text_lower)
        if hit_count:
            scores[role] = scores.get(role, 0) + hit_count

    if not scores:
        return "Software Engineer"

    # Return the highest-scoring role; on a tie keep the earlier (more specific) entry.
    best_role = max(scores, key=lambda r: scores[r])
    return best_role


def parse_resume_sections(text: str) -> dict:
    """
    Parse resume text into structured sections.
    Returns a dict with: name, contact, summary, skills, experience, education, role.
    """
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    # ── Name (first non-empty line, usually) ─────────────────────────────────
    name = lines[0] if lines else "Unknown"

    # ── Contact info ──────────────────────────────────────────────────────────
    email_match    = re.search(r'[\w.+-]+@[\w-]+\.[a-z]{2,}', text, re.IGNORECASE)
    phone_match    = re.search(r'(\+?\d[\d\s\-().]{7,15}\d)', text)
    linkedin_match = re.search(r'linkedin\.com/in/[\w-]+', text, re.IGNORECASE)
    github_match   = re.search(r'github\.com/[\w-]+', text, re.IGNORECASE)

    contact = {
        "email":    email_match.group(0)    if email_match    else None,
        "phone":    phone_match.group(0)    if phone_match    else None,
        "linkedin": linkedin_match.group(0) if linkedin_match else None,
        "github":   github_match.group(0)   if github_match   else None,
    }

    # ── Skills extraction ─────────────────────────────────────────────────────
    SKILL_KEYWORDS = [
        # Languages
        "Python","JavaScript","TypeScript","Java","C++","C#","Go","Rust","Ruby","PHP","Swift","Kotlin","Scala","R",
        # Web
        "React","Vue","Angular","Next.js","Node.js","Express","Django","Flask","FastAPI","Spring","Rails","Laravel",
        # Data / ML
        "TensorFlow","PyTorch","scikit-learn","Pandas","NumPy","Keras","OpenCV","Hugging Face","LangChain",
        # Databases
        "MySQL","PostgreSQL","MongoDB","Redis","SQLite","Cassandra","DynamoDB","Elasticsearch","Supabase",
        # Cloud / DevOps
        "AWS","GCP","Azure","Docker","Kubernetes","Terraform","CI/CD","GitHub Actions","Jenkins","Ansible",
        # Tools
        "Git","Linux","REST","GraphQL","gRPC","Kafka","RabbitMQ","Nginx","Celery","SQLAlchemy","Prisma",
        # Other
        "Machine Learning","Deep Learning","NLP","LLM","API","Microservices","Agile","Scrum",
    ]
    found_skills = []
    text_lower = text.lower()
    for skill in SKILL_KEYWORDS:
        if skill.lower() in text_lower:
            found_skills.append(skill)

    # ── Experience years ──────────────────────────────────────────────────────
    exp_match = re.search(r'(\d+)\+?\s*year', text, re.IGNORECASE)
    years_exp = exp_match.group(1) if exp_match else None

    # ── Summary block ─────────────────────────────────────────────────────────
    summary = ""
    summary_pattern = re.search(
        r'(summary|profile|objective|about)[:\s]*\n?(.*?)(?=\n[A-Z][A-Z\s]{3,}|\Z)',
        text, re.IGNORECASE | re.DOTALL
    )
    if summary_pattern:
        summary = summary_pattern.group(2).strip()[:500]

    # ── Section detection (headings) ──────────────────────────────────────────
    SECTION_HEADERS = {
        "experience":     r'(work\s+)?experience|employment|career',
        "education":      r'education|academic|qualification',
        "projects":       r'projects?|portfolio|work',
        "certifications": r'certif|license|credential',
    }
    sections_found = []
    for section, pattern in SECTION_HEADERS.items():
        if re.search(pattern, text, re.IGNORECASE):
            sections_found.append(section)

    # ── Inferred role ─────────────────────────────────────────────────────────
    role = infer_role_from_resume(text)

    return {
        "name":        name,
        "contact":     contact,
        "skills":      found_skills,
        "years_exp":   years_exp,
        "summary":     summary,
        "sections":    sections_found,
        "total_chars": len(text),
        "word_count":  len(text.split()),
        "role":        role,   # ← NEW: used by app.py to set inferred_role
    }