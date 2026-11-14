"""Tests for study-card generation. Ollama is simulated."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shodhkosha.paper_analysis import AiResponseError, AiUnavailableError  # noqa: E402
from shodhkosha.study_cards import (  # noqa: E402
    GeneratedDeck,
    StudyCardRequest,
    build_messages,
    export_anki_text,
    request_study_cards,
)

PAGES = [
    (4, "Photothermal microscopy detects absorption by heating a particle. " * 5),
    (5, "The thermal lens changes the probe beam, giving the signal. " * 5),
]


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
        return {"message": {"content": content}}


class StudyCardTests(unittest.TestCase):
    def test_prompt_labels_pages_with_printed_numbers(self) -> None:
        user_message = build_messages(StudyCardRequest(PAGES, card_count=5))[1]["content"]
        self.assertIn("Write 5 study flashcards", user_message)
        self.assertIn("[Page 5]\nPhotothermal microscopy", user_message)
        self.assertIn("[Page 6]\nThe thermal lens", user_message)

    def test_cards_are_cleaned_and_mapped_to_pages(self) -> None:
        answer = {
            "cards": [
                {"question": " What creates the signal? ", "answer": "The thermal  lens.", "page": 6},
                {"question": "What creates the signal?", "answer": "Duplicate.", "page": 6},
                {"question": "How is absorption detected?", "answer": "By heating.", "page": 99},
                {"question": "", "answer": "No question.", "page": 5},
            ]
        }
        client = FakeClient(answer)
        cards = request_study_cards(StudyCardRequest(PAGES), client)
        self.assertEqual(
            cards,
            [
                (5, "What creates the signal?", "The thermal lens."),
                (4, "How is absorption detected?", "By heating."),  # unknown page -> first page
            ],
        )
        self.assertEqual(client.calls[0]["format"], GeneratedDeck.model_json_schema())

    def test_failures_become_readable_errors(self) -> None:
        request = StudyCardRequest(PAGES)
        with self.assertRaises(AiUnavailableError):
            request_study_cards(request, FakeClient(error=ConnectionError("refused")))
        with self.assertRaises(AiResponseError):
            request_study_cards(request, FakeClient(answer="not json"))
        with self.assertRaises(AiResponseError):
            request_study_cards(request, FakeClient(answer={"cards": []}))
        with self.assertRaises(AiResponseError):
            request_study_cards(StudyCardRequest([(0, "too short")]), FakeClient({"cards": []}))

    def test_anki_export(self) -> None:
        exported = export_anki_text(
            [("What\tis it?", "A lens.\nThermal."), ("Q2", "A2")], deck_tag="My Paper 2023"
        )
        self.assertEqual(
            exported,
            "#separator:tab\n#html:false\n#tags column:3\n"
            "What is it?\tA lens. Thermal.\tMy_Paper_2023\n"
            "Q2\tA2\tMy_Paper_2023\n",
        )


if __name__ == "__main__":
    unittest.main()
