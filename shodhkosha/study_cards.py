"""Turn pages of a paper into question-and-answer study cards with the local AI.

No GUI code here. ``request_study_cards`` blocks while the model works, so the
application calls it from a background thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from pydantic import BaseModel, Field, ValidationError

from shodhkosha.paper_analysis import (
    CONTEXT_WINDOW_TOKENS,
    DEFAULT_MODEL_NAME,
    AiError,
    AiResponseError,
    create_local_client,
    translate_client_error,
)

DEFAULT_CARD_COUNT = 8
MAXIMUM_TEXT_CHARACTERS = 8000
MINIMUM_TEXT_CHARACTERS = 200

_SYSTEM_PROMPT = (
    "You write study flashcards for a graduate student. Use only facts stated in the "
    "text you are given. Reply with JSON only."
)


class GeneratedCard(BaseModel):
    question: str = Field(description="A specific question answerable from the text.")
    answer: str = Field(description="The answer in one or two sentences, taken from the text.")
    page: int = Field(description="The page number, as labelled in the text, the answer is on.")


class GeneratedDeck(BaseModel):
    cards: list[GeneratedCard]


@dataclass(frozen=True)
class StudyCardRequest:
    """Pages are given as (zero-based page number, text)."""

    pages: Sequence[tuple[int, str]]
    card_count: int = DEFAULT_CARD_COUNT
    model_name: str = DEFAULT_MODEL_NAME


def _labelled_text(request: StudyCardRequest) -> str:
    """Page texts under '[Page N]' labels, sharing the character budget evenly."""
    pages = [(number, " ".join(text.split())) for number, text in request.pages]
    pages = [(number, text) for number, text in pages if text]
    if not pages:
        return ""
    budget_per_page = max(MAXIMUM_TEXT_CHARACTERS // len(pages), 200)
    return "\n\n".join(f"[Page {number + 1}]\n{text[:budget_per_page]}" for number, text in pages)


def build_messages(request: StudyCardRequest) -> list[dict[str, str]]:
    instructions = (
        f"Write {request.card_count} study flashcards from the pages below.\n"
        "RULES:\n"
        "- Each question tests one important idea: a definition, a method, a result, "
        "a number with its unit, or why something was done.\n"
        "- A question must make sense on its own, without seeing the page. Do not write "
        "“according to the text” or “in this paper”.\n"
        "- Each answer is one or two sentences and contains only facts stated on the pages.\n"
        "- page is the number in the [Page N] label where the answer is found.\n"
        "- Do not ask about authors, affiliations, references or acknowledgements."
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"{instructions}\n\nPAGES:\n{_labelled_text(request)}"},
    ]


def request_study_cards(
    request: StudyCardRequest, client: Optional[Any] = None
) -> list[tuple[int, str, str]]:
    """Ask the local model for cards. Returns (zero-based page, question, answer)."""
    if len(_labelled_text(request)) < MINIMUM_TEXT_CHARACTERS:
        raise AiResponseError("Those pages have too little readable text to make cards from.")
    if client is None:
        client = create_local_client()
    try:
        response = client.chat(
            model=request.model_name,
            messages=build_messages(request),
            format=GeneratedDeck.model_json_schema(),
            options={"temperature": 0.2, "num_ctx": CONTEXT_WINDOW_TOKENS},
        )
        content = response["message"]["content"]
    except AiError:
        raise
    except Exception as error:
        raise translate_client_error(error, request.model_name) from error

    try:
        deck = GeneratedDeck.model_validate_json(content)
    except ValidationError as error:
        raise AiResponseError("The AI model returned an answer in the wrong format.") from error

    valid_pages = [number for number, _ in request.pages]
    cards: list[tuple[int, str, str]] = []
    seen_questions: set[str] = set()
    for card in deck.cards:
        question = " ".join(card.question.split())
        answer = " ".join(card.answer.split())
        if not question or not answer or question.casefold() in seen_questions:
            continue
        seen_questions.add(question.casefold())
        page_number = card.page - 1 if card.page - 1 in valid_pages else valid_pages[0]
        cards.append((page_number, question, answer))
    if not cards:
        raise AiResponseError("The AI model did not produce any usable cards.")
    return cards


def export_anki_text(cards: Sequence[tuple[str, str]], deck_tag: str = "") -> str:
    """Tab-separated text that Anki imports directly (File > Import).

    Each line is: question <tab> answer <tab> tag.
    """
    tag = "_".join(deck_tag.split())

    def clean(text: str) -> str:
        return " ".join(text.replace("\t", " ").split())

    lines = ["#separator:tab", "#html:false", "#tags column:3"]
    lines += [f"{clean(question)}\t{clean(answer)}\t{tag}" for question, answer in cards]
    return "\n".join(lines) + "\n"
