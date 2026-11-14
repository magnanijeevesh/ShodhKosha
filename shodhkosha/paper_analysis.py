"""Read a paper with a local AI model and file it in the library.

The model runs on this computer through Ollama. Nothing is sent to the
internet: the client only ever talks to ``127.0.0.1``.

This module contains no GUI code. ``request_analysis`` blocks while the model
works, so the application calls it from a background thread;
``apply_analysis`` writes to the database and must run on the GUI thread.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence

from pydantic import BaseModel, Field, ValidationError

from shodhkosha.library_database import LibraryDatabase, Paper

LOCAL_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_MODEL_NAME = "llama3.2"
REQUEST_TIMEOUT_SECONDS = 300
CONTEXT_WINDOW_TOKENS = 8192
MINIMUM_TEXT_LENGTH = 200
MAXIMUM_HIGHLIGHT_CHARACTERS = 1500
MAXIMUM_NEW_TAGS = 5
_MAXIMUM_FOLDER_LENGTH = 60
_MAXIMUM_TAG_LENGTH = 40
_EARLIEST_PLAUSIBLE_YEAR = 1600

_MAXIMUM_TAG_WORDS = 4
# Words that say nothing about a paper's topic; tags made only of these are dropped.
_GENERIC_TAG_WORDS = frozenset(
    """research study studies science scientific analysis paper article method methods
    methodology results findings experiment experiments experimental review introduction
    conclusion academic technology data theory model models modeling application
    applications physics chemistry biology engineering materials nanotechnology
    nanoscience investigation characterization measurement measurements novel new
    approach technique techniques""".split()
)

_AUTHOR_MARKER_PATTERN = re.compile(r"[\s\d,*\u2020\u2021\u00a7\u00b6]+$")  # "Bohr*,1"

_SYSTEM_PROMPT = (
    "You are a meticulous research librarian cataloguing academic papers. "
    "Use only information found in the text you are given. Never invent authors, "
    "years or findings. Reply with JSON only."
)


class PaperAnalysis(BaseModel):
    """The exact JSON structure the model must return."""

    title: str = Field(description="The paper's title exactly as printed.")
    authors: list[str] = Field(
        description='Author names in printed order, each as "First Last". Empty if not shown.'
    )
    year: int = Field(description="Four-digit publication year, or 0 if it is not shown.")
    folder: str = Field(
        description="The best matching existing folder, or a concise new category of 1-3 words."
    )
    tags: list[str] = Field(
        description=(
            "Three to five specific lowercase technical tags of 1-3 words each: the "
            "technique, the material or system studied, the phenomenon, the application."
        )
    )
    summary: str = Field(
        description="Exactly two sentences on the core methodology and the main findings."
    )


@dataclass(frozen=True)
class AnalysisRequest:
    """Everything the model needs, gathered on the GUI thread beforehand."""

    paper_text: str
    highlights: list[str] = field(default_factory=list)
    existing_folders: list[str] = field(default_factory=list)
    existing_tags: list[str] = field(default_factory=list)
    model_name: str = DEFAULT_MODEL_NAME


class AiError(Exception):
    """Base class; messages are suitable to show to the user."""


class AiUnavailableError(AiError):
    """The local AI cannot be used right now (not running, or model missing)."""


class AiResponseError(AiError):
    """The model answered, but not with something usable."""


def build_messages(request: AnalysisRequest) -> list[dict[str, str]]:
    """Compose the prompt, including the library's existing folders and tags."""
    folder_list = ", ".join(request.existing_folders) or "(none yet)"
    tag_list = ", ".join(request.existing_tags) or "(none yet)"

    sections = [
        "Catalogue the academic paper below.",
        f"EXISTING FOLDERS: {folder_list}",
        f"EXISTING TAGS: {tag_list}",
        "RULES:\n"
        "- folder: choose one of the existing folders if any of them fits reasonably "
        "well, spelled exactly as listed. Only if none fits, give a concise new "
        "category of 1-3 words (a research field, not the paper's title).\n"
        "- tags: 3 to 5 lowercase tags that a specialist would search for. Each tag is a "
        "specific technical term of 1-3 words naming one of: the experimental or "
        "computational technique, the material or system studied, the physical or "
        "chemical phenomenon, the application. Take the terms from the paper itself. "
        "Reuse an existing tag, spelled exactly as listed, whenever it means the same "
        "thing. Do not use broad words such as research, study, science, analysis, "
        "method, results, nanotechnology or materials, do not repeat the folder name, "
        "and do not use whole phrases from the title.\n"
        "- title: exactly as printed on the paper.\n"
        '- authors: in printed order, each as "First Last", without affiliations or symbols.\n'
        "- year: the four-digit publication year, or 0 if it is not shown.\n"
        "- summary: exactly two sentences covering the core methodology and the main findings.",
    ]
    highlights_text = "\n".join(f"- {text}" for text in request.highlights if text.strip())
    if highlights_text:
        sections.append(
            "PASSAGES THE READER HIGHLIGHTED (these show what the reader cares about):\n"
            + highlights_text[:MAXIMUM_HIGHLIGHT_CHARACTERS]
        )
    sections.append("PAPER TEXT (first pages and last page):\n" + request.paper_text)

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n\n".join(sections)},
    ]


def create_local_client() -> Any:
    """An Ollama client bound to this computer only."""
    try:
        import ollama
    except ImportError as error:
        raise AiUnavailableError(
            "The 'ollama' Python package is not installed. Run: pip install ollama"
        ) from error
    return ollama.Client(host=LOCAL_OLLAMA_HOST, timeout=REQUEST_TIMEOUT_SECONDS)


