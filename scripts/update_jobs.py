#!/usr/bin/env python3
"""Fetch public ATS jobs and merge them with manually maintained jobs.

Only public Greenhouse and Lever endpoints are used.  The script never logs in,
uses cookies, or retries indefinitely; an unavailable source is reported and
does not prevent the remaining sources from being processed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import requests
except ImportError:  # Allows a local/manual-only refresh before dependencies are installed.
    requests = None  # type: ignore[assignment]


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
JOBS_FILE = DATA_DIR / "jobs.json"
MANUAL_JOBS_FILE = DATA_DIR / "manual_jobs.json"
SOURCES_FILE = Path(__file__).resolve().parent / "sources.json"
REQUEST_TIMEOUT = 20

SCHEMA_FIELDS = (
    "id", "company", "title", "city", "location", "direction",
    "medicalPhdFit", "degree", "major", "experience", "salary", "date",
    "source", "sourceType", "sourceJobId", "verified", "summary", "whyFit",
    "skillsMatch", "careerPath", "url", "discoveryUrl", "firstSeen",
    "discoverySource", "lastSeen", "tags",
)

# Ordered from most specific to more general.  These values exactly match the
# directions exposed by app.js; title matching always happens before fallback
# matching against a description.
DIRECTION_RULES = (
    ("Medical Science Liaison", "MSL", (r"\bmedical science liaison\b", r"\bmsl\b")),
    ("Clinical Research Physician", "Clinical Research Physician", (r"\bclinical research physician\b",)),
    ("Medical Advisor", "Medical Advisor", (r"\bmedical advisor\b",)),
    ("Clinical Pharmacology", "Clinical Pharmacology", (r"\bclinical pharmacology\b",)),
    ("Clinical Scientist", "Clinical Scientist", (r"\bclinical scientist\b",)),
    ("Clinical Development", "Clinical Development", (r"\bclinical development\b",)),
    ("Translational Medicine", "Translational Medicine", (r"\btranslational medicine\b", r"\btranslational\b", "转化医学")),
    ("Biomarker", "Biomarker", (r"\bbiomarker\b", r"\bcompanion diagnostic\b")),
    ("Regulatory Affairs", "Regulatory Affairs", (r"\bregulatory affairs\b",)),
    ("Pharmacovigilance", "Pharmacovigilance", (r"\bpharmacovigilance\b", r"\bdrug safety\b")),
    ("Medical Writing", "Medical Writing", (r"\bmedical writing\b", r"\bmedical writer\b", r"\bscientific writer\b")),
    ("Healthcare Consulting", "Healthcare Consulting", (r"\bhealthcare consulting\b", r"\bhealthcare (?:strategy )?consultant\b", r"\blife sciences? consulting\b")),
    ("Business Development", "Business Development", (r"\bbusiness development\b", r"\bbd manager\b")),
    ("Medical Affairs", "Medical Affairs", (r"\bmedical affairs\b",)),
)

# Medical PhD fit is intentionally deterministic.  Title signals carry the
# largest weight; explicit degree/qualification signals come next, while the
# description can only add supporting points.  The lists use lowercase text.
TARGET_TITLE_RULES = (
    ("Clinical Research Physician", (r"\bclinical research physician\b",), 22),
    ("Clinical Scientist", (r"\bclinical scientist\b",), 20),
    ("Clinical Development", (r"\bclinical development\b",), 20),
    ("Medical Affairs", (r"\bmedical affairs\b",), 18),
    ("Medical Advisor", (r"\bmedical advisor\b",), 18),
    ("Medical Science Liaison", (r"\bmedical science liaison\b", r"\bmsl\b"), 18),
    ("Translational Medicine", (r"\btranslational medicine\b", r"\btranslational\b", "转化医学"), 18),
    ("Biomarker", (r"\bbiomarker\b",), 16),
    ("Precision Medicine", (r"\bprecision medicine\b",), 16),
    ("Clinical Pharmacology", (r"\bclinical pharmacology\b",), 16),
    ("Pharmacovigilance", (r"\bpharmacovigilance\b", r"\bdrug safety\b"), 14),
    ("Regulatory Affairs", (r"\bregulatory affairs\b",), 14),
    ("Medical Writing", (r"\bmedical writing\b", r"\bmedical writer\b"), 14),
)

THERAPEUTIC_RULES = (
    ("Oncology", (r"\boncology\b", "肿瘤")),
    ("Hematology", (r"\bhematology\b", "血液")),
    ("Immunology", (r"\bimmunology\b", "免疫")),
)

QUALIFICATION_RULES = (
    ("PhD", (r"\bph\.?d\b", r"\bdoctorate\b", r"\bdoctoral\b", "博士"), 14),
    ("MD/Medical Degree", (r"\bmd\b", r"\bmedical degree\b", r"\bclinical medicine\b", "临床医学"), 12),
)

NEGATIVE_ROLE_RULES = (
    ("Sales Representative", (r"\bsales representative\b",)),
    ("Administrative Assistant", (r"\badministrative assistant\b",)),
    ("Production Operator", (r"\bproduction operator\b",)),
    ("Technician", (r"\btechnician\b",)),
    ("Manufacturing Operator", (r"\bmanufacturing operator\b",)),
    ("Receptionist", (r"\breceptionist\b",)),
)


def warn(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def read_json_list(path: Path) -> List[Dict[str, Any]]:
    """Return a JSON array of objects, treating absent or bad inputs as empty."""
    if not path.exists():
        warn(f"{path.relative_to(ROOT)} does not exist; treating it as an empty list")
        return []
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        warn(f"could not read {path.relative_to(ROOT)}: {error}; treating it as empty")
        return []
    if not isinstance(value, list):
        warn(f"{path.relative_to(ROOT)} must contain a JSON array; treating it as empty")
        return []
    return [item for item in value if isinstance(item, dict)]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(filter(None, (clean_text(item) for item in value)))
    return str(value).strip()


def is_sample_job(raw: Dict[str, Any]) -> bool:
    """Identify development-only SAMPLE records so they never reach jobs.json."""
    if raw.get("sample") is True:
        return True
    company = clean_text(raw.get("company")).casefold()
    source = clean_text(raw.get("source")).casefold()
    tags = raw.get("tags", [])
    if not isinstance(tags, list):
        tags = [tags]
    return (
        company.startswith("sample ")
        or source in {"示例数据", "sample data"}
        or any(clean_text(tag).casefold() == "sample" for tag in tags)
    )


def html_to_text(value: Any) -> str:
    text = unescape(clean_text(value))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def city_from(*values: Any) -> str:
    combined = " ".join(clean_text(value) for value in values).casefold()
    if "上海" in combined or "shanghai" in combined:
        return "上海"
    if "苏州" in combined or "suzhou" in combined:
        return "苏州"
    return ""


def stable_id(*parts: Any) -> str:
    text = "|".join(clean_text(part).casefold() for part in parts if clean_text(part))
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]
    return f"job-{digest}"


def match_direction(text: str) -> str:
    """Return the first matching supported direction for one text field."""
    normalized = clean_text(text).casefold()
    for _, direction, patterns in DIRECTION_RULES:
        if any(re.search(pattern, normalized, re.IGNORECASE) for pattern in patterns):
            return direction
    return ""


def classify_direction(title: str, description: str = "") -> str:
    """Classify by title first; use description only when title has no signal."""
    return match_direction(title) or match_direction(description) or "Other"


def extract_degree(text: str, existing: str = "") -> str:
    lowered = text.casefold()
    if re.search(r"\b(ph\.?d|doctoral|doctorate)\b|博士", lowered):
        return "PhD/博士"
    if re.search(r"\b(md|mbbs)\b|临床医学", lowered):
        return "MD/临床医学"
    if re.search(r"\b(master'?s|msc|ms)\b|硕士", lowered):
        return "硕士"
    if re.search(r"\b(bachelor'?s|bsc|bs)\b|本科", lowered):
        return "本科"
    return clean_text(existing)


def extract_major(text: str, existing: str = "") -> str:
    matches = []
    labels = (
        ("医学", ("medical", "medicine", "医学")),
        ("药学", ("pharmacy", "pharmaceutical", "药学")),
        ("生命科学", ("life science", "biology", "生物")),
        ("免疫学", ("immunology", "免疫")),
        ("生物统计", ("biostatistics", "生物统计")),
        ("生物信息学", ("bioinformatics", "computational biology", "生物信息")),
    )
    lowered = text.casefold()
    for label, keywords in labels:
        if any(keyword in lowered for keyword in keywords):
            matches.append(label)
    return "、".join(matches) if matches else clean_text(existing)


def extract_experience(text: str, existing: str = "") -> str:
    match = re.search(r"(?:至少|至少需要|minimum of|at least|over)?\s*(\d+(?:\s*[-–]\s*\d+)?\+?\s*(?:years?|年))", text, re.IGNORECASE)
    if match:
        return f"{match.group(1).replace('years', '年').replace('year', '年')} 相关经验"
    return clean_text(existing)


def matched_labels(text: str, rules: Iterable[tuple[str, Iterable[str], Any]]) -> List[tuple[str, Any]]:
    """Return rule labels found in text, retaining each rule's weight/value."""
    lowered = clean_text(text).casefold()
    matches = []
    for label, patterns, value in rules:
        if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in patterns):
            matches.append((label, value))
    return matches


