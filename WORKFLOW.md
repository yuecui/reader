# Fiction XML → selectable multi-voice audiobook

This project uses a reviewable three-stage workflow. Speaker inference is never
treated as perfect: every quotation receives a confidence score and method, and
uncertain results remain visible for human correction before TTS work.

## 1. Prepare and identify speakers

```text
book/
├── book.xml
├── characters.txt        # one canonical character name per line
├── speaker_overrides.csv # optional corrections
└── voice_config.json
```

The TEI should use `<div type="chapter" n="1">`, `<p>`, either `<q>` or
`<quotation>`, and preferably `<name>` around character mentions. Run the first pass:

```bash
python3 pipeline/identify_speakers.py \
  book/book.xml book/characters.txt book/book.enriched.xml \
  --review book/speaker_review.csv
```

Review blank speakers and confidence below `0.75`. Put corrections in
`speaker_overrides.csv` using `quote_id,speaker`, then rerun:

```bash
python3 pipeline/identify_speakers.py \
  book/book.xml book/characters.txt book/book.enriched.xml \
  --review book/speaker_review.csv \
  --overrides book/speaker_overrides.csv
```

The enriched TEI records `who`, `cert`, `source`, and `resp` on every `<q>`.
Do not generate final audio until unresolved and low-confidence rows are reviewed.

## 2. Configure voices

First copy the template, then generate a first-pass gender mapping and insert
it into the voice configuration:

```bash
cp pipeline/workflow_config.example.json book/voice_config.json
```

```bash
python3 pipeline/identify_character_genders.py \
  book/book.enriched.xml book/characters.txt \
  --output book/character_genders.json \
  --review book/gender_review.csv \
  --update-config book/voice_config.json
```

Review `gender_review.csv`. Add corrections to a CSV containing
`character_id,gender`, then rerun with `--overrides`. The program uses explicit
TEI person metadata first, honorifics second, and nearby pronouns/kinship terms
third. Ambiguous characters remain `unknown`.

While Voicebox is open, list exact installed profile names with:

```bash
curl http://127.0.0.1:17493/profiles
```

| Field | What it controls |
|---|---|
| `profiles.narrator` | Voice choices for narration |
| `profiles.male` | Every selectable male voice |
| `profiles.female` | Every selectable female voice |
| `profiles.unknown` | Choices for unresolved dialogue |
| `character_genders` | Speaker ID → `male`, `female`, or `unknown` |
| `character_profiles` | Optional speaker-specific choices overriding the gender pool |
| `minimum_confidence` | Below this, use unknown voices |
| `engine` / `model_size` | Voicebox generation backend |
| `instruction` | Accent, pace, emotion, and performance direction |

Speaker IDs are lowercase slugs: `Mr. Arthur Donnithorne` becomes
`mr-arthur-donnithorne`. Profile names must exactly match `/profiles`. Engines
that ignore `instruction` cannot be forced into a reliable accent; use profiles
natively recorded or trained for that accent.

## 3. Preview, then generate

```bash
python3 pipeline/generate_voice_variants.py \
  book/book.enriched.xml book/voice_config.json \
  --chapter 1 --output book/audio/chapter_1 --dry-run
```

Review the manifest, remove `--dry-run`, and rerun. Existing valid clips are
reused, so interrupted runs resume safely. Each segment gets `voice_options`;
`clip` and `profile` keep the first voice for compatibility with old readers.
Requests are approximately `segments × voices for that gender`, so start with
one chapter and a small voice pool.

Generate all chapters by changing `55` to the book's chapter count:

```bash
for chapter in {1..55}; do
  python3 pipeline/generate_voice_variants.py \
    book/book.enriched.xml book/voice_config.json \
    --chapter "$chapter" --output "book/audio/chapter_${chapter}"
done
```

## 4. Reader and hosting

Run `python3 serve_reader.py` and choose a generated chapter folder. **Current
voice** lists variants for the active speaker. The preference is remembered per
speaker and reused later. For hosting, publish `manifest.json` beside `clips/`.
Use a raw JSON or GitHub Pages URL, not a GitHub `/blob/` webpage.

## Where to customize code

| File/function | Change when… |
|---|---|
| `identify_speakers.py` → `SPEECH_VERBS` | The fiction uses more attribution verbs |
| `explicit_attribution()` | You need new grammar or punctuation patterns |
| `enrich()` | You want additional context/inference rules |
| `generate_voice_variants.py` → `split_long_text()` | The engine needs different request lengths |
| `build_segments()` | The TEI uses different chapter/block elements |
| `VoiceboxClient` | Voicebox API endpoints change |
| `voice_config.json` | Voices, genders, accent, engine, or threshold change |
| `app.js` → `voiceOptions()` | The manifest voice schema changes |
| `currentVoice` change handler | Selection should be per gender/book, not speaker |

Keep inference conservative. A wrong confident label can generate many wrong
files; an unresolved label is visible and cheap to correct.
