"""cosmos_db_connector.py — 
Azure Cosmos DB connector: reads/writes users, resumes, job descriptions, and evaluations, with helper functions for diagnostics and debugging."""

import os
import re
import json
from typing import Optional
from urllib.parse import urlparse
from dotenv import load_dotenv
# ── SDK import ────────────────────────────────────────────────────────────────
try:
    from azure.cosmos import CosmosClient
    from azure.core.exceptions import ServiceRequestError, HttpResponseError
    _SDK = True
except ImportError:
    _SDK = False
    import requests as _req
    import hashlib, hmac, base64, urllib.parse
    from datetime import datetime, timezone


# ══════════════════════════════════════════════════════════════════════════════
# READ .env VARIABLES
# ══════════════════════════════════════════════════════════════════════════════

# def _env(key: str, default: str = "") -> str:
    # return os.environ.get(key, default).strip().strip('"').strip("'")
load_dotenv()

COSMOS_ENDPOINT         = os.getenv("COSMOS_ENDPOINT")
COSMOS_KEY              = os.getenv("COSMOS_KEY")
COSMOS_DATABASE         = os.getenv("COSMOS_DATABASE_NAME")
COSMOS_USERS_CONTAINER  = os.getenv("COSMOS_CONTAINER",        "users")
COSMOS_RESUME_CONTAINER = os.getenv("COSMOS_RESUME_CONTAINER")
COSMOS_JD_CONTAINER     = os.getenv("COSMOS_JD_CONTAINER")

# UUID pattern — matches anywhere inside a composite id string
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE
)


# ══════════════════════════════════════════════════════════════════════════════
# CONFIG VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

def _check_config():
    """Raise EnvironmentError if any required .env key is empty."""
    missing = []
    if not COSMOS_ENDPOINT:  missing.append("COSMOS_ENDPOINT")
    if not COSMOS_KEY:       missing.append("COSMOS_KEY")
    if not COSMOS_DATABASE:  missing.append("COSMOS_DATABASE_NAME")
    if missing:
        raise EnvironmentError(
            f"Missing Cosmos DB credentials in .env:\n"
            + "\n".join(f"  - {k}" for k in missing)
            + "\n\n"
            "  HOW TO FIX:\n"
            "  1. portal.azure.com -> search 'Azure Cosmos DB'\n"
            "  2. Open your account -> left menu -> 'Keys'\n"
            "  3. Copy URI -> COSMOS_ENDPOINT\n"
            "  4. Copy PRIMARY KEY -> COSMOS_KEY\n"
            "  5. Add COSMOS_DATABASE_NAME = your database name\n"
            "  6. Restart Flask"
        )


def check_endpoint_format():
    """
    Warn if COSMOS_ENDPOINT looks like a placeholder.
    Returns (is_valid: bool, message: str).
    """
    ep = COSMOS_ENDPOINT
    if not ep:
        return False, "COSMOS_ENDPOINT is empty"
    if "<" in ep or ">" in ep:
        return False, f"COSMOS_ENDPOINT still has placeholder text: '{ep}'"
    if not ep.startswith("https://"):
        return False, f"COSMOS_ENDPOINT must start with https://  Got: '{ep}'"
    if not ep.rstrip("/").endswith(":443") and "documents.azure.com" not in ep:
        return False, f"COSMOS_ENDPOINT looks wrong: '{ep}'"
    return True, f"COSMOS_ENDPOINT looks valid: '{ep}'"


# ══════════════════════════════════════════════════════════════════════════════
# LAZY SDK CLIENT  (created once, reused)
# ══════════════════════════════════════════════════════════════════════════════

_client_cache: Optional["CosmosClient"] = None


def _cosmos_host() -> str:
    ep = (COSMOS_ENDPOINT or "").strip()
    if not ep:
        return ""
    try:
        return (urlparse(ep).hostname or "").strip()
    except Exception:
        return ""


