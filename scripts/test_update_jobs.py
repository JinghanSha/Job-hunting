"""Simple deterministic test cases for classification, scoring, and deduplication."""

import unittest
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

from scripts.update_jobs import (
    SCHEMA_FIELDS,
    DATA_DIR,
    ROOT,
    canonicalize_url,
    classify_direction,
    is_sample_job,
    merge_jobs,
    normalize_identity,
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
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["url"], official["url"])
        self.assertTrue(merged[0]["verified"])
        self.assertEqual(merged[0]["discoveryUrl"], linkedin["url"])
        self.assertEqual(merged[0]["discoverySource"], "LinkedIn")
        self.assertEqual(merged[0]["firstSeen"], "2026-01-10")
        self.assertEqual(merged[0]["lastSeen"], date.today().isoformat())

    def test_canonical_url_deduplicates_different_tracking_links(self):
        first = self.job(company="Acme Bio", title="Scientist A", sourceJobId="a", url="https://jobs.acme.example/7?utm_source=x&ref=a")
        second = self.job(company="Another Co", title="Scientist B", sourceJobId="b", url="https://jobs.acme.example/7?trackingId=123&refId=abc")
        self.assertEqual(len(merge_jobs([], [first, second])), 1)

    def test_normalized_company_title_and_city_is_final_fallback(self):
        first = self.job(company="Acme Bio, Inc.", title="Senior Scientist - Oncology", sourceJobId="one", url="https://one.example/1")
        second = self.job(company=" ACME BIO INC ", title="senior  scientist oncology", sourceJobId="two", url="https://two.example/2")
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
        )
        merged = merge_jobs([existing], [])
        self.assertEqual(merged[0]["firstSeen"], "2026-01-10")
        self.assertEqual(merged[0]["lastSeen"], "2026-02-01")


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


if __name__ == "__main__":
    unittest.main()