def score_medical_phd_fit(
    title: str,
    degree: str = "",
    qualifications: str = "",
    description: str = "",
) -> tuple[str, str]:
    """Return an A--D Medical PhD fit grade and rule-generated explanation.

    A requires both a target title (at least 14 title points) and an explicit
    PhD/MD/medical-degree qualification.  Therefore, a standalone mention of
    PhD can never receive A.  Obvious non-target titles always receive D.
    """
    title_text = clean_text(title)
    qualification_text = f"{clean_text(degree)} {clean_text(qualifications)}"
    description_text = clean_text(description)

    negative_title_hits = matched_labels(title_text, ((label, patterns, 0) for label, patterns in NEGATIVE_ROLE_RULES))
    if negative_title_hits:
        labels = "、".join(label for label, _ in negative_title_hits)
        return "D", f"职位名称命中 {labels} 等明显非目标岗位规则；即使出现博士或医学关键词，仍判为低相关。"

    title_role_hits = matched_labels(title_text, TARGET_TITLE_RULES)
    title_therapy_hits = matched_labels(title_text, ((label, patterns, 5) for label, patterns in THERAPEUTIC_RULES))
    qualification_hits = matched_labels(qualification_text, QUALIFICATION_RULES)
    description_role_hits = matched_labels(description_text, ((label, patterns, 7) for label, patterns, _ in TARGET_TITLE_RULES))
    description_therapy_hits = matched_labels(description_text, ((label, patterns, 4) for label, patterns in THERAPEUTIC_RULES))
    negative_description_hits = matched_labels(description_text, ((label, patterns, -10) for label, patterns in NEGATIVE_ROLE_RULES))

    # Use the strongest title and qualification signal rather than stacking
    # synonyms. Description is capped at 11 points so it remains supplemental.
    title_role_score = max((value for _, value in title_role_hits), default=0)
    title_therapy_score = max((value for _, value in title_therapy_hits), default=0)
    qualification_score = max((value for _, value in qualification_hits), default=0)
    description_score = min(
        max((value for _, value in description_role_hits), default=0)
        + max((value for _, value in description_therapy_hits), default=0),
        11,
    )
    negative_score = sum(value for _, value in negative_description_hits)
    total = title_role_score + title_therapy_score + qualification_score + description_score + negative_score

    if title_role_score >= 14 and qualification_score >= 12 and total >= 30:
        grade, conclusion = "A", "因此与医学博士背景匹配度较高。"
    elif total >= 20:
        grade, conclusion = "B", "因此与医学博士背景相关。"
    elif total >= 8:
        grade, conclusion = "C", "但医学博士要求或目标职能信号有限，因此可能适合。"
    else:
        grade, conclusion = "D", "公开信息中的医学博士匹配信号不足，因此相关性较低。"

    reasons = []
    if qualification_hits:
        reasons.append(f"资格要求命中 {'、'.join(label for label, _ in qualification_hits)}")
    if title_role_hits:
        reasons.append(f"职位名称命中 {'、'.join(label for label, _ in title_role_hits)}")
    if title_therapy_hits or description_therapy_hits:
        therapeutic = list(dict.fromkeys(label for label, _ in title_therapy_hits + description_therapy_hits))
        reasons.append(f"涉及 {'、'.join(therapeutic)}")
    if negative_description_hits:
        reasons.append(f"描述中出现 {'、'.join(label for label, _ in negative_description_hits)}，已降权")
    evidence = "，".join(reasons) if reasons else "未命中明确的目标岗位、资格或疾病领域规则"
    return grade, f"{evidence}（规则得分 {total}）。{conclusion}"


