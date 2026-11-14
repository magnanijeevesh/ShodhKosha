"""Tests for the DOI lookup. No internet is used; the network call is simulated."""

import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shodhkosha.doi_lookup import (  # noqa: E402
    DoiLookupError,
    look_up_doi,
    normalize_doi,
    parse_crossref_work,
    title_matches_text,
)

SAMPLE_WORK = {
    "DOI": "10.1021/acsnano.2c09212",
    "title": ["Imaging <i>in situ</i> phase changes of H<sub>2</sub>O &amp; ice"],
    "author": [
        {"given": "Ada", "family": "Lovelace", "sequence": "first"},
        {"given": "Niels H. D.", "family": "Bohr"},
        {"name": "The Analytical Engine Consortium"},
    ],
    "container-title": ["ACS Nano"],
    "volume": "17",
    "issue": "1",
    "page": "505-514",
    "publisher": "American Chemical Society (ACS)",
    "published-online": {"date-parts": [[2022, 12, 22]]},
    "published-print": {"date-parts": [[2023, 1, 10]]},
    "issued": {"date-parts": [[2022, 12, 22]]},
}


def fake_response(payload: dict):
    response = mock.MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    return response


class DoiLookupTests(unittest.TestCase):
    def test_normalize_doi_accepts_common_forms(self) -> None:
        for text in (
            "10.1021/acsnano.2c09212",
            "  doi:10.1021/acsnano.2c09212 ",
            "DOI: 10.1021/acsnano.2c09212",
            "https://doi.org/10.1021/acsnano.2c09212",
            "http://dx.doi.org/10.1021/acsnano.2c09212",
        ):
            self.assertEqual(normalize_doi(text), "10.1021/acsnano.2c09212")
        for text in ("", "acsnano.2c09212", "10.1021", "not a doi"):
            with self.assertRaises(DoiLookupError):
                normalize_doi(text)

    def test_title_matches_text(self) -> None:
        first_page = (
            "ACS Nano www.acsnano.org Photothermal Imaging of Single Gold Nano-\n"
            "particles Ada Lovelace and Niels Bohr ABSTRACT: We image single particles"
        )
        self.assertTrue(
            title_matches_text("Photothermal Imaging of Single Gold Nanoparticles", first_page + " nanoparticles")
        )
        self.assertFalse(
            title_matches_text("Quantum Chromodynamics on the Lattice Revisited", first_page)
        )
        self.assertFalse(title_matches_text("Gold", first_page))
        self.assertFalse(title_matches_text("", first_page))

    def test_parse_crossref_work(self) -> None:
        record = parse_crossref_work(SAMPLE_WORK)
        self.assertEqual(record.title, "Imaging in situ phase changes of H2O & ice")
        self.assertEqual(
            record.authors,
            ["Ada Lovelace", "Niels H. D. Bohr", "The Analytical Engine Consortium"],
        )
        self.assertEqual(record.publication_year, 2023)  # print year is the one cited
        self.assertEqual(record.journal, "ACS Nano")
        self.assertEqual((record.volume, record.issue, record.pages), ("17", "1", "505-514"))
        self.assertEqual(record.publisher, "American Chemical Society (ACS)")

    def test_parse_tolerates_missing_fields(self) -> None:
        record = parse_crossref_work({"title": [], "issued": {"date-parts": [[None]]}}, "10.1/x")
        self.assertEqual(record.doi, "10.1/x")
        self.assertEqual(record.title, "")
        self.assertEqual(record.authors, [])
        self.assertIsNone(record.publication_year)

    def test_successful_lookup(self) -> None:
        with mock.patch(
            "shodhkosha.doi_lookup.urllib.request.urlopen",
            return_value=fake_response({"status": "ok", "message": SAMPLE_WORK}),
        ) as urlopen:
            record = look_up_doi("doi:10.1021/acsnano.2c09212")
        self.assertEqual(record.journal, "ACS Nano")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url, "https://api.crossref.org/works/10.1021/acsnano.2c09212"
        )

    def test_failures_become_readable_messages(self) -> None:
        not_found = urllib.error.HTTPError("url", 404, "Not Found", None, io.BytesIO(b""))
        offline = urllib.error.URLError("no route to host")
        cases = [
            (not_found, "no record"),
            (offline, "Could not reach Crossref"),
            (TimeoutError(), "Could not reach Crossref"),
        ]
        for side_effect, expected_text in cases:
            with mock.patch(
                "shodhkosha.doi_lookup.urllib.request.urlopen", side_effect=side_effect
            ):
                with self.assertRaises(DoiLookupError) as raised:
                    look_up_doi("10.1021/acsnano.2c09212")
            self.assertIn(expected_text, str(raised.exception))

        with mock.patch(
            "shodhkosha.doi_lookup.urllib.request.urlopen",
            return_value=fake_response({"message": {"title": []}}),
        ):
            with self.assertRaises(DoiLookupError):
                look_up_doi("10.1021/acsnano.2c09212")


if __name__ == "__main__":
    unittest.main()