def _bypass_dead_local_proxy_for_cosmos() -> None:
    """
    Many local environments inherit a broken proxy like 127.0.0.1:9.
    Cosmos DB should bypass that proxy entirely.
    """
    host = _cosmos_host()
    if not host:
        return

    dead_proxy_markers = ("127.0.0.1:9", "localhost:9")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        value = (os.environ.get(key) or "").strip()
        if any(marker in value for marker in dead_proxy_markers):
            os.environ.pop(key, None)

    existing_no_proxy = [
        item.strip() for item in (os.environ.get("NO_PROXY") or "").split(",") if item.strip()
    ]
    for item in (host, "documents.azure.com", ".documents.azure.com"):
        if item and item not in existing_no_proxy:
            existing_no_proxy.append(item)
    if existing_no_proxy:
        os.environ["NO_PROXY"] = ",".join(existing_no_proxy)


def _sdk_client() -> "CosmosClient":
    """
    Return a cached CosmosClient.
    Created on first call; reused on subsequent calls.
    Raises a clear ConnectionError if the endpoint is wrong.
    """
    global _client_cache
    if _client_cache is not None:
        return _client_cache

    _check_config()

    valid, msg = check_endpoint_format()
    if not valid:
        raise EnvironmentError(
            f"Invalid COSMOS_ENDPOINT - {msg}\n\n"
            "  HOW TO FIX:\n"
            "  1. portal.azure.com -> search 'Azure Cosmos DB'\n"
            "  2. Open your account -> 'Keys'\n"
            "  3. Copy URI exactly as shown -> paste as COSMOS_ENDPOINT in .env\n"
            "  4. It should look like: https://myaccount.documents.azure.com:443/\n"
            "  5. Restart Flask"
        )

    _bypass_dead_local_proxy_for_cosmos()
    print(f"[CosmosDB] Connecting to: {COSMOS_ENDPOINT}")
    try:
        client = CosmosClient(url=COSMOS_ENDPOINT, credential=COSMOS_KEY)
        # Warm up: verify the database exists
        client.get_database_client(COSMOS_DATABASE).read()
        print(f"[CosmosDB] [OK] Connected to database '{COSMOS_DATABASE}'")
        _client_cache = client
        return client
    except ServiceRequestError as e:
        host = COSMOS_ENDPOINT.replace("https://", "").split(":")[0]
        raise ConnectionError(
            f"Cannot reach Cosmos DB: '{COSMOS_ENDPOINT}'\n"
            f"  DNS resolution failed for host: '{host}'\n\n"
            f"  HOW TO FIX:\n"
            f"  1. portal.azure.com -> search 'Azure Cosmos DB'\n"
            f"  2. Open your Cosmos DB account -> left menu -> 'Keys'\n"
            f"  3. Copy the URI field exactly -> paste as COSMOS_ENDPOINT in .env\n"
            f"  4. The URI looks like: https://<accountname>.documents.azure.com:443/\n"
            f"  5. Your current .env has: COSMOS_ENDPOINT={COSMOS_ENDPOINT}\n"
            f"  6. Restart Flask after saving .env\n\n"
            f"  SDK error: {e}"
        ) from e
    except HttpResponseError as e:
        if e.status_code == 401:
            raise PermissionError(
                f"Cosmos DB 401 Unauthorized - COSMOS_KEY is wrong.\n"
                f"  1. portal.azure.com -> your Cosmos account -> 'Keys'\n"
                f"  2. Copy PRIMARY KEY -> paste as COSMOS_KEY in .env\n"
                f"  3. Restart Flask"
            ) from e
        if e.status_code == 404:
            raise KeyError(
                f"Cosmos DB database '{COSMOS_DATABASE}' not found.\n"
                f"  Check COSMOS_DATABASE_NAME in .env.\n"
                f"  Use Azure Portal -> Data Explorer to see your database name."
            ) from e
        raise RuntimeError(f"Cosmos DB error {e.status_code}: {e.message}") from e


def _sdk_query(container_name: str, sql: str) -> list:
    """Run any SQL query cross-partition. Returns list of documents."""
    container = (
        _sdk_client()
        .get_database_client(COSMOS_DATABASE)
        .get_container_client(container_name)
    )
    try:
        return list(container.query_items(
            query=sql,
            enable_cross_partition_query=True
        ))
    except HttpResponseError as e:
        if e.status_code == 401:
            raise PermissionError("Cosmos DB 401 - check COSMOS_KEY in .env") from e
        if e.status_code == 404:
            raise KeyError(
                f"Container '{container_name}' not found in database '{COSMOS_DATABASE}'.\n"
                f"  Check COSMOS_RESUME_CONTAINER / COSMOS_JD_CONTAINER in .env."
            ) from e
        raise RuntimeError(f"Cosmos DB error {e.status_code}: {e}") from e


