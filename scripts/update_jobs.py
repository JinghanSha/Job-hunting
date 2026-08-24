#!/usr/bin/env python3
"""Fetch public career-site jobs and merge them with manually maintained jobs.

Only public endpoints are used. The script never logs in, uses cookies, or
retries indefinitely; an unavailable source is reported and does not prevent
the remaining sources from being processed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

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
OBSERVED_THIS_RUN = "_observed_this_run"
AZ_MAX_PAGE_SIZE = 20
AZ_MAX_PAGES_PER_FACET = 10
AZ_MAX_DETAIL_BACKFILL = 20
WORKDAY_MAX_PAGE_SIZE = 20
WORKDAY_MAX_PAGES_PER_QUERY = 20
DETAIL_RETRY_DAYS = {"failed": 7, "unavailable": 30}
AZ_REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "MedicalPhDJobsBot/1.0 (+https://jinghansha.github.io/Job-hunting/)",
}
AZ_DETAIL_HEADERS = {
    "Accept": "text/html,application/xhtml+xml",
    "User-Agent": AZ_REQUEST_HEADERS["User-Agent"],
}

SCHEMA_FIELDS = (
    "id", "company", "title", "city", "location", "direction",
    "medicalPhdFit", "degree", "major", "experience", "salary", "date",
    "source", "sourceType", "sourceJobId", "verified", "description", "summary", "whyFit",
    "skillsMatch", "careerPath", "url", "discoveryUrl", "firstSeen",
    "discoverySource", "lastSeen", "detailStatus", "detailFetchedAt",
    "detailError", "detailAttemptCount", "detailContentHash", "tags",
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


def read_json_list(path: Path, *, required: bool = False) -> List[Dict[str, Any]]:
    """Read a JSON object list; required production data fails closed on errors."""
    relative_path = path.relative_to(ROOT)
    if not path.exists():
        message = f"{relative_path} does not exist"
        if required:
            raise RuntimeError(message)
        warn(f"{message}; treating it as an empty list")
        return []
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        message = f"could not read {relative_path}: {error}"
        if required:
            raise RuntimeError(message) from error
        warn(f"{message}; treating it as empty")
        return []
    if not isinstance(value, list):
        message = f"{relative_path} must contain a JSON array"
        if required:
            raise RuntimeError(message)
        warn(f"{message}; treating it as empty")
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
    # AstraZeneca's JSON-LD may split words across adjacent span elements.
    text = re.sub(r"</span>\s*<span(?:\s[^>]*)?>", "", text, flags=re.IGNORECASE)
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
    if re.search(r"\b(ph\.?d\.?|doctor(?:al|ate)(?:\s+degree)?|doctor\s+of\s+philosophy)\b|博士(?:学位)?", lowered):
        return "PhD/博士"
    if re.search(r"\b(m\.?d\.?|m\.?b\.?b\.?s\.?|medical\s+degree)\b|临床医学(?:学位)?", lowered):
        return "MD/临床医学"
    if re.search(r"\b(master'?s?(?:\s+degree)?|m\.?sc\.?|m\.?s\.?\s+degree)\b|硕士(?:学位)?", lowered):
        return "硕士"
    if re.search(r"\b(bachelor'?s?(?:\s+degree)?|undergraduate(?:\s+degree)?|b\.?sc\.?|b\.?s\.?\s+degree)\b|本科(?:学位)?", lowered):
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
    english = re.search(
        r"\b(\d+(?:\s*(?:-|–|to)\s*\d+)?\+?)\s*(?:years?|yrs?)(?:['’]s?)?\s*(?:of\s+)?"
        r"(?:relevant\s+|professional\s+|work\s+|industry\s+|related\s+|prior\s+)?experience\b",
        text,
        re.IGNORECASE,
    )
    if not english:
        english = re.search(
            r"\b(?:at\s+least|(?:a\s+)?minimum(?:\s+of)?|more\s+than|over)\s+"
            r"(\d+(?:\s*(?:-|–|to)\s*\d+)?\+?)\s*(?:years?|yrs?)\b",
            text,
            re.IGNORECASE,
        )
    if english:
        years = re.sub(r"\s+", "", english.group(1))
        return f"{years} 年相关经验"
    chinese = re.search(
        r"(?:至少|不少于|超过|具备|拥有)?\s*(\d+(?:\s*[-–至]\s*\d+)?\+?|[一二两三四五六七八九十])\s*年"
        r"(?:以上)?(?:医药|临床|研发)?(?:行业)?(?:的)?(?:相关|工作|行业|专业)?经验",
        text,
    )
    if chinese:
        years = re.sub(r"\s+", "", chinese.group(1))
        years = {"一": "1", "二": "2", "两": "2", "三": "3", "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9", "十": "10"}.get(years, years)
        if "以上" in chinese.group(0) and not years.endswith("+"):
            years += "+"
        return f"{years} 年相关经验"
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
        match = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if not match:
            return ""
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
        except ValueError:
            return ""


def normalize_job(raw: Dict[str, Any], defaults: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Convert a manual, legacy, Greenhouse, or Lever record into the shared schema."""
    if is_sample_job(raw):
        return None
    defaults = defaults or {}
    # A summary from an older record remains useful for classification, but it
    # must not be mistaken for a complete official detail-page description.
    stored_description = html_to_text(raw.get("description") or raw.get("content"))
    description = stored_description or html_to_text(raw.get("summary"))
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
        "verified": bool(raw.get("verified", defaults.get("verified", False))),
        "description": stored_description,
        "summary": description[:600],
        "whyFit": why_fit,
        "skillsMatch": clean_text(raw.get("skillsMatch")) or skills_match,
        "careerPath": clean_text(raw.get("careerPath")) or career_path,
        "url": url,
        "discoveryUrl": clean_text(raw.get("discoveryUrl") or defaults.get("discoveryUrl")),
        "discoverySource": clean_text(raw.get("discoverySource") or defaults.get("discoverySource")),
        "firstSeen": parse_date(raw.get("firstSeen")),
        "lastSeen": parse_date(raw.get("lastSeen")),
        "detailStatus": clean_text(raw.get("detailStatus") or defaults.get("detailStatus")),
        "detailFetchedAt": parse_date(raw.get("detailFetchedAt") or defaults.get("detailFetchedAt")),
        "detailError": clean_text(raw.get("detailError") or defaults.get("detailError")),
        "detailAttemptCount": detail_attempt_count(raw.get("detailAttemptCount") or defaults.get("detailAttemptCount")),
        "detailContentHash": clean_text(raw.get("detailContentHash") or defaults.get("detailContentHash")),
        "tags": tags,
    })
    return record


