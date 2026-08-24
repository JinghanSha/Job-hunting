"""Simple deterministic test cases for classification, scoring, and deduplication."""

import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile
from unittest.mock import patch

from scripts.update_jobs import (
    SCHEMA_FIELDS,
    DATA_DIR,
    ROOT,
    astrazeneca_detail_fields,
    astrazeneca_careers_jobs,
    canonicalize_url,
    classify_direction,
    extract_degree,
    extract_experience,
    fetch_automatic_jobs,
    is_sample_job,
    merge_jobs,
    needs_astrazeneca_detail,
    normalize_identity,
    normalize_job,
    parse_astrazeneca_search_results,
    read_json_list,
    score_medical_phd_fit,
)


class MedicalPhdFitTests(unittest.TestCase):
    CASES = (
        ("Translational Medicine Scientist", "PhD", "", "Oncology biomarker research.", "A"),
        ("Clinical Scientist", "PhD", "", "", "A"),
        ("Medical Affairs Manager", "MD", "", "Oncology portfolio.", "A"),
        ("Medical Science Liaison", "Medical Degree", "", "Hematology field role.", "A"),
        ("Biomarker Scientist", "Doctorate", "", "", "A"),
        ("Clinical Pharmacology Scientist", "MD", "", "", "B"),
        ("Regulatory Affairs Specialist", "", "", "Regulatory submission work in immunology.", "C"),
        ("Research Associate", "PhD", "", "", "C"),
        ("Oncology Project Coordinator", "MD", "", "", "C"),
        ("Administrative Assistant", "PhD", "", "Supports Medical Affairs.", "D"),
        ("Sales Representative", "MD", "", "Oncology products.", "D"),
        ("Manufacturing Operator", "PhD", "", "Cell therapy manufacturing.", "D"),
        ("Receptionist", "", "", "", "D"),
    )

    def test_scoring_cases(self):
        for title, degree, qualifications, description, expected in self.CASES:
            with self.subTest(title=title):
                actual, why_fit = score_medical_phd_fit(title, degree, qualifications, description)
                self.assertEqual(actual, expected)
                self.assertTrue(why_fit)

    def test_phd_alone_is_not_a(self):
        actual, why_fit = score_medical_phd_fit("Research Associate", "PhD")
        self.assertNotEqual(actual, "A")
        self.assertIn("规则得分", why_fit)

    def test_why_fit_includes_rule_evidence(self):
        actual, why_fit = score_medical_phd_fit(
            "Translational Medicine Scientist", "PhD", "", "Oncology biomarker research."
        )
        self.assertEqual(actual, "A")
        self.assertIn("PhD", why_fit)
        self.assertIn("Translational Medicine", why_fit)
        self.assertIn("Oncology", why_fit)


class DirectionClassifierTests(unittest.TestCase):
    CASES = (
        ("Senior Scientist, Translational Oncology", "", "Translational Medicine"),
        ("Medical Science Liaison, Hematology", "", "MSL"),
        ("Associate Director Clinical Development", "", "Clinical Development"),
        ("Clinical Pharmacology Scientist", "", "Clinical Pharmacology"),
        ("Regulatory Affairs Manager", "", "Regulatory Affairs"),
        ("Medical Advisor, Medical Affairs", "", "Medical Advisor"),
        ("Clinical Research Physician, Clinical Development", "", "Clinical Research Physician"),
        ("Medical Affairs Manager", "", "Medical Affairs"),
        ("Business Development Manager", "", "Business Development"),
        ("Healthcare Strategy Consultant", "", "Healthcare Consulting"),
        ("Director", "Lead regulatory affairs submissions for China.", "Regulatory Affairs"),
        ("Regulatory Affairs Manager", "Supports Medical Affairs strategy.", "Regulatory Affairs"),
        ("Data Platform Engineer", "Build internal analytics systems.", "Other"),
    )

    def test_direction_cases(self):
        for title, description, expected in self.CASES:
            with self.subTest(title=title):
                self.assertEqual(classify_direction(title, description), expected)