def _sdk_fetch(container_name: str, doc_id: str) -> dict:
    """
    Fetch one document by its composite id.

    Tries two SQL queries:
      1. WHERE c.id = '<full composite id>'   AJAY_BONGANE_9eeff9c6-...
      2. WHERE c.id = '<uuid only>'           9eeff9c6-...  (fallback)
    """
    safe = doc_id.replace("'", "\\'")
    print(f"[CosmosDB] SELECT FROM '{container_name}' WHERE c.id = '{doc_id}'")

    rows = _sdk_query(container_name, f"SELECT * FROM c WHERE c.id = '{safe}'")
    if rows:
        print(f"[CosmosDB] [OK] Found  id='{rows[0].get('id', doc_id)}'")
        return rows[0]

    # Fallback: try UUID portion only
    m = _UUID_RE.search(doc_id)
    if m and m.group(0) != doc_id:
        uuid_only = m.group(0)
        safe_uuid = uuid_only.replace("'", "\\'")
        print(f"[CosmosDB] Not found. Trying UUID only: '{uuid_only}'")
        rows = _sdk_query(container_name, f"SELECT * FROM c WHERE c.id = '{safe_uuid}'")
        if rows:
            print(f"[CosmosDB] [OK] Found via UUID  id='{rows[0].get('id', uuid_only)}'")
            return rows[0]

    raise KeyError(
        f"Document not found in Cosmos container '{container_name}'.\n"
        f"  Searched id : '{doc_id}'\n\n"
        f"  TROUBLESHOOTING:\n"
        f"  - GET /api/debug/resumes  - lists all resume IDs in your DB\n"
        f"  - GET /api/debug/jds      - lists all JD IDs in your DB\n"
        f"  - Paste the exact id shown there into the interview setup form"
    )


def _sdk_list(container_name: str, limit: int) -> list:
    return _sdk_query(container_name, f"SELECT * FROM c OFFSET 0 LIMIT {limit}")


# ══════════════════════════════════════════════════════════════════════════════
# REST FALLBACK (when azure-cosmos SDK is not installed)
# ══════════════════════════════════════════════════════════════════════════════

def _rest_auth(verb: str, resource_type: str, resource_link: str, date_str: str) -> str:
    s2s = "\n".join([verb.lower(), resource_type.lower(), resource_link,
                     date_str.lower(), "", ""])
    sig = hmac.new(base64.b64decode(COSMOS_KEY),
                   s2s.encode(), hashlib.sha256).digest()
    return urllib.parse.quote(
        f"type=master&ver=1.0&sig={base64.b64encode(sig).decode()}"
    )


def _rest_query(container_name: str, sql: str) -> list:
    _check_config()
    rlink    = f"dbs/{COSMOS_DATABASE}/colls/{container_name}"
    url      = f"{COSMOS_ENDPOINT.rstrip('/')}/{rlink}/docs"
    date_str = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
    headers  = {
        "Authorization":                    _rest_auth("POST", "docs", rlink, date_str),
        "x-ms-date":                        date_str,
        "x-ms-version":                     "2018-12-31",
        "x-ms-documentdb-isquery":          "true",
        "x-ms-query-enable-crosspartition": "true",
        "Content-Type":                     "application/query+json",
    }
    try:
        r = _req.post(url, headers=headers,
                      json={"query": sql, "parameters": []}, timeout=15)
    except _req.exceptions.ConnectionError as e:
        raise ConnectionError(
            f"Cannot reach Cosmos DB: {COSMOS_ENDPOINT}\n"
            f"  Check COSMOS_ENDPOINT in .env.  Error: {e}"
        ) from e
    if r.status_code == 401:
        raise PermissionError("Cosmos DB 401 - check COSMOS_KEY in .env")
    if r.status_code != 200:
        raise RuntimeError(f"Cosmos REST {r.status_code}: {r.text[:300]}")
    return r.json().get("Documents", [])