def fit_details(title: str, description: str, degree: str, major: str, experience: str, direction: str) -> tuple[str, str, str, str, List[str]]:
    score, reason = score_medical_phd_fit(title, degree, f"{major} {experience}", description)
    haystack = f"{title} {description} {degree} {major}".casefold()
    has_research = any(term in haystack for term in ("scientist", "research", "discovery", "biomarker", "translational", "研究", "科学家", "研发"))
    tags = [direction]
    if re.search(r"\bph\.?d\b|doctoral|doctorate|博士", haystack):
        tags.append("PhD")
    if "clinical" in haystack or "临床" in haystack:
        tags.append("临床")
    if "immun" in haystack or "免疫" in haystack:
        tags.append("免疫学")
    skills = "科研分析、跨团队协作" if has_research else "岗位描述待进一步核实"
    career_path = f"可作为向 {direction} 方向发展的岗位。" if direction != "Other" else "建议结合具体职责评估后续职业路径。"
    return score, reason, skills, career_path, list(dict.fromkeys(tags))


def parse_date(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    try:
        if isinstance(value, (int, float)) or text.isdigit():
            timestamp = float(value)
            if timestamp > 10_000_000_000:
                timestamp /= 1000
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat()
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        return match.group(0) if match else ""


def normalize_job(raw: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Convert a manual, legacy, Greenhouse, or Lever record into the shared schema."""
    if is_sample_job(raw):
        return None
    defaults = defaults or {}
    description = html_to_text(raw.get("description") or raw.get("content") or raw.get("summary"))
    title = clean_text(raw.get("title") or raw.get("text"))
    company = clean_text(raw.get("company") or defaults.get("company"))
    location = clean_text(raw.get("location") or raw.get("locations") or raw.get("workplaceType"))
    city = city_from(raw.get("city"), location)
    if not city:
        return None

    source_type = clean_text(raw.get("sourceType") or defaults.get("sourceType") or "manual").lower()
    source_job_id = clean_text(raw.get("sourceJobId") or raw.get("id") or defaults.get("sourceJobId"))
    url = clean_text(raw.get("url") or raw.get("hostedUrl") or raw.get("applyUrl"))
    source = clean_text(raw.get("source") or defaults.get("source") or "Manual")
    direction = classify_direction(title, description)
    degree = clean_text(raw.get("degree")) or extract_degree(f"{title} {description}")
    major = clean_text(raw.get("major")) or extract_major(f"{title} {description}")
    experience = clean_text(raw.get("experience")) or extract_experience(f"{title} {description}")
    fit, why_fit, skills_match, career_path, generated_tags = fit_details(title, description, degree, major, experience, direction)
    rating = fit
    supplied_tags = raw.get("tags", [])
    if not isinstance(supplied_tags, list):
        supplied_tags = [supplied_tags]
    tags = list(dict.fromkeys(
        clean_text(tag) for tag in supplied_tags + generated_tags + [rating]
        if clean_text(tag) and clean_text(tag) not in {"A", "B", "C", "D"}
    ))
    tags.append(rating)

    record = {field: "" for field in SCHEMA_FIELDS}
    record.update({
        "id": clean_text(raw.get("id")) or stable_id(source_type, company, source_job_id, url, title, location),
        "company": company,
        "title": title,
        "city": city,
        "location": location,
        "direction": direction,
        "medicalPhdFit": rating,
        "degree": degree,
        "major": major,
        "experience": experience,
        "salary": clean_text(raw.get("salary")),
        "date": parse_date(raw.get("date") or raw.get("updated_at") or raw.get("createdAt")),
        "source": source,
        "sourceType": source_type,
        "sourceJobId": source_job_id,
        "verified": bool(raw.get("verified", False)),
        "summary": description[:600],
        "whyFit": why_fit,
        "skillsMatch": clean_text(raw.get("skillsMatch")) or skills_match,
        "careerPath": clean_text(raw.get("careerPath")) or career_path,
        "url": url,
        "discoveryUrl": clean_text(raw.get("discoveryUrl") or defaults.get("discoveryUrl")),
        "discoverySource": clean_text(raw.get("discoverySource") or defaults.get("discoverySource")),
        "firstSeen": parse_date(raw.get("firstSeen")),
        "lastSeen": parse_date(raw.get("lastSeen")),
        "tags": tags,
    })
    return record


def greenhouse_jobs(source: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    if requests is None:
        raise RuntimeError("requests is not installed; run: python3 -m pip install -r scripts/requirements.txt")
    token = clean_text(source.get("token"))
    if not token:
        raise ValueError("missing required 'token'")
    endpoint = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    response = requests.get(endpoint, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        raise ValueError("unexpected Greenhouse response format")
    for job in payload["jobs"]:
        if not isinstance(job, dict):
            continue
        location = job.get("location", {})
        yield {
            "id": job.get("id"), "title": job.get("title"),
            "location": location.get("name", "") if isinstance(location, dict) else location,
            "content": job.get("content", ""), "updated_at": job.get("updated_at", ""),
            "url": job.get("absolute_url", ""),
        }


def lever_jobs(source: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    if requests is None:
        raise RuntimeError("requests is not installed; run: python3 -m pip install -r scripts/requirements.txt")
    site = clean_text(source.get("site"))
    if not site:
        raise ValueError("missing required 'site'")
    endpoint = f"https://api.lever.co/v0/postings/{site}?mode=json"
    response = requests.get(endpoint, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("unexpected Lever response format")
    for job in payload:
        if not isinstance(job, dict):
            continue
        categories = job.get("categories", {}) if isinstance(job.get("categories"), dict) else {}
        yield {
            "id": job.get("id"), "title": job.get("text"),
            "location": categories.get("location", ""), "description": job.get("descriptionPlain") or job.get("description"),
            "createdAt": job.get("createdAt", ""), "url": job.get("hostedUrl") or job.get("applyUrl", ""),
        }


def fetch_automatic_jobs(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    fetchers = {"greenhouse": greenhouse_jobs, "lever": lever_jobs}
    for source in sources:
        if not source.get("enabled", False):
            continue
        source_type = clean_text(source.get("type")).lower()
        company = clean_text(source.get("company")) or "Unnamed company"
        fetcher = fetchers.get(source_type)
        if fetcher is None:
            warn(f"source '{company}' has unsupported type '{source_type or 'missing'}'; skipped")
            continue
        try:
            defaults = {
                "company": company, "source": source_type.title(), "sourceType": source_type,
                "discoveryUrl": clean_text(source.get("discoveryUrl")),
            }
            for raw_job in fetcher(source):
                normalized = normalize_job(raw_job, defaults)
                if normalized is not None:
                    jobs.append(normalized)
        except (RuntimeError, requests.RequestException if requests else OSError, ValueError, json.JSONDecodeError) as error:
            warn(f"{source_type} source '{company}' failed: {error}; continuing with other sources")
    return jobs


TRACKING_QUERY_PARAMETERS = {"ref", "trk", "trackingid", "refid"}


def canonicalize_url(value: Any) -> str:
    """Remove URL fragments and common tracking parameters for URL matching."""
    url = clean_text(value)
    if not url:
        return ""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url.casefold()
    retained_query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in TRACKING_QUERY_PARAMETERS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), parsed.netloc.casefold(), path, urlencode(sorted(retained_query)), ""))


def normalize_identity(value: Any) -> str:
    """Normalize text used in the company/title/city fallback identity key."""
    normalized = unicodedata.normalize("NFKC", clean_text(value)).casefold()
    normalized = re.sub(r"[\W_]+", " ", normalized, flags=re.UNICODE)
    return re.sub(r"\s+", " ", normalized).strip()


def dedupe_keys(job: Dict[str, Any]) -> List[tuple[str, ...]]:
    """Return three ordered identity keys: source ID, canonical URL, then details."""
    company = normalize_identity(job.get("company"))
    source_job_id = clean_text(job.get("sourceJobId")).casefold()
    title = normalize_identity(job.get("title"))
    city = normalize_identity(job.get("city"))
    keys = []
    if company and source_job_id:
        keys.append(("sourceJobId", company, source_job_id))
    canonical_url = canonicalize_url(job.get("url"))
    if canonical_url:
        keys.append(("url", canonical_url))
    if company and title and city:
        keys.append(("details", company, title, city))
    return keys


def source_priority(job: Dict[str, Any]) -> int:
    """Rank sources from official company careers down to manual discovery."""
    source_info = f"{clean_text(job.get('sourceType'))} {clean_text(job.get('source'))}".casefold()
    if any(marker in source_info for marker in ("official-careers", "official careers", "company-careers", "company careers")):
        return 6
    if any(marker in source_info for marker in ("greenhouse", "lever", "official ats")):
        return 5
    if "linkedin" in source_info:
        return 4
    if "猎聘" in source_info or "liepin" in source_info:
        return 3
    if "boss直聘" in source_info or "boss zhipin" in source_info or "boss" in source_info:
        return 2
    return 1


def is_official_source(job: Dict[str, Any]) -> bool:
    return source_priority(job) >= 5


def source_sort_key(job: Dict[str, Any]) -> tuple[int, int, int, int]:
    """Prefer source authority, then verification and the more complete record."""
    populated_fields = sum(bool(job.get(field)) for field in SCHEMA_FIELDS if field not in {"firstSeen", "lastSeen", "tags"})
    return (source_priority(job), int(bool(job.get("verified"))), int(bool(job.get("url"))), populated_fields)


def merge_duplicate_group(jobs: List[Dict[str, Any]], today: str) -> Dict[str, Any]:
    """Merge equivalent records, retaining the highest-priority source's URL."""
    ranked = sorted(jobs, key=source_sort_key, reverse=True)
    merged = dict(ranked[0])
    preferred_url = canonicalize_url(merged.get("url"))

    for field in SCHEMA_FIELDS:
        if field in {"id", "url", "source", "sourceType", "sourceJobId", "verified", "firstSeen", "lastSeen", "tags", "discoveryUrl", "discoverySource"}:
            continue
        if not merged.get(field):
            merged[field] = next((job[field] for job in ranked if job.get(field)), merged[field])

    all_tags = []
    for job in ranked:
        all_tags.extend(job.get("tags") if isinstance(job.get("tags"), list) else [])
    merged["tags"] = list(dict.fromkeys(clean_text(tag) for tag in all_tags if clean_text(tag)))

    seen_dates = [job.get("firstSeen") or job.get("date") for job in jobs if job.get("firstSeen") or job.get("date")]
    merged["firstSeen"] = min(seen_dates) if seen_dates else today
    merged["lastSeen"] = today
    merged["verified"] = bool(merged.get("verified")) or is_official_source(merged)

    # Keep the selected (typically official) URL as the primary record, and
    # retain one distinct lower-priority URL as evidence of discovery.
    existing_discovery = canonicalize_url(merged.get("discoveryUrl"))
    if not existing_discovery or existing_discovery == preferred_url:
        alternate = next(
            (job for job in ranked[1:] if canonicalize_url(job.get("url")) and canonicalize_url(job.get("url")) != preferred_url),
            None,
        )
        if alternate:
            merged["discoveryUrl"] = alternate["url"]
            merged["discoverySource"] = alternate.get("source", "")
    return merged


def merge_jobs(existing_raw: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate using source ID, canonical URL, then normalized job details."""
    today = date.today().isoformat()
    records = []
    for raw in existing_raw:
        job = normalize_job(raw)
        if job is not None:
            records.append(job)
    records.extend(job for job in incoming if not is_sample_job(job))

    parents = list(range(len(records)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    owner_by_key: Dict[tuple[str, ...], int] = {}
    for index, job in enumerate(records):
        for key in dedupe_keys(job):
            if key in owner_by_key:
                union(index, owner_by_key[key])
            else:
                owner_by_key[key] = index

    grouped: Dict[int, List[Dict[str, Any]]] = {}
    for index, job in enumerate(records):
        grouped.setdefault(find(index), []).append(job)
    merged = [merge_duplicate_group(group, today) for group in grouped.values()]
    return sorted(merged, key=lambda job: (job["lastSeen"], job["date"], job["company"], job["title"]), reverse=True)


def main() -> int:
    existing = read_json_list(JOBS_FILE)
    manual_raw = read_json_list(MANUAL_JOBS_FILE)
    sources = read_json_list(SOURCES_FILE)
    manual = []
    for raw in manual_raw:
        if is_sample_job(raw):
            warn(f"manual SAMPLE job '{clean_text(raw.get('title')) or 'untitled'}' skipped")
            continue
        normalized = normalize_job(raw, {"source": "Manual", "sourceType": "manual"})
        if normalized is None:
            warn(f"manual job '{clean_text(raw.get('title')) or 'untitled'}' is not in Shanghai or Suzhou; skipped")
            continue
        manual.append(normalized)

    automatic = fetch_automatic_jobs(sources)
    merged = merge_jobs(existing, automatic + manual)
    with JOBS_FILE.open("w", encoding="utf-8") as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"Updated {JOBS_FILE.relative_to(ROOT)}: {len(merged)} jobs ({len(automatic)} automatic, {len(manual)} manual).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
