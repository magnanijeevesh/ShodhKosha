"""Format references as BibTeX entries and .bib files.

Titles and journal names are written exactly as stored. Titles are wrapped in
an extra pair of braces so BibTeX styles do not change their capitalisation,
which would damage chemical formulas and proper nouns.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional

from shodhkosha.library_database import Paper

_NAME_PARTICLES = frozenset(
    "van von de der den del della di da dos du la le ten ter bin ibn al el".split()
)
_NAME_SUFFIXES = frozenset(["jr", "jr.", "sr", "sr.", "ii", "iii", "iv"])
_TITLE_STOP_WORDS = frozenset("a an the on of in for and to with from by at".split())
_SPECIAL_CHARACTERS = re.compile(r"([&%$#_])")
_DASHES = re.compile(r"\s*[-‐-―]+\s*")


@dataclass(frozen=True)
class AuthorName:
    given: str
    family: str
    suffix: str = ""


def parse_author_name(name: str) -> AuthorName:
    """Split a stored name into given and family parts.

    Accepts 'Ada Lovelace', 'Lovelace, Ada', 'Ludwig van Beethoven' and
    'Martin Luther King Jr.'. A single word is treated as a family name.
    """
    text = " ".join(name.split())
    if "," in text:
        family, _, given = text.partition(",")
        return AuthorName(given.strip(), family.strip())

    words = text.split()
    suffix = ""
    if len(words) > 2 and words[-1].lower() in _NAME_SUFFIXES:
        suffix = words.pop()
    if len(words) <= 1:
        return AuthorName("", " ".join(words), suffix)

    family_start = len(words) - 1
    while family_start > 1 and words[family_start - 1].lower() in _NAME_PARTICLES:
        family_start -= 1
    return AuthorName(" ".join(words[:family_start]), " ".join(words[family_start:]), suffix)


def _ascii_letters(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return re.sub(r"[^a-z0-9]", "", decomposed.encode("ascii", "ignore").decode().lower())


def bibtex_key(paper: Paper) -> str:
    """Citation key such as 'lovelace2021photothermal'."""
    family = _ascii_letters(parse_author_name(paper.authors[0]).family) if paper.authors else ""
    year = str(paper.publication_year) if paper.publication_year else ""
    title_word = ""
    for word in paper.title.split():
        candidate = _ascii_letters(word)
        if candidate and candidate not in _TITLE_STOP_WORDS:
            title_word = candidate
            break
    return f"{family}{year}{title_word}" or f"reference{paper.paper_id}"


def _escape(text: str) -> str:
    return _SPECIAL_CHARACTERS.sub(r"\\\1", text.strip())


def _format_author(name: str) -> str:
    author = parse_author_name(name)
    if not author.given:
        return "{" + author.family + "}"  # braces keep an organisation name intact
    family = f"{author.family}, {author.suffix}" if author.suffix else author.family
    return f"{family}, {author.given}"


def format_bibtex_entry(paper: Paper, key: Optional[str] = None) -> str:
    """One BibTeX entry: @article when a journal is recorded, @book when only a
    publisher is, otherwise @misc."""
    fields: list[tuple[str, str]] = []
    if paper.authors:
        fields.append(("author", " and ".join(_escape(_format_author(n)) for n in paper.authors)))
    fields.append(("title", "{" + _escape(paper.title) + "}"))
    if paper.journal.strip():
        fields.append(("journal", _escape(paper.journal)))
    if paper.publication_year:
        fields.append(("year", str(paper.publication_year)))
    if paper.volume.strip():
        fields.append(("volume", _escape(paper.volume)))
    if paper.issue.strip():
        fields.append(("number", _escape(paper.issue)))
    if paper.pages.strip():
        fields.append(("pages", _DASHES.sub("--", paper.pages.strip())))
    if paper.publisher.strip():
        fields.append(("publisher", _escape(paper.publisher)))
    if paper.doi.strip():
        fields.append(("doi", paper.doi.strip()))

    if paper.journal.strip():
        entry_type = "article"
    elif paper.publisher.strip():
        entry_type = "book"
    else:
        entry_type = "misc"
    width = max(len(name) for name, _ in fields)
    lines = [f"@{entry_type}{{{key or bibtex_key(paper)},"]
    lines += [f"  {name.ljust(width)} = {{{value}}}," for name, value in fields]
    lines.append("}")
    return "\n".join(lines)


def export_bibtex(papers: Iterable[Paper]) -> str:
    """A complete .bib file. Repeated keys get a suffix: key, key-b, key-c, ..."""
    used_keys: dict[str, int] = {}
    entries = []
    for paper in papers:
        key = bibtex_key(paper)
        occurrence = used_keys.get(key, 0)
        used_keys[key] = occurrence + 1
        if occurrence:
            key = f"{key}-{chr(ord('a') + occurrence) if occurrence < 26 else occurrence + 1}"
        entries.append(format_bibtex_entry(paper, key))
    return "\n\n".join(entries) + ("\n" if entries else "")