def _rest_fetch(container_name: str, doc_id: str) -> dict:
    safe = doc_id.replace("'", "\\'")
    print(f"[CosmosDB-REST] SELECT FROM '{container_name}' WHERE c.id = '{doc_id}'")
    rows = _rest_query(container_name, f"SELECT * FROM c WHERE c.id = '{safe}'")
    if rows:
        return rows[0]
    m = _UUID_RE.search(doc_id)
    if m and m.group(0) != doc_id:
        uuid_only = m.group(0)
        rows = _rest_query(container_name, f"SELECT * FROM c WHERE c.id = '{uuid_only}'")
        if rows:
            return rows[0]
    raise KeyError(
        f"Document not found in '{container_name}'  id='{doc_id}'\n"
        f"  -> GET /api/debug/resumes or /api/debug/jds to see real IDs"
    )


def _rest_list(container_name: str, limit: int) -> list:
    return _rest_query(container_name, f"SELECT * FROM c OFFSET 0 LIMIT {limit}")


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED WRAPPERS
# ══════════════════════════════════════════════════════════════════════════════

def _fetch(container_name: str, doc_id: str) -> dict:
    return _sdk_fetch(container_name, doc_id) if _SDK else _rest_fetch(container_name, doc_id)

def _list(container_name: str, limit: int = 100) -> list:
    return _sdk_list(container_name, limit) if _SDK else _rest_list(container_name, limit)

def _query(container_name: str, sql: str) -> list:
    return _sdk_query(container_name, sql) if _SDK else _rest_query(container_name, sql)


# ══════════════════════════════════════════════════════════════════════════════
# ID VALIDATION  (no hardcoded IDs — all IDs come from the request)
# ══════════════════════════════════════════════════════════════════════════════

def validate_id(raw: str, label: str = "ID") -> str:
    """
    Validate that `raw` contains a UUID somewhere (composite or pure UUID).
    Returns `raw` unchanged — the full string is the Cosmos document id.
    Raises ValueError with a clear message if no UUID is found.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError(f"{label} is empty.")
    if not _UUID_RE.search(raw):
        raise ValueError(
            f"Invalid {label}: '{raw}'\n"
            f"  It must contain a UUID  (xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx)\n"
            f"  Examples:\n"
            f"    AJAY_BONGANE_9eeff9c6-720c-4e6e-ac29-f2f81e24f9ad\n"
            f"    Data_Scientist_007da85d-73a5-45d9-ad90-311769687924"
        )
    return raw


# ══════════════════════════════════════════════════════════════════════════════
# SCHEMA HELPER
# ══════════════════════════════════════════════════════════════════════════════


def _as_dict(value) -> dict:
    """
    Ensure `value` is a dict.

    Cosmos sometimes stores nested objects as JSON strings instead of
    embedded objects. This function handles both cases:
      - Already a dict  → returned as-is
      - A JSON string   → parsed and returned as dict
      - Anything else   → empty dict returned (safe default)

    Example issue this fixes:
      raw["metadata"] == '{"candidate_name": "John", "extracted_data": {...}}'
      instead of
      raw["metadata"] == {"candidate_name": "John", "extracted_data": {...}}
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("{"):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                pass
    return {}