def detail_attempt_count(value: Any) -> int:
    """Return a safe persisted detail-fetch attempt count."""
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def normalize_existing_job(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize identity fields while preserving trusted stored production data.

    Existing records must not have their prior classifications, summaries, or
    detail metadata recomputed merely because an upstream source was unavailable
    or returned only a partial listing.  New schema fields are added with safe
    defaults, while fields already present in production remain unchanged until
    an observed incoming record supplies a more complete value.
    """
    normalized = normalize_job(raw)
    if normalized is None:
        return None
    for field in SCHEMA_FIELDS:
        if field not in raw:
            continue
        if field == "verified":
            normalized[field] = bool(raw[field])
        elif field == "tags":
            value = raw[field]
            normalized[field] = value if isinstance(value, list) else [value]
        else:
            normalized[field] = raw[field]
    return normalized


def note_request(stats: Optional[Dict[str, Any]]) -> None:
    if stats is not None:
        stats["requests"] += 1


def note_http_success(stats: Optional[Dict[str, Any]]) -> None:
    if stats is not None:
        stats["httpSuccessful"] += 1


def source_stats(source: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": clean_text(source.get("name") or source.get("company")) or "Unnamed source",
        "type": clean_text(source.get("type")),
        "requests": 0,
        "httpSuccessful": 0,
        "rawJobs": 0,
        "normalizedJobs": 0,
        "relevantJobs": 0,
        "newJobs": 0,
        "existingJobs": 0,
        "skippedJobs": 0,
        "backfillJobs": 0,
        "detailRequests": 0,
        "detailSuccessful": 0,
        "detailEnriched": 0,
        "detailPartial": 0,
        "detailUnavailable": 0,
        "detailFailed": 0,
        "warnings": [],
        "facets": [],
    }


def source_warning(stats: Dict[str, Any], message: str) -> None:
    if message not in stats["warnings"]:
        stats["warnings"].append(message)
        warn(message)


def print_source_stats(stats: Dict[str, Any]) -> None:
    print(f"=== {stats['name']} ===")
    print(f"Requests: {stats['requests']}")
    print(f"HTTP successful: {stats['httpSuccessful']}")
    print(f"Raw jobs: {stats['rawJobs']}")
    print(f"Normalized jobs: {stats['normalizedJobs']}")
    print(f"Relevant jobs: {stats['relevantJobs']}")
    print(f"New jobs: {stats['newJobs']}")
    print(f"Existing jobs: {stats['existingJobs']}")
    print(f"Skipped jobs: {stats['skippedJobs']}")
    if stats["backfillJobs"]:
        print(f"Historical detail backfill: {stats['backfillJobs']}")
    if stats["detailRequests"]:
        print(f"Detail requests: {stats['detailRequests']}")
        print(f"Detail successful: {stats['detailSuccessful']}")
        print(
            "Detail results: "
            f"enriched={stats['detailEnriched']}, partial={stats['detailPartial']}, "
            f"unavailable={stats['detailUnavailable']}, failed={stats['detailFailed']}"
        )
    for facet in stats["facets"]:
        print(f"Search slice {facet['name']}: {facet['status']}, jobs={facet['jobs']}")
    if stats["warnings"]:
        print("Status: WARNING")
        print(f"Reason: {'; '.join(stats['warnings'])}")
    else:
        print("Status: HEALTHY")


def greenhouse_jobs(source: Dict[str, Any], stats: Optional[Dict[str, Any]] = None) -> Iterable[Dict[str, Any]]:
    if requests is None:
        raise RuntimeError("requests is not installed; run: python3 -m pip install -r scripts/requirements.txt")
    token = clean_text(source.get("token"))
    if not token:
        raise ValueError("missing required 'token'")
    endpoint = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
    note_request(stats)
    response = requests.get(endpoint, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    note_http_success(stats)
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


def lever_jobs(source: Dict[str, Any], stats: Optional[Dict[str, Any]] = None) -> Iterable[Dict[str, Any]]:
    if requests is None:
        raise RuntimeError("requests is not installed; run: python3 -m pip install -r scripts/requirements.txt")
    site = clean_text(source.get("site"))
    if not site:
        raise ValueError("missing required 'site'")
    endpoint = f"https://api.lever.co/v0/postings/{site}?mode=json"
    note_request(stats)
    response = requests.get(endpoint, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    note_http_success(stats)
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


def workday_careers_jobs(source: Dict[str, Any], stats: Optional[Dict[str, Any]] = None) -> Iterable[Dict[str, Any]]:
    """Read public Workday CXS postings for Shanghai and Suzhou.

    Workday exposes its public career-search response without a candidate
    account.  The configured queries keep traffic bounded and the final city
    check is still performed by ``normalize_job`` before a record is retained.
    """
    if requests is None:
        raise RuntimeError("requests is not installed; run: python3 -m pip install -r scripts/requirements.txt")
    endpoint = clean_text(source.get("endpoint"))
    base_url = clean_text(source.get("baseUrl")).rstrip("/")
    queries = source.get("locationQueries", ["Shanghai", "Suzhou"])
    if not endpoint.startswith("https://") or not base_url.startswith("https://"):
        raise ValueError("Workday source requires HTTPS 'endpoint' and 'baseUrl'")
    if not isinstance(queries, list) or not queries or not all(clean_text(query) for query in queries):
        raise ValueError("Workday source requires non-empty 'locationQueries'")
    try:
        page_size = int(source.get("pageSize", WORKDAY_MAX_PAGE_SIZE))
        max_pages = int(source.get("maxPagesPerQuery", WORKDAY_MAX_PAGES_PER_QUERY))
    except (TypeError, ValueError) as error:
        raise ValueError("Workday page limits must be integers") from error
    page_size = min(max(page_size, 1), WORKDAY_MAX_PAGE_SIZE)
    max_pages = min(max(max_pages, 1), WORKDAY_MAX_PAGES_PER_QUERY)
    emitted_ids: set[str] = set()

    for query in (clean_text(value) for value in queries):
        query_stats = {"name": query, "jobs": 0, "status": "OK"}
        if stats is not None:
            stats["facets"].append(query_stats)
        try:
            offset = 0
            pages = 0
            while pages < max_pages:
                note_request(stats)
                response = requests.post(
                    endpoint,
                    json={"appliedFacets": {}, "limit": page_size, "offset": offset, "searchText": query},
                    timeout=REQUEST_TIMEOUT,
                    headers=AZ_REQUEST_HEADERS,
                )
                response.raise_for_status()
                note_http_success(stats)
                payload = response.json()
                postings = payload.get("jobPostings") if isinstance(payload, dict) else None
                total = payload.get("total") if isinstance(payload, dict) else None
                if not isinstance(postings, list) or not isinstance(total, int) or total < 0:
                    raise ValueError("unexpected Workday response schema")
                for posting in postings:
                    if not isinstance(posting, dict):
                        continue
                    external_path = clean_text(posting.get("externalPath"))
                    identifiers = posting.get("bulletFields")
                    source_job_id = clean_text(identifiers[0]) if isinstance(identifiers, list) and identifiers else ""
                    source_job_id = source_job_id or external_path.rsplit("_", 1)[-1]
                    if not external_path or not source_job_id or source_job_id in emitted_ids:
                        continue
                    emitted_ids.add(source_job_id)
                    query_stats["jobs"] += 1
                    yield {
                        "id": source_job_id,
                        "sourceJobId": source_job_id,
                        "title": posting.get("title", ""),
                        "location": posting.get("locationsText", ""),
                        "date": posting.get("postedOn", ""),
                        "url": f"{base_url}{external_path}",
                    }
                pages += 1
                offset += len(postings)
                if not postings or offset >= total:
                    break
            if query_stats["jobs"] == 0:
                query_stats["status"] = "WARNING"
                source_warning(stats, f"Workday query '{query}' returned no jobs")
        except (requests.RequestException if requests else OSError, ValueError, json.JSONDecodeError) as error:
            query_stats["status"] = "WARNING"
            source_warning(stats, f"Workday query '{query}' failed: {error}; continuing")


class AstraZenecaSearchResultsParser(HTMLParser):
    """Extract official job cards from AstraZeneca's public search-result HTML."""

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.jobs: List[Dict[str, str]] = []
        self.current: Optional[Dict[str, str]] = None
        self.in_title = False
        self.in_location = False

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attributes = dict(attrs)
        # AstraZeneca currently renders ``class`` twice on result anchors.  Do
        # not discard the first value when turning attributes into a mapping.
        classes = [
            class_name
            for name, value in attrs
            if name == "class" and value
            for class_name in value.split()
        ]
        if tag == "a" and "search-results-link" in classes:
            job_id = clean_text(attributes.get("data-job-id"))
            href = clean_text(attributes.get("href"))
            if job_id and href:
                self.current = {
                    "id": job_id,
                    "url": urljoin(self.base_url, href),
                    "title": "",
                    "location": "",
                    # The public search response does not expose detail text or
                    # dates.  Keep these schema-compatible fields explicit
                    # rather than issuing one detail request per listing.
                    "description": "",
                    "date": "",
                }
        elif self.current is not None and tag == "h2":
            self.in_title = True
        elif self.current is not None and tag == "span" and "job-location" in classes:
            self.in_location = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2":
            self.in_title = False
        elif tag == "span":
            self.in_location = False
        elif tag == "a" and self.current is not None:
            self.current["title"] = clean_text(self.current["title"])
            self.current["location"] = clean_text(self.current["location"])
            if self.current["title"] and self.current["location"]:
                self.jobs.append(self.current)
            self.current = None
            self.in_title = False
            self.in_location = False

    def handle_data(self, data: str) -> None:
        if self.current is None:
            return
        if self.in_title:
            self.current["title"] += data
        elif self.in_location:
            self.current["location"] += data


def parse_astrazeneca_search_results(html: str, base_url: str) -> List[Dict[str, str]]:
    """Parse one public AstraZeneca Careers result fragment without network access."""
    parser = AstraZenecaSearchResultsParser(base_url)
    parser.feed(html)
    parser.close()
    return parser.jobs


def astrazeneca_total_pages(html: str) -> int:
    """Read the server-reported page count, falling back to one result page."""
    match = re.search(r'data-total-pages=["\'](\d+)["\']', html)
    if not match:
        return 1
    return max(1, int(match.group(1)))


def astrazeneca_search_payload(facet: Dict[str, Any], page: int, page_size: int) -> Dict[str, Any]:
    """Build the documented-in-page request model for the public careers search."""
    facet_id = clean_text(facet.get("id"))
    facet_type = int(facet.get("facetType", 4))
    display = clean_text(facet.get("display"))
    if not facet_id or not display:
        raise ValueError("AstraZeneca location facet requires 'id' and 'display'")
    applied_facet = {
        "ID": facet_id,
        "FacetType": facet_type,
        "Count": int(facet.get("count", 0)),
        "Display": display,
        "IsApplied": True,
        "FieldName": "",
    }
    return {
        "ActiveFacetID": facet_id,
        "CurrentPage": page,
        "RecordsPerPage": page_size,
        "Distance": 50,
        "RadiusUnitType": 0,
        "Keywords": "",
        "Location": "",
        "Latitude": None,
        "Longitude": None,
        "ShowRadius": False,
        "IsPagination": "False",
        "CustomFacetName": "",
        "FacetTerm": "",
        "FacetType": 0,
        "FacetFilters": [applied_facet],
        "StaticFacets": None,
        "SearchResultsModuleName": "Search Results",
        "SearchFiltersModuleName": "Search Filters",
        "SortCriteria": 0,
        "SortDirection": 0,
        "SearchType": 5,
        "CategoryFacetTerm": None,
        "CategoryFacetType": None,
        "LocationFacetTerm": None,
        "LocationFacetType": None,
        "KeywordType": None,
        "LocationType": None,
        "LocationPath": None,
        "OrganizationIds": None,
        "RefinedKeywords": [],
        "PostalCode": "",
        "ResultsType": 0,
    }


def astrazeneca_detail_fields(html: str) -> Dict[str, str]:
    """Extract optional JobPosting fields from an AstraZeneca public detail page.

    AstraZeneca normally embeds JobPosting JSON-LD.  Some public templates put
    that object inside an @graph, so walk nested JSON objects before using a
    deliberately conservative <main> fallback.
    """
    posting_date = ""
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        try:
            structured = json.loads(unescape(block).strip())
        except json.JSONDecodeError:
            continue
        for entry in walk_jsonld(structured):
            entry_type = entry.get("@type")
            if entry_type == "JobPosting" or (isinstance(entry_type, list) and "JobPosting" in entry_type):
                description = html_to_text(entry.get("description"))
                posting_date = parse_date(entry.get("datePosted")) or posting_date
                if description:
                    return {"description": description, "date": posting_date, "parser": "json-ld"}
    fallback = astrazeneca_html_detail_fallback(html)
    return {"description": fallback, "date": posting_date, "parser": "html-fallback" if fallback else ""}


def walk_jsonld(value: Any) -> Iterable[Dict[str, Any]]:
    """Yield JSON-LD objects, including records placed under @graph."""
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from walk_jsonld(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from walk_jsonld(nested)


def astrazeneca_html_detail_fallback(html: str) -> str:
    """Read a public job-detail <main> only when it has clear JD markers."""
    match = re.search(r"<main\b[^>]*>(.*?)</main>", html, flags=re.IGNORECASE | re.DOTALL)
    candidate = html_to_text(match.group(1) if match else "")
    markers = (
        "job description", "responsibilities", "requirements", "qualifications",
        "what you'll do", "what you will do", "任职要求", "岗位职责", "职位描述", "工作职责",
    )
    if len(candidate) >= 80 and any(marker in candidate.casefold() for marker in markers):
        return candidate
    return ""


def enrich_astrazeneca_job(raw_job: Dict[str, Any], stats: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort detail enrichment for a newly discovered official job only."""
    if requests is None:
        raise RuntimeError("requests is not installed; run: python3 -m pip install -r scripts/requirements.txt")
    url = clean_text(raw_job.get("url"))
    if not url:
        return raw_job
    stats["detailRequests"] += 1
    note_request(stats)
    response = requests.get(url, timeout=REQUEST_TIMEOUT, headers=AZ_DETAIL_HEADERS)
    response.raise_for_status()
    note_http_success(stats)
    stats["detailSuccessful"] += 1
    enriched = dict(raw_job)
    details = astrazeneca_detail_fields(response.text)
    enriched.update(details)
    enriched["detailFetchedAt"] = date.today().isoformat()
    enriched["detailAttemptCount"] = detail_attempt_count(enriched.get("detailAttemptCount")) + 1
    if details["description"]:
        enriched["detailContentHash"] = hashlib.sha256(details["description"].encode("utf-8")).hexdigest()[:16]
        degree = extract_degree(details["description"]) or clean_text(enriched.get("degree"))
        experience = extract_experience(details["description"]) or clean_text(enriched.get("experience"))
        enriched["degree"] = degree
        enriched["experience"] = experience
        missing = [label for label, value in (("学历", degree), ("经验", experience)) if not value]
        if missing:
            enriched["detailStatus"] = "partial"
            enriched["detailError"] = f"Official JD did not explicitly state: {'、'.join(missing)}"
            stats["detailPartial"] += 1
        else:
            enriched["detailStatus"] = "enriched"
            enriched["detailError"] = ""
            stats["detailEnriched"] += 1
    else:
        enriched["detailStatus"] = "unavailable"
        enriched["detailError"] = "Official public detail page did not contain a readable job description"
        stats["detailUnavailable"] += 1
    return enriched


def astrazeneca_careers_jobs(source: Dict[str, Any], stats: Optional[Dict[str, Any]] = None) -> Iterable[Dict[str, Any]]:
    """Fetch target-city listings from AstraZeneca's unauthenticated public careers search.

    The endpoint returns a JSON object whose ``results`` property is HTML.
    Details are requested later, only for genuinely new jobs.
    """
    if requests is None:
        raise RuntimeError("requests is not installed; run: python3 -m pip install -r scripts/requirements.txt")
    endpoint = clean_text(source.get("endpoint"))
    base_url = clean_text(source.get("baseUrl"))
    facets = source.get("locationFacets")
    if not endpoint.startswith("https://") or not base_url.startswith("https://"):
        raise ValueError("AstraZeneca source requires HTTPS 'endpoint' and 'baseUrl'")
    if not isinstance(facets, list) or not facets:
        raise ValueError("AstraZeneca source requires at least one location facet")
    try:
        page_size = int(source.get("pageSize", AZ_MAX_PAGE_SIZE))
        max_pages = int(source.get("maxPagesPerFacet", AZ_MAX_PAGES_PER_FACET))
    except (TypeError, ValueError) as error:
        raise ValueError("AstraZeneca page limits must be integers") from error
    page_size = min(max(page_size, 1), AZ_MAX_PAGE_SIZE)
    max_pages = min(max(max_pages, 1), AZ_MAX_PAGES_PER_FACET)

    for facet in facets:
        if not isinstance(facet, dict):
            raise ValueError("AstraZeneca location facets must be objects")
        facet_stats = {
            "name": clean_text(facet.get("name") or facet.get("display")) or clean_text(facet.get("id")),
            "jobs": 0,
            "status": "OK",
        }
        if stats is not None:
            stats["facets"].append(facet_stats)
        try:
            note_request(stats)
            first_response = requests.post(
                endpoint,
                json=astrazeneca_search_payload(facet, 1, page_size),
                timeout=REQUEST_TIMEOUT,
                headers=AZ_REQUEST_HEADERS,
            )
            first_response.raise_for_status()
            note_http_success(stats)
            first_payload = first_response.json()
            if not isinstance(first_payload, dict) or not isinstance(first_payload.get("results"), str):
                raise ValueError("unexpected response schema")
            first_html = first_payload["results"]
            pages = min(astrazeneca_total_pages(first_html), max_pages)
            for job in parse_astrazeneca_search_results(first_html, base_url):
                facet_stats["jobs"] += 1
                yield job
            for page in range(2, pages + 1):
                note_request(stats)
                response = requests.post(
                    endpoint,
                    json=astrazeneca_search_payload(facet, page, page_size),
                    timeout=REQUEST_TIMEOUT,
                    headers=AZ_REQUEST_HEADERS,
                )
                response.raise_for_status()
                note_http_success(stats)
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("results"), str):
                    raise ValueError("unexpected response schema")
                for job in parse_astrazeneca_search_results(payload["results"], base_url):
                    facet_stats["jobs"] += 1
                    yield job
            if facet_stats["jobs"] == 0:
                facet_stats["status"] = "WARNING"
        except (requests.RequestException if requests else OSError, ValueError, json.JSONDecodeError) as error:
            facet_stats["status"] = "WARNING"
            if isinstance(error, ValueError) and "schema" in str(error):
                message = "AZ_HEALTH_WARNING: unexpected response schema"
            elif requests is not None and isinstance(error, requests.RequestException):
                message = "AZ_HEALTH_WARNING: request failed"
            else:
                message = f"AZ_HEALTH_WARNING: {error}"
            if stats is not None:
                source_warning(stats, message)
            else:
                warn(message)
    if stats is not None and stats["rawJobs"] == 0:
        source_warning(
            stats,
            "AZ_HEALTH_WARNING: AstraZeneca returned zero jobs. Possible API or location facet taxonomy change.",
        )


def job_identity_key(job: Dict[str, Any]) -> Optional[tuple[str, ...]]:
    """Return the durable identity required for same-source observation checks.

    Official external job IDs take precedence.  When an upstream source has no
    external ID, only a complete company/title/city/canonical-URL tuple is
    accepted as a fallback; empty descriptions and posting dates are irrelevant.
    """
    company = normalize_identity(job.get("company"))
    source_job_id = clean_text(job.get("sourceJobId")).casefold()
    if company and source_job_id:
        return ("sourceJobId", company, source_job_id)
    title = normalize_identity(job.get("title"))
    city = normalize_identity(job.get("city"))
    canonical_url = canonicalize_url(job.get("url"))
    if company and title and city and canonical_url:
        return ("details", company, title, city, canonical_url)
    return None


def needs_astrazeneca_detail(job: Dict[str, Any]) -> bool:
    """Return whether an official detail page should be requested again.

    Legacy records may have a short summary but no persisted full description,
    so they remain eligible for one controlled backfill.  Successfully stored
    descriptions are never repeatedly requested.  Failed or unavailable pages
    retry only after a conservative delay.
    """
    status = clean_text(job.get("detailStatus")).casefold()
    if status in {"enriched", "partial"} and clean_text(job.get("description")):
        return False
    if status not in DETAIL_RETRY_DAYS:
        return True
    fetched_at = parse_date(job.get("detailFetchedAt"))
    if not fetched_at:
        return True
    try:
        return date.fromisoformat(fetched_at) <= date.today() - timedelta(days=DETAIL_RETRY_DAYS[status])
    except ValueError:
        return True


def astrazeneca_backfill_keys(existing_raw: List[Dict[str, Any]], limit: int) -> set[tuple[str, ...]]:
    """Choose a stable, bounded set of historical AstraZeneca detail records."""
    candidates = []
    for raw in existing_raw:
        job = normalize_existing_job(raw)
        if job is None or normalize_identity(job.get("company")) != "astrazeneca":
            continue
        identity = job_identity_key(job)
        if identity is not None and needs_astrazeneca_detail(job):
            candidates.append((clean_text(job.get("sourceJobId")), canonicalize_url(job.get("url")), identity))
    candidates.sort()
    return {identity for _, _, identity in candidates[:limit]}


def requested_backfill_limit() -> int:
    """Read an explicit, bounded one-off detail-backfill request from the environment."""
    value = clean_text(os.environ.get("AZ_DETAIL_BACKFILL_LIMIT"))
    if not value:
        return 0
    try:
        return min(max(int(value), 0), AZ_MAX_DETAIL_BACKFILL)
    except ValueError:
        warn("Invalid AZ_DETAIL_BACKFILL_LIMIT; skipping historical detail backfill")
        return 0


def fetch_automatic_jobs(
    sources: List[Dict[str, Any]],
    existing_raw: Optional[List[Dict[str, Any]]] = None,
    stats_out: Optional[List[Dict[str, Any]]] = None,
    backfill_limit: int = 0,
) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    existing_by_key = {
        key: normalized
        for raw in existing_raw or []
        if (normalized := normalize_existing_job(raw)) is not None
        if (key := job_identity_key(normalized)) is not None
    }
    observed_keys = set(existing_by_key)
    backfill_limit = min(max(backfill_limit, 0), AZ_MAX_DETAIL_BACKFILL)
    selected_backfill_keys = astrazeneca_backfill_keys(existing_raw or [], backfill_limit)
    fetchers = {
        "greenhouse": greenhouse_jobs,
        "lever": lever_jobs,
        "astrazeneca-careers": astrazeneca_careers_jobs,
        "workday-careers": workday_careers_jobs,
    }
    for source in sources:
        if not source.get("enabled", False):
            continue
        source_type = clean_text(source.get("type")).lower()
        company = clean_text(source.get("company")) or "Unnamed company"
        stats = source_stats(source)
        fetcher = fetchers.get(source_type)
        if fetcher is None:
            source_warning(stats, f"source '{company}' has unsupported type '{source_type or 'missing'}'; skipped")
            print_source_stats(stats)
            if stats_out is not None:
                stats_out.append(stats)
            continue
        try:
            defaults = {
                "company": company,
                "source": clean_text(source.get("source")) or source_type.title(),
                "sourceType": clean_text(source.get("sourceType")) or source_type,
                "verified": bool(source.get("verified", False)),
                "discoveryUrl": clean_text(source.get("discoveryUrl")),
            }
            for raw_job in fetcher(source, stats):
                stats["rawJobs"] += 1
                normalized = normalize_job(raw_job, defaults)
                if normalized is None:
                    stats["skippedJobs"] += 1
                    continue
                stats["normalizedJobs"] += 1
                stats["relevantJobs"] += 1
                identity = job_identity_key(normalized)
                is_new = identity is None or identity not in observed_keys
                existing_job = existing_by_key.get(identity) if identity is not None else None
                if is_new:
                    stats["newJobs"] += 1
                else:
                    stats["existingJobs"] += 1
                should_backfill = (
                    source_type == "astrazeneca-careers"
                    and existing_job is not None
                    and identity in selected_backfill_keys
                )
                should_enrich = source_type == "astrazeneca-careers" and (is_new or should_backfill)
                if should_backfill:
                    stats["backfillJobs"] += 1
                if should_enrich:
                    if existing_job is not None:
                        # Retain historical detail metadata and full JD while
                        # refreshing the listing fields from the official search.
                        raw_job = {**existing_job, **raw_job}
                    try:
                        raw_job = enrich_astrazeneca_job(raw_job, stats)
                        normalized = normalize_job(raw_job, defaults)
                    except (RuntimeError, requests.RequestException if requests else OSError, ValueError, json.JSONDecodeError) as error:
                        raw_job = dict(raw_job)
                        raw_job.update({
                            "detailStatus": "failed",
                            "detailFetchedAt": date.today().isoformat(),
                            "detailAttemptCount": detail_attempt_count(raw_job.get("detailAttemptCount")) + 1,
                            "detailError": clean_text(error)[:240],
                        })
                        normalized = normalize_job(raw_job, defaults)
                        stats["detailFailed"] += 1
                        source_warning(stats, f"AZ_HEALTH_WARNING: detail enrichment failed: {error}")
                if identity is not None:
                    observed_keys.add(identity)
                    if normalized is not None:
                        existing_by_key[identity] = normalized
                if normalized is not None:
                    jobs.append(normalized)
        except (RuntimeError, requests.RequestException if requests else OSError, ValueError, json.JSONDecodeError) as error:
            source_warning(stats, f"{source_type} source '{company}' failed: {error}; continuing with other sources")
        print_source_stats(stats)
        if stats_out is not None:
            stats_out.append(stats)
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
    """Return the preferred external-ID key or the conservative fallback key."""
    identity = job_identity_key(job)
    return [identity] if identity is not None else []


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
    if any(job.get(OBSERVED_THIS_RUN) for job in jobs):
        merged["lastSeen"] = today
    else:
        prior_last_seen = [job.get("lastSeen") for job in jobs if job.get("lastSeen")]
        merged["lastSeen"] = max(prior_last_seen) if prior_last_seen else merged["firstSeen"]
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
    merged.pop(OBSERVED_THIS_RUN, None)
    return merged


def merge_jobs(existing_raw: List[Dict[str, Any]], incoming: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate using source ID, canonical URL, then normalized job details."""
    today = date.today().isoformat()
    records = []
    for raw in existing_raw:
        job = normalize_existing_job(raw)
        if job is not None:
            job[OBSERVED_THIS_RUN] = False
            records.append(job)
    for job in incoming:
        if not is_sample_job(job):
            observed_job = dict(job)
            observed_job[OBSERVED_THIS_RUN] = True
            records.append(observed_job)

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


def write_jobs_atomically(jobs: List[Dict[str, Any]]) -> None:
    """Write a complete replacement beside jobs.json before atomically swapping it in."""
    temporary_path: Optional[Path] = None
    try:
        serialized = json.dumps(jobs, ensure_ascii=False, indent=2)
        # Validate the complete payload before touching the production file.
        if not isinstance(json.loads(serialized), list):
            raise ValueError("jobs payload must be a JSON array")
        with NamedTemporaryFile("w", encoding="utf-8", dir=DATA_DIR, prefix=".jobs-", suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(serialized)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, JOBS_FILE)
    except (OSError, TypeError, ValueError):
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def main() -> int:
    try:
        existing = read_json_list(JOBS_FILE, required=True)
    except RuntimeError as error:
        warn(f"update aborted; production jobs data was left unchanged: {error}")
        return 1
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

    run_stats: List[Dict[str, Any]] = []
    backfill_limit = requested_backfill_limit()
    automatic = fetch_automatic_jobs(sources, existing, run_stats, backfill_limit)
    merged = merge_jobs(existing, automatic + manual)
    try:
        write_jobs_atomically(merged)
    except (OSError, TypeError, ValueError) as error:
        warn(f"update failed; production jobs data was left unchanged: {error}")
        return 1
    az_stats = next((stats for stats in run_stats if stats["type"] == "astrazeneca-careers"), None)
    print(f"Total jobs before: {len(existing)}")
    print(f"Total jobs after: {len(merged)}")
    print(f"Automatically collected: {len(automatic)}")
    print(f"Manual jobs: {len(manual)}")
    print(f"New jobs: {sum(stats['newJobs'] for stats in run_stats)}")
    if backfill_limit:
        print(f"Historical detail backfill limit: {backfill_limit}")
    if az_stats is not None:
        print(f"AstraZeneca raw: {az_stats['rawJobs']}")
        print(f"AstraZeneca relevant: {az_stats['relevantJobs']}")
        print(f"AstraZeneca new: {az_stats['newJobs']}")
    print(f"Updated {JOBS_FILE.relative_to(ROOT)}: {len(merged)} jobs ({len(automatic)} automatic, {len(manual)} manual).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