class QualificationExtractionTests(unittest.TestCase):
    def test_english_degree_and_experience_patterns(self):
        self.assertEqual(extract_degree("Ph.D. or Doctor of Philosophy degree required"), "PhD/博士")
        self.assertEqual(extract_degree("Master's degree in life sciences"), "硕士")
        self.assertEqual(extract_experience("Minimum of 3-5 years of relevant experience"), "3-5 年相关经验")
        self.assertEqual(extract_experience("At least 2+ years of work experience"), "2+ 年相关经验")
        self.assertEqual(extract_experience("A minimum of 5 years’ experience"), "5 年相关经验")

    def test_chinese_degree_and_experience_patterns(self):
        self.assertEqual(extract_degree("要求博士学位，临床医学背景优先"), "PhD/博士")
        self.assertEqual(extract_experience("至少3年以上相关经验"), "3+ 年相关经验")
        self.assertEqual(extract_experience("具备3-5年工作经验"), "3-5 年相关经验")
        self.assertEqual(extract_experience("一年以上医药行业的相关经验"), "1+ 年相关经验")


class DeduplicationTests(unittest.TestCase):
    def job(self, **values):
        record = {field: "" for field in SCHEMA_FIELDS}
        record.update({"city": "上海", "tags": [], "verified": False, "source": "Manual", "sourceType": "manual"})
        record.update(values)
        return record

    def test_canonical_url_removes_tracking_and_fragment(self):
        url = "https://jobs.example.com/opening/42/?utm_source=linkedin&ref=mail&keep=yes#details"
        self.assertEqual(canonicalize_url(url), "https://jobs.example.com/opening/42?keep=yes")

    def test_identity_normalization_removes_simple_symbols(self):
        self.assertEqual(normalize_identity("  Acme--Bio, Inc.  "), "acme bio inc")
        self.assertEqual(normalize_identity("Acme_Bio"), "acme bio")
        self.assertEqual(normalize_identity("Senior  Scientist / Oncology"), "senior scientist oncology")

    def test_company_and_source_job_id_deduplicates(self):
        first = self.job(company="Acme Bio", title="Scientist", sourceJobId="123", url="https://a.example/one")
        second = self.job(company="ACME   BIO", title="Different title", sourceJobId="123", url="https://b.example/two")
        self.assertEqual(len(merge_jobs([], [first, second])), 1)

    def test_official_url_wins_over_linkedin_and_preserves_discovery(self):
        linkedin = self.job(
            company="Acme Bio", title="Clinical Scientist", source="LinkedIn", sourceType="linkedin",
            sourceJobId="linkedin-9", url="https://www.linkedin.com/jobs/view/9?trk=feed#job",
            firstSeen="2026-01-10",
        )
        official = self.job(
            company="Acme Bio", title="Clinical Scientist", source="Official Company Careers", sourceType="official-careers",
            sourceJobId="career-55", url="https://careers.acme.example/jobs/55?utm_campaign=spring",
            firstSeen="2026-02-01",
        )
        merged = merge_jobs([], [linkedin, official])
        # Different external IDs are deliberately not merged on title/city
        # alone; an official source cannot silently replace a manual record.
        self.assertEqual(len(merged), 2)
        self.assertEqual({job["sourceJobId"] for job in merged}, {"linkedin-9", "career-55"})

    def test_canonical_url_does_not_deduplicate_different_companies(self):
        first = self.job(company="Acme Bio", title="Scientist A", sourceJobId="a", url="https://jobs.acme.example/7?utm_source=x&ref=a")
        second = self.job(company="Another Co", title="Scientist B", sourceJobId="b", url="https://jobs.acme.example/7?trackingId=123&refId=abc")
        self.assertEqual(len(merge_jobs([], [first, second])), 2)

    def test_normalized_company_title_and_city_is_final_fallback(self):
        first = self.job(company="Acme Bio, Inc.", title="Senior Scientist - Oncology", sourceJobId="", url="https://one.example/1?utm_source=test")
        second = self.job(company=" ACME BIO INC ", title="senior  scientist oncology", sourceJobId="", url="https://one.example/1")
        self.assertEqual(len(merge_jobs([], [first, second])), 1)

    def test_sample_jobs_are_excluded_from_production_merge(self):
        sample = self.job(company="SAMPLE BioPharma", title="Sample role", sample=True, tags=["SAMPLE"])
        real = self.job(company="Acme Bio", title="Clinical Scientist", sourceJobId="real-1", url="https://jobs.acme.example/1")
        self.assertTrue(is_sample_job(sample))
        merged = merge_jobs([sample], [real])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["company"], "Acme Bio")

    def test_unobserved_existing_job_keeps_its_last_seen_date(self):
        existing = self.job(
            company="Acme Bio", title="Clinical Scientist", sourceJobId="real-1",
            url="https://jobs.acme.example/1", firstSeen="2026-01-10", lastSeen="2026-02-01",
            direction="Manually verified direction", whyFit="Stored assessment", summary="Stored source text",
        )
        merged = merge_jobs([existing], [])
        self.assertEqual(merged[0]["firstSeen"], "2026-01-10")
        self.assertEqual(merged[0]["lastSeen"], "2026-02-01")
        self.assertEqual(merged[0]["direction"], "Manually verified direction")
        self.assertEqual(merged[0]["whyFit"], "Stored assessment")