def _as_list(value) -> list:
    """
    Ensure `value` is a list.
    Handles: already a list, JSON string containing a list, or anything else → [].
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("["):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
    return []


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — RESUME
# ══════════════════════════════════════════════════════════════════════════════

def fetch_resume(resume_id: str) -> dict:
    """
    Fetch a resume document from Cosmos DB.

    Args:
        resume_id — the document id exactly as stored in Cosmos.
                    Example: "AJAY_BONGANE_9eeff9c6-720c-4e6e-ac29-f2f81e24f9ad"

    Schema (from your Cosmos container):
        doc["metadata"]["candidate_name"]
        doc["metadata"]["extracted_data"]["contact"]
        doc["metadata"]["extracted_data"]["skills"]
        doc["metadata"]["extracted_data"]["experience"]
        doc["metadata"]["extracted_data"]["education"]
        doc["metadata"]["extracted_data"]["projects"]
        doc["metadata"]["extracted_data"]["certifications"]
        doc["metadata"]["extracted_data"]["other"]
        doc["metadata"]["extracted_data"]["summary"]
        doc["metadata"]["extracted_data"]["total_experience_display"]

    Returns a JSON-serializable dict with all resume fields plus
    "resume_text" (formatted plain-text string for the AI interviewer).
    """
    doc_id   = validate_id(resume_id, "Resume ID")
    raw      = _fetch(COSMOS_RESUME_CONTAINER, doc_id)

    # metadata may be a nested dict OR a JSON string — _as_dict handles both
    metadata = _as_dict(raw.get("metadata", {}))
    exdata   = _as_dict(metadata.get("extracted_data", {}))

    candidate_name = metadata.get("candidate_name", "Unknown Candidate")
    contact        = _as_dict(exdata.get("contact", {}))
    skills         = _as_list(exdata.get("skills", []))
    experience     = _as_list(exdata.get("experience", []))
    education      = _as_list(exdata.get("education", []))
    projects       = _as_list(exdata.get("projects", []))
    certifications = _as_list(exdata.get("certifications", []))
    other          = _as_dict(exdata.get("other", {}))
    summary        = exdata.get("summary", "") or ""

    resume_text = _build_resume_text(
        name=candidate_name, contact=contact, summary=summary,
        skills=skills, experience=experience, education=education,
        projects=projects, certifications=certifications,
        exp_display=exdata.get("total_experience_display", ""),
    )

    print(f"[CosmosDB] Resume loaded  candidate='{candidate_name}'  "
          f"skills={len(skills)}  exp={len(experience)}  projects={len(projects)}")

    return {
        "id":                        raw.get("id", doc_id),
        "candidate_name":            candidate_name,
        "contact":                   contact,
        "summary":                   summary,
        "skills":                    skills,
        "experience":                experience,
        "education":                 education,
        "projects":                  projects,
        "certifications":            certifications,
        "languages":                 other.get("languages", []),
        "hobbies":                   other.get("hobbies", []),
        "total_experience_months":   exdata.get("total_experience_months", 0),
        "total_experience_years":    exdata.get("total_experience_years", 0.0),
        "total_experience_display":  exdata.get("total_experience_display", ""),
        "resume_text":               resume_text,
        "raw":                       raw,
    }


def list_all_resumes(limit: int = 100) -> list:
    """
    List all resume documents from Cosmos (up to `limit`).
    Returns: [ { "id", "candidate_name", "source_file" }, ... ]
    Use /api/debug/resumes to call this and see what IDs exist.
    """
    return [
        {
            "id":             d.get("id", ""),
            "candidate_name": _as_dict(d.get("metadata", {})).get("candidate_name", "Unknown"),
            "source_file":    _as_dict(d.get("metadata", {})).get("source_file", ""),
        }
        for d in _list(COSMOS_RESUME_CONTAINER, limit)
    ]


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — JOB DESCRIPTION
# ══════════════════════════════════════════════════════════════════════════════

def fetch_jd(jd_id: str) -> dict:
    """
    Fetch a JD document from Cosmos DB.

    Args:
        jd_id — the document id exactly as stored in Cosmos.
                 Example: "Data_Scientist_007da85d-73a5-45d9-ad90-311769687924"

    Returns a JSON-serializable dict with all JD fields plus
    "jd_text" (formatted string for the AI interviewer).
    """
    doc_id   = validate_id(jd_id, "JD ID")
    raw      = _fetch(COSMOS_JD_CONTAINER, doc_id)
    # metadata may be a nested dict OR a JSON string — _as_dict handles both
    metadata = _as_dict(raw.get("metadata", {}))

    job_title        = metadata.get("title", "Open Position") or "Open Position"
    raw_text         = metadata.get("raw_text", "") or ""
    required_skills  = _as_list(metadata.get("required_skills", []))
    nice_to_have     = _as_list(metadata.get("nice_to_have_skills", []))
    responsibilities = _as_list(metadata.get("responsibilities", []))
    req_years        = metadata.get("required_years_experience")
    pref_years       = metadata.get("preferred_years_experience")
    seniority        = metadata.get("seniority", "") or ""
    domain           = metadata.get("domain", "") or ""
    edu_req          = metadata.get("education_requirement", "") or ""
    location         = metadata.get("location", "") or ""

    jd_text = (raw_text.strip() if raw_text and raw_text.strip()
               else _build_jd_text(
                   title=job_title, seniority=seniority, domain=domain,
                   location=location, required_skills=required_skills,
                   nice_to_have=nice_to_have, responsibilities=responsibilities,
                   req_years=req_years, pref_years=pref_years, edu_req=edu_req,
               ))

    # recruiter_comments is a top-level optional field on the JD document.
    # It is None when the recruiter left no comment at upload time.
    recruiter_comments = raw.get("recruiter_comments") or None

    avatar = raw.get("avatar") or {}

    print(f"[CosmosDB] JD loaded  title='{job_title}'  "
          f"required_skills={len(required_skills)}  "
          f"recruiter_comments={'yes' if recruiter_comments else 'none'}")

    return {
        "id":                         raw.get("id", doc_id),
        "job_title":                  job_title,
        "seniority":                  seniority,
        "domain":                     domain,
        "location":                   location,
        "required_years_experience":  req_years,
        "preferred_years_experience": pref_years,
        "education_requirement":      edu_req,
        "required_skills":            required_skills,
        "nice_to_have_skills":        nice_to_have,
        "responsibilities":           responsibilities,
        "raw_text":                   raw_text,
        "jd_text":                    jd_text,
        "recruiter_comments":         recruiter_comments,
        "avatar":                     avatar,
        "raw":                        raw,
    }


def list_all_jds(limit: int = 100) -> list:
    """
    List all JD documents from Cosmos (up to `limit`).
    Returns: [ { "id", "title", "source_file" }, ... ]
    Use /api/debug/jds to call this and see what IDs exist.
    """
    return [
        {
            "id":          d.get("id", ""),
            "title":       _as_dict(d.get("metadata", {})).get("title", "Unknown"),
            "source_file": _as_dict(d.get("metadata", {})).get("source_file", ""),
        }
        for d in _list(COSMOS_JD_CONTAINER, limit)
    ]


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API — USERS
# ══════════════════════════════════════════════════════════════════════════════

def fetch_user(user_id: str) -> dict:
    """Fetch a user document by id. Returns the raw Cosmos document."""
    doc_id = validate_id(user_id, "User ID")
    return _fetch(COSMOS_USERS_CONTAINER, doc_id)


def list_all_users(limit: int = 100) -> list:
    """List all users. Returns raw Cosmos documents."""
    return _list(COSMOS_USERS_CONTAINER, limit)


# ══════════════════════════════════════════════════════════════════════════════
# COMBINED FETCHER  ← called by main.py
# ══════════════════════════════════════════════════════════════════════════════

def fetch_resume_with_best_jd(resume_id: str) -> dict:
    """
    Fetch a resume by ID, then automatically select the best-matching JD
    from the JD container based on skill overlap with the resume.

    No jd_id is needed from the caller — the JD is resolved automatically.

    Selection strategy:
      1. Fetch the resume.
      2. List all JDs (up to 100).
      3. Score each JD by counting how many of its required_skills appear
         in the candidate's resume text (case-insensitive).
      4. Pick the highest-scoring JD.  Ties broken by whichever appears first.
      5. If no JDs exist, raises KeyError with a clear message.

    Returns the same dict shape as fetch_resume_and_jd() so the rest of
    main.py (create_session, etc.) needs no changes.
    """
    resume_data = fetch_resume(resume_id)
    resume_text_lower = resume_data["resume_text"].lower()

    # List all JDs — metadata only (light query)
    jd_docs = _list(COSMOS_JD_CONTAINER, limit=100)
    if not jd_docs:
        raise KeyError(
            "No job descriptions found in Cosmos DB.\n"
            "  Make sure COSMOS_JD_CONTAINER is set correctly in .env and "
            "that at least one JD document exists."
        )

    # Score each JD by skill overlap
    best_doc  = None
    best_score = -1
    for doc in jd_docs:
        meta     = _as_dict(doc.get("metadata", {}))
        skills   = _as_list(meta.get("required_skills", []))
        # also check raw_text for any keyword hits
        raw_text = (meta.get("raw_text", "") or "").lower()
        score    = sum(
            1 for s in skills
            if s.lower() in resume_text_lower or s.lower() in raw_text
        )
        if score > best_score:
            best_score = score
            best_doc   = doc

    # Fetch the full best JD document
    best_jd_id = best_doc.get("id", "")
    print(f"[CosmosDB] Auto-selected JD  id='{best_jd_id}'  skill_overlap={best_score}")
    jd_data = fetch_jd(best_jd_id)

    return {
        "candidate_name":  resume_data["candidate_name"],
        "job_title":       jd_data["job_title"],
        "resume":          resume_data["resume_text"],
        "job_description": jd_data["jd_text"],
        "resume_data":     resume_data,
        "jd_data":         jd_data,
        "avatar":          resume_data["raw"].get("avatar", {}),
    }


def fetch_resume_and_jd(resume_id: str, jd_id: str) -> dict:
    """
    Fetch both resume and JD from Cosmos DB in one call.

    IDs come from the HTTP request — nothing is hardcoded.
    Example:
        resume_id = "AJAY_BONGANE_9eeff9c6-720c-4e6e-ac29-f2f81e24f9ad"
        jd_id     = "Data_Scientist_007da85d-73a5-45d9-ad90-311769687924"

    Returns the dict that create_session() in main.py expects:
        {
          "candidate_name":  str,
          "job_title":       str,
          "resume":          str,   # formatted resume text for the AI
          "job_description": str,   # formatted JD text for the AI
          "resume_data":     dict,  # full fetch_resume() result
          "jd_data":         dict,  # full fetch_jd() result
        }
    """
    resume_data = fetch_resume(resume_id)
    jd_data     = fetch_jd(jd_id)
    return {
        "candidate_name":  resume_data["candidate_name"],
        "job_title":       jd_data["job_title"],
        "resume":          resume_data["resume_text"],
        "job_description": jd_data["jd_text"],
        "resume_data":     resume_data,
        "jd_data":         jd_data,
        "avatar":          resume_data["raw"].get("avatar", {}),
    }


def fetch_resume_with_linked_jd(resume_id: str) -> dict:
    """
    Fetch a resume by ID, then fetch the JD using the ``jd_id`` field
    that is stored directly on the resume document.

    Resume schema (top-level field):
        {
          "id":    "Aashay_Vaidya_f6b8b2e8-...",
          "jd_id": "Senior_Data_Scientist_7a8b9c0d-...",   ← used here
          "metadata": { ... },
          ...
        }

    Raises:
        KeyError  — if the resume has no ``jd_id`` field or it is empty
        KeyError  — if the JD document cannot be found
        ValueError — if resume_id is invalid
    """
    resume_data = fetch_resume(resume_id)
    raw_doc     = resume_data["raw"]

    # Read jd_id from the top-level resume document
    jd_id = (raw_doc.get("jd_id") or "").strip()
    if not jd_id:
        raise KeyError(
            f"Resume '{resume_id}' has no 'jd_id' field.\n"
            f"  Make sure the resume document in Cosmos DB contains a top-level "
            f"'jd_id' field pointing to the matched job description."
        )

    print(f"[CosmosDB] Resume '{resume_id}' -> linked jd_id='{jd_id}'")
    jd_data = fetch_jd(jd_id)

    # recruiter_comment_id is optional — None when no comment was added at JD upload time.
    recruiter_comment_id = jd_data.get("recruiter_comments") or None

    return {
        "candidate_name":       resume_data["candidate_name"],
        "job_title":            jd_data["job_title"],
        "resume":               resume_data["resume_text"],
        "job_description":      jd_data["jd_text"],
        "resume_data":          resume_data,
        "jd_data":              jd_data,
        "jd_id":                jd_id,
        "recruiter_comment_id": recruiter_comment_id,
        "avatar":               resume_data["raw"].get("avatar", {}),
    }


# ══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTIC  ← used by /api/healthcheck and /api/debug/* in main.py
# ══════════════════════════════════════════════════════════════════════════════

def diagnose_cosmos(container_name: str, search_id: Optional[str] = None) -> dict:
    """
    Health check for a Cosmos container.
    Returns sample ids and optionally tests a specific document lookup.
    """
    valid_ep, ep_msg = check_endpoint_format()
    report: dict = {
        "container":         container_name,
        "database":          COSMOS_DATABASE,
        "endpoint":          COSMOS_ENDPOINT,
        "endpoint_valid":    valid_ep,
        "endpoint_message":  ep_msg,
        "sdk_available":     _SDK,
        "sample_docs":       [],
        "id_search_result":  None,
        "errors":            [],
    }

    try:
        for d in _list(container_name, 5):
            report["sample_docs"].append({
                "id":    d.get("id", "-"),
                "label": (_as_dict(d.get("metadata", {})).get("candidate_name")
                          or _as_dict(d.get("metadata", {})).get("title") or "-"),
            })
    except Exception as exc:
        report["errors"].append(str(exc))

    if search_id:
        try:
            doc_id = validate_id(search_id, "search_id")
            doc    = _fetch(container_name, doc_id)
            report["id_search_result"] = {
                "found":       True,
                "document_id": doc.get("id", ""),
                "top_fields":  [k for k in doc if not k.startswith("_")][:8],
            }
        except (ValueError, KeyError, EnvironmentError, ConnectionError) as exc:
            report["id_search_result"] = {"found": False, "reason": str(exc)}
        except Exception as exc:
            report["id_search_result"] = {"found": False, "error": str(exc)}

    return report


# ══════════════════════════════════════════════════════════════════════════════
# TEXT FORMATTERS
# ══════════════════════════════════════════════════════════════════════════════

def _build_resume_text(name, contact, summary, skills, experience,
                       education, projects, certifications, exp_display) -> str:
    L = []
    L.append(name)
    if contact.get("email"):    L.append(f"Email    : {contact['email']}")
    if contact.get("phone"):    L.append(f"Phone    : {contact['phone']}")
    if contact.get("location"): L.append(f"Location : {contact['location']}")
    if contact.get("linkedin"): L.append(f"LinkedIn : {contact['linkedin']}")
    if exp_display:             L.append(f"Total Exp: {exp_display}")
    L.append("")
    if summary:
        L.append("Summary:")
        L.append(f"  {summary}")
        L.append("")
    if skills:
        L.append("Skills:")
        L.append("  " + ", ".join(skills))
        L.append("")
    if education:
        L.append("Education:")
        for e in education:
            line = f"  {e.get('degree','')}"
            if e.get("institution"):     line += f", {e['institution']}"
            if e.get("graduation_year"): line += f" ({e['graduation_year']})"
            if e.get("score_value") and e.get("score_type"):
                line += f" — {e['score_type']}: {e['score_value']}"
            L.append(line)
        L.append("")
    if experience:
        L.append("Experience:")
        for e in experience:
            end    = "Present" if e.get("is_present") else (e.get("end_date") or "")
            header = f"  {e.get('title','')}"
            if e.get("company"):          header += f" — {e['company']}"
            if e.get("start_date"):       header += f" ({e['start_date']}"
            if end:                       header += f" – {end}"
            if e.get("start_date"):       header += ")"
            if e.get("duration_display"): header += f" [{e['duration_display']}]"
            L.append(header)
            for desc in e.get("description", []):
                L.append(f"    • {desc}")
        L.append("")
    if projects:
        L.append("Projects:")
        for p in projects:
            L.append(f"  • {p.get('name','')}")
            if p.get("description"):  L.append(f"    {p['description']}")
            if p.get("technologies"): L.append(f"    Tech: {', '.join(p['technologies'])}")
        L.append("")
    if certifications:
        L.append("Certifications:")
        for c in certifications:
            if isinstance(c, dict):
                parts = [c.get("name","")]
                if c.get("issuer"): parts.append(c["issuer"])
                if c.get("year"):   parts.append(str(c["year"]))
                L.append("  • " + " — ".join(p for p in parts if p))
            else:
                L.append(f"  • {c}")
        L.append("")
    return "\n".join(L).strip()


def _build_jd_text(title, seniority, domain, location, required_skills,
                   nice_to_have, responsibilities, req_years, pref_years, edu_req) -> str:
    L = []
    L.append(title)
    if seniority: L.append(f"Seniority : {seniority}")
    if domain:    L.append(f"Domain    : {domain}")
    if location:  L.append(f"Location  : {location}")
    L.append("")
    if req_years is not None:
        exp = f"Required: {req_years} yrs"
        if pref_years: exp += f"  |  Preferred: {pref_years} yrs"
        L.append(f"Experience: {exp}")
    if edu_req: L.append(f"Education : {edu_req}")
    L.append("")
    if required_skills:
        L.append("Required Skills:")
        L.append("  " + ", ".join(required_skills))
        L.append("")
    if nice_to_have:
        L.append("Nice-to-Have:")
        L.append("  " + ", ".join(nice_to_have))
        L.append("")
    if responsibilities:
        L.append("Responsibilities:")
        for r in responsibilities:
            L.append(f"  • {r}")
        L.append("")
    return "\n".join(L).strip()