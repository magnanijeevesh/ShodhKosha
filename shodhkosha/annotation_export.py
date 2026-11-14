"""Write a paper's highlights and notes out as Markdown."""

from __future__ import annotations

from typing import Sequence

from shodhkosha.library_database import AnnotationDraft


def describe_annotation(annotation: AnnotationDraft, maximum_length: int = 110) -> str:
    """One-line description for lists, such as 'p. 3 · Highlight: thermal lens ...'."""
    if annotation.annotation_type == "highlight":
        label, text = "Highlight", annotation.selected_text or annotation.note_text
    elif annotation.annotation_type == "note":
        label, text = "Note", annotation.note_text
    else:
        label, text = "Text box", annotation.note_text
    text = " ".join(text.split())
    if len(text) > maximum_length:
        text = text[: maximum_length - 1].rstrip() + "…"
    return f"p. {annotation.page_number + 1} · {label}: {text}"


def format_annotations_markdown(title: str, annotations: Sequence[AnnotationDraft]) -> str:
    """Highlights as quotations and notes as plain lines, grouped by page."""
    lines = [f"# {title.strip() or 'Untitled'}", ""]
    if not annotations:
        lines.append("No highlights or notes.")
        return "\n".join(lines) + "\n"

    current_page = None
    for annotation in sorted(annotations, key=lambda item: item.page_number):
        if annotation.page_number != current_page:
            current_page = annotation.page_number
            lines += [f"## Page {current_page + 1}", ""]
        selected_text = " ".join(annotation.selected_text.split())
        note_text = " ".join(annotation.note_text.split())
        if annotation.annotation_type == "highlight":
            lines += [f"> {selected_text}", ""]
            if note_text:
                lines += [f"**Note:** {note_text}", ""]
        elif annotation.annotation_type == "note":
            lines += [f"**Note:** {note_text}", ""]
        else:
            lines += [f"**Text box:** {note_text}", ""]
    return "\n".join(lines).rstrip() + "\n"
