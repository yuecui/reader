#!/usr/bin/env python3
"""Infer character genders from TEI evidence and produce a reviewable mapping.

The program uses, in order: manual overrides, TEI person metadata, honorifics,
and pronouns/kinship terms near character mentions. Ambiguous results remain
"unknown". This is textual classification for voice casting, not a claim about
a real person's identity.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET


XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
SPACE = re.compile(r"\s+")
MALE_WORDS = {"he", "him", "his", "himself", "man", "boy", "father", "son", "brother", "husband", "gentleman"}
FEMALE_WORDS = {"she", "her", "hers", "herself", "woman", "girl", "mother", "daughter", "sister", "wife", "lady"}
MALE_TITLES = re.compile(r"^(?:mr\.?|sir|lord|master|father|brother)\s+", re.I)
FEMALE_TITLES = re.compile(r"^(?:mrs\.?|miss|ms\.?|lady|madam|dame|mother|sister)\s+", re.I)
VALID_GENDERS = {"male", "female", "unknown"}


@dataclass
class Decision:
    character_id: str
    name: str
    gender: str = "unknown"
    confidence: float = 0.0
    method: str = "unresolved"
    evidence: str = ""


def local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def clean(text: str) -> str:
    return SPACE.sub(" ", text).strip()


def slug(text: str) -> str:
    value = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower() or "unknown"


def normalize_gender(value: str | None) -> str | None:
    value = clean(value or "").casefold()
    aliases = {
        "m": "male", "man": "male", "masculine": "male", "1": "male",
        "f": "female", "woman": "female", "feminine": "female", "2": "female",
        "u": "unknown", "unspecified": "unknown", "other": "unknown", "0": "unknown",
    }
    result = aliases.get(value, value)
    return result if result in VALID_GENDERS else None


def read_characters(path: Path) -> list[str]:
    seen: set[str] = set()
    names: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name = clean(line)
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            names.append(name)
    if not names:
        raise ValueError(f"no character names found in {path}")
    return names


def read_overrides(path: Path | None) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    result: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            character_id = slug(row.get("character_id", ""))
            gender = normalize_gender(row.get("gender"))
            if character_id != "unknown" and gender:
                result[character_id] = gender
    return result


def person_metadata(root: ET.Element) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for person in root.iter():
        if local_name(person.tag) != "person":
            continue
        person_id = (person.get(XML_ID) or "").removeprefix("spk-")
        names = [clean("".join(node.itertext())) for node in person.iter() if local_name(node.tag) == "persName"]
        raw = person.get("sex") or person.get("gender")
        if not raw:
            sex = next((node for node in person.iter() if local_name(node.tag) in {"sex", "gender"}), None)
            if sex is not None:
                raw = sex.get("value") or sex.get("type") or clean("".join(sex.itertext()))
        gender = normalize_gender(raw)
        if not gender:
            continue
        label = names[0] if names else person_id
        if person_id:
            result[person_id] = (gender, f"TEI person {person_id}")
        for name in names:
            result[slug(name)] = (gender, f"TEI person {label}")
    return result


def context_evidence(text: str, name: str, window: int) -> tuple[int, int, list[str]]:
    pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.I)
    male = female = 0
    samples: list[str] = []
    for match in pattern.finditer(text):
        start = max(0, match.start() - window)
        end = min(len(text), match.end() + window)
        context = text[start:end]
        tokens = re.findall(r"[A-Za-z]+", context.casefold())
        male += sum(token in MALE_WORDS for token in tokens)
        female += sum(token in FEMALE_WORDS for token in tokens)
        if len(samples) < 3:
            samples.append(clean(context))
    return male, female, samples


def decide(name: str, text: str, metadata: dict[str, tuple[str, str]], override: str | None, window: int) -> Decision:
    character_id = slug(name)
    if override:
        return Decision(character_id, name, override, 1.0, "manual-override", "gender_overrides.csv")
    if character_id in metadata:
        gender, evidence = metadata[character_id]
        return Decision(character_id, name, gender, 1.0, "tei-person", evidence)
    if MALE_TITLES.match(name):
        return Decision(character_id, name, "male", .99, "honorific", name.split()[0])
    if FEMALE_TITLES.match(name):
        return Decision(character_id, name, "female", .99, "honorific", name.split()[0])
    male, female, samples = context_evidence(text, name, window)
    total = male + female
    margin = abs(male - female)
    if total >= 2 and margin >= 2:
        gender = "male" if male > female else "female"
        confidence = min(.96, .62 + .22 * margin / total + .02 * min(total, 6))
        evidence = f"male_terms={male}; female_terms={female}; contexts=" + " | ".join(samples)
        return Decision(character_id, name, gender, confidence, "context-terms", evidence)
    evidence = f"male_terms={male}; female_terms={female}"
    return Decision(character_id, name, "unknown", 0.0, "unresolved", evidence)


def write_review(path: Path, decisions: list[Decision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["character_id", "name", "gender", "confidence", "method", "evidence"])
        for item in decisions:
            writer.writerow([item.character_id, item.name, item.gender, f"{item.confidence:.2f}", item.method, item.evidence])


def update_config(path: Path, mapping: dict[str, str]) -> None:
    document: dict = {}
    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"{path} must contain a JSON object")
    document["character_genders"] = mapping
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path, help="source or speaker-enriched TEI XML")
    parser.add_argument("characters", type=Path, help="one canonical character name per line")
    parser.add_argument("--output", type=Path, default=Path("character_genders.json"))
    parser.add_argument("--review", type=Path, default=Path("gender_review.csv"))
    parser.add_argument("--overrides", type=Path, help="CSV with character_id,gender corrections")
    parser.add_argument("--update-config", type=Path, help="write mapping into this voice_config.json")
    parser.add_argument("--context-chars", type=int, default=240)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        root = ET.parse(args.xml).getroot()
        names = read_characters(args.characters)
        overrides = read_overrides(args.overrides)
        metadata = person_metadata(root)
        text = clean(" ".join(root.itertext()))
        decisions = [decide(name, text, metadata, overrides.get(slug(name)), args.context_chars) for name in names]
        mapping = {item.character_id: item.gender for item in decisions}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(mapping, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        write_review(args.review, decisions)
        if args.update_config:
            update_config(args.update_config, mapping)
        counts = {gender: sum(item.gender == gender for item in decisions) for gender in VALID_GENDERS}
        print(f"Characters: {len(decisions)}")
        print(f"Male: {counts['male']} · Female: {counts['female']} · Unknown: {counts['unknown']}")
        print(f"Mapping: {args.output}")
        print(f"Review: {args.review}")
        if args.update_config:
            print(f"Updated config: {args.update_config}")
        return 0
    except (OSError, ET.ParseError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