def translate_client_error(error: Exception, model_name: str) -> AiError:
    status_code = getattr(error, "status_code", None)
    if status_code == 404:
        return AiUnavailableError(
            f"The AI model '{model_name}' is not installed. "
            f"Run this in a terminal: ollama pull {model_name}"
        )
    error_type = type(error)
    is_connection_problem = isinstance(error, (ConnectionError, TimeoutError)) or (
        error_type.__module__.split(".")[0] in ("httpx", "httpcore")
        and any(word in error_type.__name__ for word in ("Connect", "Timeout", "Network"))
    )
    if is_connection_problem:
        return AiUnavailableError(
            "Ollama is not running. Start Ollama, then use Re-analyze with AI. "
            "You can still tag and file papers by hand."
        )
    return AiResponseError(f"The AI model reported an error: {error}")


def request_analysis(request: AnalysisRequest, client: Optional[Any] = None) -> PaperAnalysis:
    """Ask the local model to catalogue a paper. Blocks until it answers."""
    if len(request.paper_text.strip()) < MINIMUM_TEXT_LENGTH:
        raise AiResponseError(
            "This PDF has no readable text yet. If it is a scan, the AI will analyze it "
            "once its pages have been read by OCR."
        )
    if client is None:
        client = create_local_client()
    try:
        response = client.chat(
            model=request.model_name,
            messages=build_messages(request),
            format=PaperAnalysis.model_json_schema(),
            options={"temperature": 0, "num_ctx": CONTEXT_WINDOW_TOKENS},
        )
        content = response["message"]["content"]
    except AiError:
        raise
    except Exception as error:
        raise translate_client_error(error, request.model_name) from error

    try:
        return PaperAnalysis.model_validate_json(content)
    except ValidationError as error:
        raise AiResponseError("The AI model returned an answer in the wrong format.") from error


# ------------------------------------------------------------ applying results


def _name_key(name: str) -> str:
    """Comparison key that ignores case, spacing and punctuation."""
    return re.sub(r"[^0-9a-z]+", "", name.casefold())


def _clean_name(name: str, maximum_length: int) -> str:
    cleaned = " ".join(str(name).split()).strip(" .,;:\"'")
    return cleaned[:maximum_length].strip()


def resolve_existing_name(candidate: str, existing_names: Sequence[str]) -> str:
    """Return the existing name that matches the candidate, else the candidate.

    'photo-thermal microscopy' matches an existing 'Photothermal Microscopy',
    so the model cannot create near-duplicate folders or tags.
    """
    candidate_key = _name_key(candidate)
    for existing_name in existing_names:
        if _name_key(existing_name) == candidate_key:
            return existing_name
    return candidate


def _is_placeholder_title(paper: Paper) -> bool:
    title_key = _name_key(paper.title)
    return not title_key or title_key == _name_key(Path(paper.file_path).stem)


def _is_useful_tag(tag_name: str, folder_name: str) -> bool:
    words = re.findall(r"[0-9a-z]+", tag_name.casefold())
    if not words or len(words) > _MAXIMUM_TAG_WORDS:
        return False
    if all(word in _GENERIC_TAG_WORDS for word in words):
        return False
    return _name_key(tag_name) != _name_key(folder_name)


def apply_analysis(database: LibraryDatabase, paper: Paper, analysis: PaperAnalysis) -> Paper:
    """File a paper using the model's answer and return the updated paper.

    The summary and folder come from the model, and its tags replace the
    paper's current tags, so analyzing a paper again gives it a clean set.
    Title, authors and year are only filled in where they are missing, and
    never on a paper whose details the user has marked as verified.
    """
    changes: dict[str, object] = {}
    summary = " ".join(analysis.summary.split())
    if summary:
        changes["summary"] = summary

    if not paper.metadata_verified:
        title = " ".join(analysis.title.split())
        if title and _is_placeholder_title(paper):
            changes["title"] = title
        authors = [
            _clean_name(_AUTHOR_MARKER_PATTERN.sub("", author), 100)
            for author in analysis.authors
        ]
        authors = [author for author in authors if author]
        if authors and not paper.authors:
            changes["authors"] = authors
        if (
            not paper.publication_year
            and _EARLIEST_PLAUSIBLE_YEAR <= analysis.year <= date.today().year + 1
        ):
            changes["publication_year"] = analysis.year

    with database.transaction():
        if changes:
            database.update_paper_metadata(paper.paper_id, **changes)

        folder_name = _clean_name(analysis.folder, _MAXIMUM_FOLDER_LENGTH)
        if folder_name:
            folder_name = resolve_existing_name(
                folder_name, [folder.name for folder in database.list_folders()]
            )
            database.set_paper_folder(
                paper.paper_id, database.get_or_create_folder(folder_name).folder_id
            )

        existing_tag_names = [tag.name for tag in database.list_tags()]
        tag_names: list[str] = []
        seen_keys: set[str] = set()
        for raw_tag in analysis.tags:
            tag_name = _clean_name(raw_tag, _MAXIMUM_TAG_LENGTH).lower()
            if len(tag_names) >= MAXIMUM_NEW_TAGS or not _is_useful_tag(tag_name, folder_name):
                continue
            tag_name = resolve_existing_name(tag_name, existing_tag_names)
            if _name_key(tag_name) not in seen_keys:
                seen_keys.add(_name_key(tag_name))
                tag_names.append(tag_name)
        if tag_names:  # keep the current tags if the model offered nothing usable
            database.set_paper_tags(paper.paper_id, tag_names)

    return database.get_paper(paper.paper_id)
