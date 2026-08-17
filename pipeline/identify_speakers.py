#!/usr/bin/env python3
"""Infer speakers for TEI <q> elements and write an enriched XML copy.

The program is deliberately conservative. It uses explicit speech-attribution
verbs and paragraph structure first, then limited neighboring-quote rules. Every
assignment records its method and confidence, and unresolved quotes are retained.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET


XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
SPEECH_VERBS = (
    "said|asked|answered|replied|returned|cried|exclaimed|observed|shouted|"
    "called|continued|added|murmured|whispered|remarked|responded|began|"
    "interrupted|urged|pleaded|declared|demanded|suggested|repeated|"
    "protested|pronounced|ejaculated|resumed|went on"
)
NAME_TOKEN = re.compile(r"\[\[NAME:([^\]]+)\]\]")
SPACE = re.compile(r"\s+")
QUOTE_TAGS = {"q", "quotation"}


@dataclass
class Decision:
    speaker: str | None = None
    confidence: float = 0.0
    method: str = "unresolved"


def local_name(tag: object) -> str:
    # ElementTree represents comments and processing instructions with callable
    # sentinel tags when they are retained by TreeBuilder.
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def norm_space(text: str) -> str:
    return SPACE.sub(" ", text).strip()


def slug(text: str) -> str:
    value = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value or "unknown"


def quote_text(q: ET.Element) -> str:
    return norm_space("".join(q.itertext()))


def read_roster(path: Path) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        name = norm_space(raw)
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def render_context(node: ET.Element, target: ET.Element) -> str:
    """Flatten a paragraph, preserving marked names and the target quote."""
    parts: list[str] = []

    def walk(element: ET.Element) -> None:
        tag = local_name(element.tag)
        if element is target:
            parts.append(" [[QUOTE]] ")
        elif tag == "name":
            parts.append(f" [[NAME:{norm_space(''.join(element.itertext()))}]] ")
        else:
            if element.text:
                parts.append(element.text)
            for child in element:
                walk(child)
                if child.tail:
                    parts.append(child.tail)

    walk(node)
    return norm_space("".join(parts))


def roster_match(raw_name: str, roster: list[str]) -> str | None:
    raw = norm_space(raw_name).casefold()
    exact = [name for name in roster if name.casefold() == raw]
    if exact:
        return exact[0]
    # The XML sometimes marks only the surname inside a title phrase. Do not
    # fuzzy-match arbitrary strings; allow only whole-token containment.
    candidates = [
        name for name in roster
        if re.search(rf"(?<!\w){re.escape(raw)}(?!\w)", name.casefold())
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def explicit_attribution(context: str, roster: list[str]) -> Decision:
    before, _, after = context.partition("[[QUOTE]]")
    before = before[-260:]
    after = after[:260]
    nt = r"\[\[NAME:([^\]]+)\]\]"

    patterns: list[tuple[str, str, str, float]] = [
        ("after-verb-name", after, rf"\b(?:{SPEECH_VERBS})\b(?!\s+to\b)[^\[]{{0,90}}{nt}", .98),
        ("after-name-verb", after, rf"{nt}[^\[]{{0,90}}\b(?:{SPEECH_VERBS})\b", .97),
        # Material after the verb may contain an addressee ("Adam said to
        # Seth ..."); the grammatical subject is still the name before it.
        ("before-name-verb", before, rf"{nt}[^\[]{{0,220}}\b(?:{SPEECH_VERBS})\b.*$", .96),
        ("before-verb-name", before, rf"\b(?:{SPEECH_VERBS})\b(?!\s+to\b)[^\[]{{0,70}}{nt}[^\[]{{0,50}}$", .94),
    ]
    for method, text, pattern, confidence in patterns:
        matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
        if matches:
            raw_name = matches[-1].group(1)
            speaker = roster_match(raw_name, roster)
            if speaker:
                return Decision(speaker, confidence, method)
    return Decision()


def find_ancestor_paragraph(
    element: ET.Element, parent: dict[ET.Element, ET.Element]
) -> ET.Element | None:
    current = element
    while current in parent:
        current = parent[current]
        if local_name(current.tag) == "p":
            return current
    return None


def load_overrides(path: Path | None) -> dict[str, str]:
    if not path:
        return {}
    result: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            quote_id = norm_space(row.get("quote_id", ""))
            speaker = norm_space(row.get("speaker", ""))
            if quote_id and speaker:
                result[quote_id] = speaker
    return result


def add_or_update_personography(root: ET.Element, speakers: Iterable[str]) -> None:
    header = next((e for e in root.iter() if local_name(e.tag) == "teiHeader"), None)
    if header is None:
        return
    profile_desc = next(
        (e for e in header if local_name(e.tag) == "profileDesc"), None
    )
    if profile_desc is None:
        profile_desc = ET.SubElement(header, "profileDesc")
    partic_desc = next(
        (e for e in profile_desc if local_name(e.tag) == "particDesc"), None
    )
    if partic_desc is None:
        partic_desc = ET.SubElement(profile_desc, "particDesc")
    list_person = next(
        (e for e in partic_desc if local_name(e.tag) == "listPerson"), None
    )
    if list_person is None:
        list_person = ET.SubElement(partic_desc, "listPerson")

    existing = {e.get(XML_ID) for e in list_person if local_name(e.tag) == "person"}
    for speaker in sorted(set(speakers), key=str.casefold):
        person_id = "spk-" + slug(speaker)
        if person_id in existing:
            continue
        person = ET.SubElement(list_person, "person", {XML_ID: person_id})
        ET.SubElement(person, "persName").text = speaker


def write_review(
    path: Path,
    quotes: list[ET.Element],
    decisions: list[Decision],
    paragraph_numbers: list[int | None],
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "quote_id", "speaker", "confidence", "method", "paragraph", "text"
        ])
        for q, decision, paragraph_number in zip(quotes, decisions, paragraph_numbers):
            writer.writerow([
                q.get(XML_ID),
                decision.speaker or "",
                f"{decision.confidence:.2f}",
                decision.method,
                paragraph_number or "",
                quote_text(q),
            ])


def enrich(args: argparse.Namespace) -> Counter:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
    tree = ET.parse(args.input, parser=parser)
    root = tree.getroot()
    roster = read_roster(args.characters)
    overrides = load_overrides(args.overrides)
    parent = {child: node for node in root.iter() for child in node}
    paragraphs = [e for e in root.iter() if local_name(e.tag) == "p"]
    paragraph_index = {p: i + 1 for i, p in enumerate(paragraphs)}
    quotes = [e for e in root.iter() if local_name(e.tag) in QUOTE_TAGS]
    decisions = [Decision() for _ in quotes]
    quote_to_index = {q: i for i, q in enumerate(quotes)}
    paragraph_numbers: list[int | None] = []

    for number, q in enumerate(quotes, start=1):
        if not q.get(XML_ID):
            q.set(XML_ID, f"q{number:05d}")
        p = find_ancestor_paragraph(q, parent)
        paragraph_numbers.append(paragraph_index.get(p) if p is not None else None)
        if p is not None:
            decisions[number - 1] = explicit_attribution(render_context(p, q), roster)

    # All quotations in a paragraph normally belong to the same explicitly
    # attributed speaker. Apply only when exactly one speaker was found there.
    for p in paragraphs:
        p_quotes = [e for e in p.iter() if local_name(e.tag) in QUOTE_TAGS]
        indices = [quote_to_index[q] for q in p_quotes]
        speakers = {decisions[i].speaker for i in indices if decisions[i].speaker}
        if len(speakers) == 1:
            speaker = next(iter(speakers))
            for i in indices:
                if not decisions[i].speaker:
                    decisions[i] = Decision(speaker, .91, "same-paragraph")

    # A lone unresolved quote between two quotes attributed to the same speaker
    # is a safe continuation signal, provided all three are close in the text.
    for i in range(1, len(quotes) - 1):
        if decisions[i].speaker:
            continue
        left, right = decisions[i - 1], decisions[i + 1]
        pn = paragraph_numbers[i]
        if (
            left.speaker and left.speaker == right.speaker and pn
            and paragraph_numbers[i - 1]
            and paragraph_numbers[i + 1]
            and pn - paragraph_numbers[i - 1] <= 1
            and paragraph_numbers[i + 1] - pn <= 1
        ):
            decisions[i] = Decision(left.speaker, .78, "neighbor-continuation")

    for i, q in enumerate(quotes):
        qid = q.get(XML_ID, "")
        if qid in overrides:
            decisions[i] = Decision(overrides[qid], 1.0, "manual-override")
        decision = decisions[i]
        q.set("who", "#spk-" + slug(decision.speaker) if decision.speaker else "#unknown")
        q.set("ana", "#speaker-inferred" if decision.speaker else "#speaker-unresolved")
        q.set("cert", f"{decision.confidence:.2f}")
        q.set("resp", "#speaker-enrichment-script")
        q.set("source", decision.method)

    assigned_speakers = [d.speaker for d in decisions if d.speaker]
    add_or_update_personography(root, assigned_speakers)
    # Do not call ElementTree.indent(): this is mixed-content TEI, and generic
    # indentation can insert whitespace inside quotations around inline tags.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(args.output, encoding="utf-8", xml_declaration=True)
    write_review(args.review, quotes, decisions, paragraph_numbers)

    stats = Counter(d.method for d in decisions)
    stats["total"] = len(quotes)
    stats["assigned"] = sum(d.speaker is not None for d in decisions)
    stats["unresolved"] = sum(d.speaker is None for d in decisions)
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enrich TEI <q> or <quotation> elements with inferred speaker metadata."
    )
    parser.add_argument("input", type=Path, help="source TEI XML")
    parser.add_argument("characters", type=Path, help="one character name/alias per line")
    parser.add_argument("output", type=Path, help="enriched XML output")
    parser.add_argument(
        "--review", type=Path, default=Path("speaker_review.csv"),
        help="CSV containing every decision (default: speaker_review.csv)",
    )
    parser.add_argument(
        "--overrides", type=Path,
        help="optional CSV with quote_id,speaker columns for corrections",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        stats = enrich(args)
    except (OSError, ET.ParseError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Quotes: {stats['total']}")
    print(f"Assigned: {stats['assigned']}")
    print(f"Unresolved: {stats['unresolved']}")
    for method, count in sorted(stats.items()):
        if method not in {"total", "assigned", "unresolved"}:
            print(f"  {method}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
