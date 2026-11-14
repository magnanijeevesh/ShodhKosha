"""Tests for the AI cataloguing logic. Ollama is simulated; no model is needed."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shodhkosha.library_database import LibraryDatabase  # noqa: E402
from shodhkosha.paper_analysis import (  # noqa: E402
    AiResponseError,
    AiUnavailableError,
    AnalysisRequest,
    PaperAnalysis,
    apply_analysis,
    build_messages,
    request_analysis,
    resolve_existing_name,
)

PAPER_TEXT = "Photothermal imaging of single gold nanoparticles. " * 10

GOOD_ANSWER = {
    "title": "Photothermal Imaging of Single Gold Nanoparticles",
    "authors": ["Ada Lovelace", "Niels Bohr*"],
    "year": 2021,
    "folder": "photo-thermal microscopy",
    "tags": ["Plasmonics", "gold nanoparticles", "Research Methods", "plasmonics",
             "photothermal microscopy", "single particle absorption imaging setup", "thermal lens"],
    "summary": "They image particles with a heating beam.  The signal scales with absorption.",
}


class FakeClient:
    def __init__(self, answer=None, error=None) -> None:
        self.answer = answer
        self.error = error
        self.calls = []

    def chat(self, **arguments):
        self.calls.append(arguments)
        if self.error is not None:
            raise self.error
        content = self.answer if isinstance(self.answer, str) else json.dumps(self.answer)
        return {"message": {"role": "assistant", "content": content}}


class ModelMissingError(Exception):
    status_code = 404


class PromptTests(unittest.TestCase):
    def test_prompt_lists_existing_folders_tags_and_highlights(self) -> None:
        request = AnalysisRequest(
            paper_text=PAPER_TEXT,
            highlights=["thermal diffusivity", "  "],
            existing_folders=["Photothermal Microscopy", "Beam Profiling"],
            existing_tags=["plasmonics"],
        )
        user_message = build_messages(request)[1]["content"]
        self.assertIn("EXISTING FOLDERS: Photothermal Microscopy, Beam Profiling", user_message)
        self.assertIn("EXISTING TAGS: plasmonics", user_message)
        self.assertIn("- thermal diffusivity", user_message)
        self.assertIn(PAPER_TEXT, user_message)
        self.assertIn("(none yet)", build_messages(AnalysisRequest(PAPER_TEXT))[1]["content"])

    def test_request_sends_schema_and_local_options(self) -> None:
        client = FakeClient(GOOD_ANSWER)
        analysis = request_analysis(AnalysisRequest(PAPER_TEXT, model_name="qwen2.5:7b"), client)
        self.assertEqual(analysis.year, 2021)
        call = client.calls[0]
        self.assertEqual(call["model"], "qwen2.5:7b")
        self.assertEqual(call["format"], PaperAnalysis.model_json_schema())
        self.assertEqual(call["options"]["temperature"], 0)
        self.assertGreaterEqual(call["options"]["num_ctx"], 8192)

    def test_failures_become_readable_errors(self) -> None:
        request = AnalysisRequest(PAPER_TEXT)
        with self.assertRaises(AiUnavailableError) as raised:
            request_analysis(request, FakeClient(error=ConnectionError("refused")))
        self.assertIn("Ollama is not running", str(raised.exception))

        with self.assertRaises(AiUnavailableError) as raised:
            request_analysis(request, FakeClient(error=ModelMissingError("not found")))
        self.assertIn("ollama pull llama3.2", str(raised.exception))

        with self.assertRaises(AiResponseError):
            request_analysis(request, FakeClient(error=RuntimeError("boom")))
        with self.assertRaises(AiResponseError):
            request_analysis(request, FakeClient(answer="not json"))
        with self.assertRaises(AiResponseError):
            request_analysis(request, FakeClient(answer={"title": "only a title"}))
        with self.assertRaises(AiResponseError):
            request_analysis(AnalysisRequest("too short"), FakeClient(GOOD_ANSWER))


class ApplyAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database = LibraryDatabase(":memory:")
        self.analysis = PaperAnalysis(**GOOD_ANSWER)

    def tearDown(self) -> None:
        self.database.close()

    def test_resolve_existing_name(self) -> None:
        existing = ["Photothermal Microscopy", "Beam Profiling"]
        self.assertEqual(
            resolve_existing_name("photo-thermal  microscopy", existing), "Photothermal Microscopy"
        )
        self.assertEqual(resolve_existing_name("Spectroscopy", existing), "Spectroscopy")

    def test_new_paper_is_filled_and_filed(self) -> None:
        self.database.create_folder("Photothermal Microscopy")
        self.database.get_or_create_tag("plasmonics")
        paper = self.database.add_paper("papers/nn2c09212.pdf", "hash", title="nn2c09212")

        updated = apply_analysis(self.database, paper, self.analysis)

        self.assertEqual(updated.title, "Photothermal Imaging of Single Gold Nanoparticles")
        self.assertEqual(updated.authors, ["Ada Lovelace", "Niels Bohr"])
        self.assertEqual(updated.publication_year, 2021)
        self.assertEqual(updated.folder_name, "Photothermal Microscopy")  # reused, not duplicated
        # Reuses "plasmonics"; drops the generic tag, the duplicate, the copy of
        # the folder name and the five-word phrase.
        self.assertEqual(updated.tags, ["gold nanoparticles", "plasmonics", "thermal lens"])
        self.assertEqual(
            updated.summary,
            "They image particles with a heating beam. The signal scales with absorption.",
        )
        self.assertEqual(len(self.database.list_folders()), 1)
        self.assertFalse(updated.metadata_verified)

    def test_existing_details_are_kept(self) -> None:
        paper = self.database.add_paper(
            "papers/a.pdf", "hash", title="A Real Title", authors=["Marie Curie"],
            publication_year=1903, tag_names=["my tag"],
        )
        updated = apply_analysis(self.database, paper, self.analysis)
        self.assertEqual(updated.title, "A Real Title")
        self.assertEqual(updated.authors, ["Marie Curie"])
        self.assertEqual(updated.publication_year, 1903)
        self.assertNotIn("my tag", updated.tags)  # analysis replaces the tag set
        self.assertIn("thermal lens", updated.tags)
        self.assertEqual(updated.folder_name, "photo-thermal microscopy")

    def test_verified_paper_keeps_all_bibliographic_details(self) -> None:
        paper = self.database.add_paper("papers/b.pdf", "hash", title="b", metadata_verified=True)
        updated = apply_analysis(self.database, paper, self.analysis)
        self.assertEqual(updated.title, "b")
        self.assertEqual(updated.authors, [])
        self.assertIsNone(updated.publication_year)
        self.assertTrue(updated.summary)

    def test_implausible_values_are_ignored(self) -> None:
        paper = self.database.add_paper("papers/c.pdf", "hash", title="c")
        odd = PaperAnalysis(
            title="", authors=[" ", ""], year=0, folder="  ", tags=["", "a", "b", "c", "d", "e", "f"],
            summary="",
        )
        updated = apply_analysis(self.database, paper, odd)
        self.assertEqual(updated.title, "c")
        self.assertIsNone(updated.publication_year)
        self.assertIsNone(updated.folder_name)
        self.assertEqual(len(updated.tags), 5)
        self.assertEqual(self.database.list_folders(), [])

    def test_tags_are_kept_when_the_model_offers_nothing_usable(self) -> None:
        paper = self.database.add_paper("papers/d.pdf", "hash", title="d", tag_names=["my tag"])
        generic = self.analysis.model_copy(update={"tags": ["research", "new methods", ""]})
        self.assertEqual(apply_analysis(self.database, paper, generic).tags, ["my tag"])


if __name__ == "__main__":
    unittest.main()
