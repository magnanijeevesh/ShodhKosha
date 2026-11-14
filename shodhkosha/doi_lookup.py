"""Fetch a paper's bibliographic details from Crossref using its DOI.

This is the only part of ShodhKosha that uses the internet, and it runs only
when the user asks for it. Nothing but the DOI is sent.
"""

from __future__ import annotations

import html
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

CROSSREF_WORKS_URL = "https://api.crossref.org/works/"
REQUEST_TIMEOUT_SECONDS = 10
_USER_AGENT = "ShodhKosha/0.1 (desktop reference manager)"

_DOI_PREFIX_PATTERN = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_VALID_DOI_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")
_MARKUP_TAG_PATTERN = re.compile(r"<[^>]+>")
_DATE_FIELDS = ("published-print", "published", "issued", "published-online")


class DoiLookupError(Exception):
    """The lookup failed; the message is suitable to show to the user."""


@dataclass(frozen=True)
class BibliographicRecord:
    doi: str
    title: str = ""
    authors: list[str] = field(default_factory=list)
    publication_year: Optional[int] = None
    journal: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    publisher: str = ""


def normalize_doi(text: str) -> str:
    """Accept '10.1021/x', 'doi:10.1021/x' or a doi.org link; return the bare DOI."""
    doi = _DOI_PREFIX_PATTERN.sub("", text.strip()).strip()
    if not _VALID_DOI_PATTERN.match(doi):
        raise DoiLookupError("Enter a DOI such as 10.1021/acsnano.2c09212 first.")
    return doi


def title_matches_text(title: str, text: str, required_fraction: float = 0.7) -> bool:
    """Whether a title's words appear in a paper's own text.

    The DOI found on a paper's first pages is occasionally that of a cited
    work. Before trusting a Crossref record, check that most words of its
    title occur in the paper's opening pages.
    """
    title_words = [word for word in re.findall(r"[0-9a-z]+", title.casefold()) if len(word) >= 3]
    if len(title_words) < 2:
        return False
    text_words = set(re.findall(r"[0-9a-z]+", text.casefold()))
    found = sum(1 for word in title_words if word in text_words)
    return found / len(title_words) >= required_fraction


def _clean_text(value: object) -> str:
    """Remove markup such as <i> and <sub> that Crossref leaves in titles."""
    text = html.unescape(_MARKUP_TAG_PATTERN.sub("", str(value or "")))
    return " ".join(text.split())


def _first_text(value: object) -> str:
    if isinstance(value, list):
        return _clean_text(value[0]) if value else ""
    return _clean_text(value)


def _publication_year(work: dict) -> Optional[int]:
    for date_field in _DATE_FIELDS:
        date_parts = (work.get(date_field) or {}).get("date-parts") or []
        if date_parts and date_parts[0] and date_parts[0][0]:
            try:
                return int(date_parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


def _author_names(work: dict) -> list[str]:
    names = []
    for author in work.get("author") or []:
        given = _clean_text(author.get("given"))
        family = _clean_text(author.get("family"))
        name = f"{given} {family}".strip() or _clean_text(author.get("name"))
        if name:
            names.append(name)
    return names


def parse_crossref_work(work: dict, doi: str = "") -> BibliographicRecord:
    """Convert the ``message`` object of a Crossref response into a record."""
    return BibliographicRecord(
        doi=_clean_text(work.get("DOI")) or doi,
        title=_first_text(work.get("title")),
        authors=_author_names(work),
        publication_year=_publication_year(work),
        journal=_first_text(work.get("container-title")),
        volume=_clean_text(work.get("volume")),
        issue=_clean_text(work.get("issue")),
        pages=_clean_text(work.get("page")),
        publisher=_clean_text(work.get("publisher")),
    )


def look_up_doi(doi_text: str, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> BibliographicRecord:
    """Ask Crossref for the details of a DOI. Blocks; call from a worker thread."""
    doi = normalize_doi(doi_text)
    request = urllib.request.Request(
        CROSSREF_WORKS_URL + urllib.parse.quote(doi, safe="/"),
        headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise DoiLookupError(f"Crossref has no record for the DOI {doi}.") from error
        raise DoiLookupError(f"Crossref returned an error (HTTP {error.code}).") from error
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as error:
        raise DoiLookupError(
            "Could not reach Crossref. Check the internet connection and try again."
        ) from error
    except ValueError as error:
        raise DoiLookupError("Crossref sent a reply that could not be read.") from error

    work = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(work, dict):
        raise DoiLookupError("Crossref sent a reply that could not be read.")
    record = parse_crossref_work(work, doi)
    if not record.title and not record.authors:
        raise DoiLookupError(f"Crossref has no usable details for the DOI {doi}.")
    return record
