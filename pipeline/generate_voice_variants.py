#!/usr/bin/env python3
"""Generate selectable voice variants from speaker-enriched TEI XML.

Uses Voicebox's local REST API. Narration, dialogue, and their ordering are
derived directly from the TEI mixed content. Generated clips are cached and a
checkpoint manifest makes interrupted runs resumable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator
import xml.etree.ElementTree as ET


XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
SPACE = re.compile(r"\s+")
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
QUOTE_TAGS = {"q", "quotation"}


@dataclass
class Segment:
    index: int
    kind: str
    text: str
    speaker_id: str
    gender: str
    profile: str
    quote_id: str | None = None
    source: str | None = None
    confidence: float | None = None


def voice_list(value: object) -> list[str]:
    """Normalize a profile setting so legacy strings and new arrays both work."""
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def clean_text(text: str) -> str:
    return SPACE.sub(" ", text).strip()


def text_of(element: ET.Element) -> str:
    return clean_text("".join(element.itertext()))


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def find_chapter(root: ET.Element, chapter: str) -> ET.Element:
    wanted_id = chapter if chapter.startswith("chapter") else f"chapter{chapter}"
    wanted_n = chapter.removeprefix("chapter")
    for element in root.iter():
        if local_name(element.tag) != "div":
            continue
        if element.get(XML_ID) == wanted_id:
            return element
        if element.get("type") == "chapter" and element.get("n") == wanted_n:
            return element
    raise ValueError(f"chapter {chapter!r} was not found")


def split_long_text(text: str, limit: int) -> list[str]:
    text = clean_text(text)
    if len(text) <= limit:
        return [text] if text else []
    sentences = SENTENCE_END.split(text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > limit:
            words = sentence.split()
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and len(candidate) > limit:
                    chunks.append(current)
                    current = word
                else:
                    current = candidate
            continue
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > limit:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def mixed_content_parts(block: ET.Element) -> Iterator[tuple[str, ET.Element | None]]:
    """Yield narration text and top-level quotations in reading order."""
    narrative: list[str] = []

    def flush() -> Iterator[tuple[str, ET.Element | None]]:
        value = clean_text("".join(narrative))
        narrative.clear()
        if value:
            yield value, None

    def walk(element: ET.Element, inside_quote: bool = False) -> Iterator[tuple[str, ET.Element | None]]:
        if element.text:
            narrative.append(element.text)
        for child in element:
            if local_name(child.tag) in QUOTE_TAGS and not inside_quote:
                yield from flush()
                value = text_of(child)
                if value:
                    yield value, child
            else:
                yield from walk(child, inside_quote or local_name(child.tag) in QUOTE_TAGS)
            if child.tail:
                narrative.append(child.tail)

    yield from walk(block)
    yield from flush()


def build_segments(
    chapter: ET.Element,
    config: dict,
    max_chars: int,
) -> list[Segment]:
    profiles = config.get("profiles", {})
    narrator_profiles = voice_list(profiles.get("narrator", "Alloy"))
    fallback_profiles = voice_list(profiles.get("unknown")) or narrator_profiles
    character_genders = {
        key.removeprefix("#spk-"): value.casefold()
        for key, value in config.get("character_genders", {}).items()
    }
    character_profiles = {
        key.removeprefix("#spk-"): value
        for key, value in config.get("character_profiles", {}).items()
    }
    segments: list[Segment] = []

    # Process chapter headings and paragraphs only. This avoids duplicating text
    # by iterating both containers and their descendants.
    blocks = [e for e in chapter.iter() if local_name(e.tag) in {"head", "p"}]
    for block in blocks:
        for text, quote in mixed_content_parts(block):
            if quote is None:
                kind, speaker_id, gender, candidate_profiles = (
                    "narration", "narrator", "neutral", narrator_profiles
                )
                quote_id = source = confidence = None
            else:
                who = quote.get("who", "#unknown")
                speaker_id = who.removeprefix("#spk-")
                quote_id = quote.get(XML_ID)
                source = quote.get("source")
                try:
                    confidence = float(quote.get("cert", "0"))
                except ValueError:
                    confidence = 0.0
                gender = character_genders.get(speaker_id, "unknown")
                if who == "#unknown" or confidence < float(config.get("minimum_confidence", .75)):
                    kind, gender, candidate_profiles = "unresolved-quote", "unknown", fallback_profiles
                else:
                    kind = "dialogue"
                    candidate_profiles = (
                        voice_list(character_profiles.get(speaker_id))
                        or voice_list(profiles.get(gender))
                    )
                    if not candidate_profiles:
                        raise ValueError(
                            f"no profile configured for speaker {speaker_id!r} "
                            f"with gender {gender!r}"
                        )

            for chunk in split_long_text(text, max_chars):
                # Merge adjacent narration so attributions around paragraphs do
                # not become unnecessarily tiny API requests.
                if (
                    segments and kind == "narration"
                    and segments[-1].kind == "narration"
                    and segments[-1].profile == candidate_profiles[0]
                    and len(segments[-1].text) + len(chunk) + 1 <= max_chars
                ):
                    segments[-1].text += " " + chunk
                    continue
                segments.append(Segment(
                    index=len(segments) + 1,
                    kind=kind,
                    text=chunk,
                    speaker_id=speaker_id,
                    gender=gender,
                    profile=candidate_profiles[0],
                    quote_id=quote_id,
                    source=source,
                    confidence=confidence,
                ))
                setattr(segments[-1], "candidate_profiles", candidate_profiles)
    return segments


class VoiceboxClient:
    def __init__(self, base_url: str, timeout: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, path: str, payload: dict | None = None) -> bytes:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            detail = body.strip() or exc.reason or "no response body"
            raise RuntimeError(
                f"Voicebox {exc.code} for {path}: {detail}"
            ) from exc

    def profiles(self) -> list[dict]:
        return json.loads(self._request("/profiles"))

    def generate(self, payload: dict) -> dict:
        return json.loads(self._request("/generate", payload))

    def audio(self, generation_id: str) -> bytes:
        return self._request(f"/audio/{generation_id}")

    def generation_status(self, generation_id: str) -> dict:
        raw = self._request(f"/generate/{generation_id}/status")
        if not raw.strip():
            # Some Voicebox builds return HTTP 200/204 with no body while the
            # background job is still in progress.
            return {"status": "generating"}
        decoded = raw.decode("utf-8", errors="replace")
        # Current Voicebox builds expose this endpoint as an SSE stream. A
        # single request may contain generating ... completed events and close
        # only after reaching a terminal state. Use the final valid data event.
        if any(line.startswith("data:") for line in decoded.splitlines()):
            events: list[dict] = []
            for line in decoded.splitlines():
                if not line.startswith("data:"):
                    continue
                payload = line.removeprefix("data:").strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"Voicebox returned an invalid SSE status event for "
                        f"{generation_id}: {payload[:500]!r}"
                    ) from exc
                if isinstance(event, dict):
                    events.append(event)
            if events:
                return events[-1]
            return {"status": "generating"}
        try:
            result = json.loads(decoded)
        except json.JSONDecodeError as exc:
            preview = decoded[:500]
            raise RuntimeError(
                f"Voicebox returned a non-JSON status response for "
                f"{generation_id}: {preview!r}"
            ) from exc
        if isinstance(result, str):
            return {"status": result}
        if not isinstance(result, dict):
            raise RuntimeError(
                f"Voicebox returned an invalid status response for "
                f"{generation_id}: {result!r}"
            )
        return result

    def wait_for_generation(
        self,
        generation_id: str,
        initial: dict,
        timeout_seconds: int,
        poll_seconds: float = 1.0,
    ) -> dict:
        complete = {"completed", "complete", "ready", "succeeded", "success"}
        failed = {"failed", "error", "cancelled", "canceled"}
        result = initial
        deadline = time.monotonic() + timeout_seconds
        while True:
            status = str(result.get("status", "")).casefold()
            if status in complete:
                return result
            if status in failed:
                detail = result.get("error") or result.get("detail") or result
                raise RuntimeError(
                    f"Voicebox generation {generation_id} {status}: {detail}"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Voicebox generation {generation_id} timed out after "
                    f"{timeout_seconds} seconds (last status: {status or 'unknown'})"
                )
            time.sleep(poll_seconds)
            result = self.generation_status(generation_id)


def resolve_profiles(client: VoiceboxClient, segments: list[Segment]) -> dict[str, str]:
    available = client.profiles()
    by_name = {str(p.get("name", "")).casefold(): str(p.get("id")) for p in available}
    by_id = {str(p.get("id")): str(p.get("id")) for p in available}
    wanted = sorted({profile for s in segments for profile in getattr(s, "candidate_profiles", [s.profile])})
    resolved: dict[str, str] = {}
    for value in wanted:
        profile_id = by_id.get(value) or by_name.get(value.casefold())
        if not profile_id:
            names = ", ".join(sorted(p.get("name", "") for p in available))
            raise ValueError(f"Voicebox profile {value!r} not found. Available: {names}")
        resolved[value] = profile_id
    return resolved


def stable_key(segment: Segment, profile: str, instruction: str, engine: str | None) -> str:
    material = json.dumps(
        [segment.text, profile, instruction, engine], ensure_ascii=False
    ).encode()
    return hashlib.sha256(material).hexdigest()[:16]


def generate_clips(args: argparse.Namespace, segments: list[Segment], config: dict) -> None:
    client = VoiceboxClient(args.url, args.timeout)
    profile_ids = resolve_profiles(client, segments)
    args.output.mkdir(parents=True, exist_ok=True)
    clips_dir = args.output / "clips"
    clips_dir.mkdir(exist_ok=True)
    manifest_path = args.output / "manifest.json"
    instruction = config.get(
        "instruction",
        "Speak in a natural, consistent British English accent suitable for a literary audiobook.",
    )
    engine = config.get("engine")
    model_size = config.get("model_size")

    for segment in segments:
        options: list[dict] = []
        candidates = getattr(segment, "candidate_profiles", [segment.profile])
        for voice_number, profile in enumerate(candidates, start=1):
            key = stable_key(segment, profile, instruction, engine)
            safe_profile = re.sub(r"[^a-zA-Z0-9]+", "-", profile).strip("-").lower()
            clip = clips_dir / f"{segment.index:04d}_{segment.kind}_{safe_profile}_{key}.wav"
            option = {"profile": profile, "clip": str(clip)}
            if clip.exists() and clip.stat().st_size > 44:
                print(f"[{segment.index}/{len(segments)} · {voice_number}/{len(candidates)}] cached {profile}")
                options.append(option)
                continue
            payload = {
                "profile_id": profile_ids[profile],
                "text": segment.text,
                "language": config.get("language", "en"),
            }
            if instruction:
                payload["instruct"] = instruction
            if engine:
                payload["engine"] = engine
            if model_size:
                payload["model_size"] = model_size
            print(f"[{segment.index}/{len(segments)} · {voice_number}/{len(candidates)}] {profile} — {segment.text[:65]}")
            for attempt in range(1, args.retries + 1):
                try:
                    result = client.generate(payload)
                    generation_id = result.get("id") or result.get("generation_id")
                    if not generation_id:
                        raise RuntimeError(f"Voicebox response has no generation id: {result}")
                    result = client.wait_for_generation(generation_id, result, args.timeout)
                    clip.write_bytes(client.audio(generation_id))
                    option["generation_id"] = generation_id
                    if result.get("duration") is not None:
                        option["duration_seconds"] = result.get("duration")
                    break
                except (urllib.error.URLError, TimeoutError, KeyError) as exc:
                    if attempt == args.retries:
                        raise RuntimeError(f"generation failed for segment {segment.index}, voice {profile}: {exc}")
                    time.sleep(min(2 ** attempt, 10))
            options.append(option)
            setattr(segment, "voice_options", options)
            setattr(segment, "clip", options[0]["clip"])
            write_manifest(manifest_path, segments, config)
        setattr(segment, "voice_options", options)
        setattr(segment, "clip", options[0]["clip"])

    write_manifest(manifest_path, segments, config)


def write_manifest(path: Path, segments: list[Segment], config: dict) -> None:
    records = []
    for segment in segments:
        record = asdict(segment)
        record["clip"] = getattr(segment, "clip", None)
        record["generation_id"] = getattr(segment, "generation_id", None)
        record["duration_seconds"] = getattr(segment, "duration_seconds", None)
        record["voice_options"] = getattr(
            segment,
            "voice_options",
            [{"profile": profile, "clip": None} for profile in getattr(segment, "candidate_profiles", [segment.profile])],
        )
        records.append(record)
    path.write_text(
        json.dumps({"config": config, "segments": records}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def concatenate_wav(segments: list[Segment], output: Path, pause_ms: int) -> None:
    clips = [Path(getattr(s, "clip")) for s in segments]
    if not clips:
        raise ValueError("no audio clips were generated")
    try:
        with wave.open(str(clips[0]), "rb") as first:
            params = first.getparams()
            frames = [first.readframes(first.getnframes())]
        silence = b"\x00" * int(params.framerate * pause_ms / 1000) * params.nchannels * params.sampwidth
        for clip in clips[1:]:
            with wave.open(str(clip), "rb") as source:
                current = source.getparams()
                current_format = (
                    current.nchannels, current.sampwidth,
                    current.framerate, current.comptype,
                )
                expected_format = (
                    params.nchannels, params.sampwidth,
                    params.framerate, params.comptype,
                )
                if current_format != expected_format:
                    raise ValueError("WAV formats differ")
                frames.extend([silence, source.readframes(source.getnframes())])
        with wave.open(str(output), "wb") as target:
            target.setparams(params)
            for frame in frames:
                target.writeframes(frame)
        return
    except (wave.Error, ValueError):
        pass

    # Fallback handles differing sample rates/codecs when FFmpeg is installed.
    concat_file = output.parent / "ffmpeg_concat.txt"
    concat_file.write_text(
        "".join(f"file '{clip.resolve().as_posix()}'\n" for clip in clips), encoding="utf-8"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file), str(output)],
        check=True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path, help="speaker-enriched TEI XML")
    parser.add_argument("config", type=Path, help="voice/gender configuration JSON")
    parser.add_argument("--chapter", default="1")
    parser.add_argument("--output", type=Path, default=Path("chapter1_audio"))
    parser.add_argument("--url", default="http://127.0.0.1:17493")
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--pause-ms", type=int, default=250)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        config = load_json(args.config)
        root = ET.parse(args.xml).getroot()
        chapter = find_chapter(root, args.chapter)
        segments = build_segments(chapter, config, args.max_chars)
        args.output.mkdir(parents=True, exist_ok=True)
        write_manifest(args.output / "manifest.json", segments, config)
        print(f"Prepared {len(segments)} segments for Chapter {args.chapter}")
        if args.dry_run:
            print(f"Dry run: {args.output / 'manifest.json'}")
            return 0
        generate_clips(args, segments, config)
        final = args.output / f"chapter_{args.chapter}.wav"
        concatenate_wav(segments, final, args.pause_ms)
        print(f"Finished: {final}")
        return 0
    except (OSError, ET.ParseError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