class StorageSafetyTests(unittest.TestCase):
    def test_invalid_required_jobs_file_raises_instead_of_becoming_empty(self):
        with NamedTemporaryFile("w", encoding="utf-8", dir=DATA_DIR, delete=False) as handle:
            path = ROOT / "data" / Path(handle.name).name
            handle.write("not valid json")
        try:
            with self.assertRaises(RuntimeError):
                read_json_list(path, required=True)
        finally:
            path.unlink(missing_ok=True)


class FakeResponse:
    def __init__(self, payload=None, error=None, text=""):
        self.payload = payload
        self.error = error
        self.text = text

    def raise_for_status(self):
        if self.error:
            raise self.error

    def json(self):
        return self.payload


class AstraZenecaCareersTests(unittest.TestCase):
    SOURCE = {
        "enabled": True,
        "company": "AstraZeneca",
        "type": "astrazeneca-careers",
        "source": "AstraZeneca Careers",
        "sourceType": "official-careers",
        "verified": True,
        "baseUrl": "https://job-search.astrazeneca.cn",
        "endpoint": "https://job-search.astrazeneca.cn/search-jobs/resultspost",
        "discoveryUrl": "https://job-search.astrazeneca.cn/search-jobs",
        "pageSize": 20,
        "maxPagesPerFacet": 1,
        "locationFacets": [{"id": "shanghai-id", "facetType": 4, "count": 3, "display": "上海, 上海, 中国"}],
    }
    RESULTS_HTML = """
      <section id="search-results" data-total-pages="1">
        <a class="search-results-link" href="/%e5%b7%a5%e4%bd%9c/shanghai/clinical-scientist/12977/1001" data-job-id="1001" class="left-link">
          <div><h2>Clinical Scientist</h2><span class="job-location">上海 上海</span></div>
        </a>
        <a class="search-results-link" href="/%e5%b7%a5%e4%bd%9c/suzhou/medical-advisor/12977/1002" data-job-id="1002" class="left-link">
          <div><h2>Medical Advisor</h2><span class="job-location">苏州市 江苏</span></div>
        </a>
        <a class="search-results-link" href="/%e5%b7%a5%e4%bd%9c/beijing/scientist/12977/1003" data-job-id="1003" class="left-link">
          <div><h2>Scientist</h2><span class="job-location">北京 北京</span></div>
        </a>
      </section>
    """
    DETAIL_HTML = """
      <script type="application/ld+json">
      {"@context":"https://schema.org", "@type":"JobPosting", "datePosted":"2026-8-21",
       "description":"<p>Clinical development and oncology biomarker work. Master's degree required. At least 3 years of relevant experience.</p>"}
      </script>
    """

    def mocked_jobs(self, html=None, existing=None, detail_response=None, stats_out=None):
        detail = detail_response or FakeResponse(text=self.DETAIL_HTML)
        with patch("scripts.update_jobs.requests.post", return_value=FakeResponse({"results": html or self.RESULTS_HTML})), \
             patch("scripts.update_jobs.requests.get", return_value=detail):
            return fetch_automatic_jobs([self.SOURCE], existing, stats_out)

    def test_response_parser_extracts_canonical_official_job_fields(self):
        jobs = parse_astrazeneca_search_results(self.RESULTS_HTML, self.SOURCE["baseUrl"])
        self.assertEqual([job["id"] for job in jobs], ["1001", "1002", "1003"])
        self.assertEqual(jobs[0]["title"], "Clinical Scientist")
        self.assertEqual(jobs[0]["location"], "上海 上海")
        self.assertEqual(jobs[0]["url"], "https://job-search.astrazeneca.cn/%e5%b7%a5%e4%bd%9c/shanghai/clinical-scientist/12977/1001")

    def test_target_cities_are_retained_and_other_cities_rejected(self):
        jobs = self.mocked_jobs()
        self.assertEqual([job["city"] for job in jobs], ["上海", "苏州"])
        self.assertTrue(all(job["source"] == "AstraZeneca Careers" for job in jobs))
        self.assertTrue(all(job["sourceType"] == "official-careers" for job in jobs))
        self.assertTrue(all(job["verified"] for job in jobs))
        self.assertTrue(all(job["discoveryUrl"] == self.SOURCE["discoveryUrl"] for job in jobs))

    def test_duplicate_astrazeneca_jobs_are_deduplicated(self):
        duplicate_html = self.RESULTS_HTML.replace("</section>", self.RESULTS_HTML.split("<a", 2)[1].split("</a>", 1)[0].join(["<a", "</a>"]) + "</section>")
        jobs = self.mocked_jobs(duplicate_html)
        self.assertEqual(len(jobs), 3)
        self.assertEqual(len(merge_jobs([], jobs)), 2)

    def test_zero_astrazeneca_response_preserves_historical_jobs(self):
        existing = [self.record("1001", "2026-08-01")]
        stats = []
        automatic = self.mocked_jobs('<section id="search-results" data-total-pages="1"></section>', existing, stats_out=stats)
        merged = merge_jobs(existing, automatic)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["lastSeen"], "2026-08-01")
        self.assertIn("AstraZeneca returned zero jobs", stats[0]["warnings"][0])

    def test_malformed_response_preserves_existing_production_record(self):
        existing = [self.record("1001", "2026-08-01")]
        with patch("scripts.update_jobs.requests.post", return_value=FakeResponse({"unexpected": "response"})):
            automatic = fetch_automatic_jobs([self.SOURCE])
        merged = merge_jobs(existing, automatic)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["sourceJobId"], "1001")
        self.assertEqual(merged[0]["lastSeen"], "2026-08-01")

    def test_unavailable_source_preserves_existing_last_seen(self):
        existing = [self.record("1001", "2026-08-01")]
        import requests
        with patch("scripts.update_jobs.requests.post", return_value=FakeResponse(error=requests.RequestException("unavailable"))):
            automatic = fetch_automatic_jobs([self.SOURCE])
        merged = merge_jobs(existing, automatic)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["lastSeen"], "2026-08-01")

    def test_last_seen_updates_only_after_successful_observation(self):
        existing = [self.record("1001", "2026-08-01")]
        successful = self.mocked_jobs()
        merged = merge_jobs(existing, successful)
        observed = next(job for job in merged if job["sourceJobId"] == "1001")
        self.assertEqual(observed["firstSeen"], "2026-01-01")
        self.assertEqual(observed["lastSeen"], date.today().isoformat())

    def test_new_job_gets_one_detail_enrichment_request(self):
        with patch("scripts.update_jobs.requests.post", return_value=FakeResponse({"results": self.RESULTS_HTML})), \
             patch("scripts.update_jobs.requests.get", return_value=FakeResponse(text=self.DETAIL_HTML)) as detail_get:
            jobs = fetch_automatic_jobs([self.SOURCE], [])
        self.assertEqual(detail_get.call_count, 2)
        self.assertIn("Clinical development", jobs[0]["summary"])
        self.assertIn("Clinical development", jobs[0]["description"])
        self.assertEqual(jobs[0]["date"], "2026-08-21")
        self.assertEqual(jobs[0]["detailStatus"], "enriched")
        self.assertEqual(jobs[0]["degree"], "硕士")
        self.assertEqual(jobs[0]["experience"], "3 年相关经验")

    def test_jsonld_graph_and_conservative_html_fallback_are_supported(self):
        graph_html = """
          <script type="application/ld+json">
          {"@graph":[{"@type":"JobPosting","datePosted":"2026-08-20",
          "description":"<p>PhD required. Minimum of 5 years of work experience.</p>"}]}
          </script>
        """
        self.assertEqual(astrazeneca_detail_fields(graph_html)["parser"], "json-ld")
        fallback_html = """
          <main><h2>Job Description</h2><p>This official role owns clinical development planning,
          cross-functional delivery, scientific communication, and compliant evidence generation.</p>
          <h2>Qualifications</h2><p>Doctoral degree with 5 years of relevant experience.</p></main>
        """
        detail = astrazeneca_detail_fields(fallback_html)
        self.assertEqual(detail["parser"], "html-fallback")
        self.assertIn("Doctoral degree", detail["description"])

    def test_detail_without_explicit_qualification_is_partial_not_invented(self):
        partial = """
          <script type="application/ld+json">
          {"@type":"JobPosting", "description":"<p>Lead customer research and marketing operations.</p>"}
          </script>
        """
        jobs = self.mocked_jobs(existing=[], detail_response=FakeResponse(text=partial))
        self.assertEqual(jobs[0]["detailStatus"], "partial")
        self.assertEqual(jobs[0]["degree"], "")
        self.assertEqual(jobs[0]["experience"], "")

    def test_existing_job_does_not_request_detail_page(self):
        existing = [self.record("1001", "2026-08-01"), self.record("1002", "2026-08-01")]
        with patch("scripts.update_jobs.requests.post", return_value=FakeResponse({"results": self.RESULTS_HTML})), \
             patch("scripts.update_jobs.requests.get", return_value=FakeResponse(text=self.DETAIL_HTML)) as detail_get:
            jobs = fetch_automatic_jobs([self.SOURCE], existing)
        self.assertEqual(detail_get.call_count, 0)
        self.assertEqual(len(jobs), 2)

    def test_limited_historical_backfill_only_enriches_pending_jobs(self):
        existing = [self.record("1001", "2026-08-01"), self.record("1002", "2026-08-01")]
        with patch("scripts.update_jobs.requests.post", return_value=FakeResponse({"results": self.RESULTS_HTML})), \
             patch("scripts.update_jobs.requests.get", return_value=FakeResponse(text=self.DETAIL_HTML)) as detail_get:
            jobs = fetch_automatic_jobs([self.SOURCE], existing, backfill_limit=1)
        self.assertEqual(detail_get.call_count, 1)
        self.assertEqual(next(job for job in jobs if job["sourceJobId"] == "1001")["detailStatus"], "enriched")
        self.assertEqual(next(job for job in jobs if job["sourceJobId"] == "1002")["detailStatus"], "")

    def test_enriched_historical_job_is_not_backfilled_again(self):
        existing = [self.record("1001", "2026-08-01"), self.record("1002", "2026-08-01")]
        for job in existing:
            job.update({
                "detailStatus": "enriched", "description": "Already enriched official JD",
                "summary": "Already enriched", "date": "2026-08-01",
            })
        with patch("scripts.update_jobs.requests.post", return_value=FakeResponse({"results": self.RESULTS_HTML})), \
             patch("scripts.update_jobs.requests.get", return_value=FakeResponse(text=self.DETAIL_HTML)) as detail_get:
            fetch_automatic_jobs([self.SOURCE], existing, backfill_limit=20)
        self.assertEqual(detail_get.call_count, 0)

    def test_legacy_enriched_summary_without_full_description_is_backfilled(self):
        existing = [self.record("1001", "2026-08-01"), self.record("1002", "2026-08-01")]
        existing[0].update({"detailStatus": "enriched", "summary": "Old short summary", "date": "2026-08-01"})
        existing[1].update({"detailStatus": "enriched", "description": "Stored official JD", "date": "2026-08-01"})
        with patch("scripts.update_jobs.requests.post", return_value=FakeResponse({"results": self.RESULTS_HTML})), \
             patch("scripts.update_jobs.requests.get", return_value=FakeResponse(text=self.DETAIL_HTML)) as detail_get:
            jobs = fetch_automatic_jobs([self.SOURCE], existing, backfill_limit=1)
        self.assertEqual(detail_get.call_count, 1)
        self.assertTrue(next(job for job in jobs if job["sourceJobId"] == "1001")["description"])

    def test_failed_and_unavailable_details_follow_retry_backoff(self):
        job = self.record("1001", "2026-08-01")
        job.update({"detailStatus": "failed", "detailFetchedAt": date.today().isoformat()})
        self.assertFalse(needs_astrazeneca_detail(job))
        job["detailFetchedAt"] = (date.today() - timedelta(days=7)).isoformat()
        self.assertTrue(needs_astrazeneca_detail(job))
        job.update({"detailStatus": "unavailable", "detailFetchedAt": date.today().isoformat()})
        self.assertFalse(needs_astrazeneca_detail(job))

    def test_detail_failure_keeps_new_job_base_record(self):
        import requests
        stats = []
        jobs = self.mocked_jobs(existing=[], detail_response=FakeResponse(error=requests.RequestException("detail unavailable")), stats_out=stats)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["summary"], "")
        self.assertEqual(jobs[0]["detailStatus"], "failed")
        self.assertTrue(any("detail enrichment failed" in warning for warning in stats[0]["warnings"]))

    def record(self, source_job_id, last_seen):
        return normalize_job({
            "id": source_job_id,
            "sourceJobId": source_job_id,
            "company": "AstraZeneca",
            "title": "Clinical Scientist",
            "location": "上海 上海",
            "source": "AstraZeneca Careers",
            "sourceType": "official-careers",
            "url": f"https://job-search.astrazeneca.cn/job/{source_job_id}",
            "firstSeen": "2026-01-01",
            "lastSeen": last_seen,
        })


if __name__ == "__main__":
    unittest.main()
