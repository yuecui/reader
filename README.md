# Living Pages — Immersive Audiobook Reader

A dependency-free local webpage for Voicebox chapter manifests and generated
audio clips. It highlights the active passage, progressively highlights words,
auto-scrolls, displays speaker/voice metadata, and logs the listening session.

Word-level underlining is optional. Use the **Word tracking** button in the
player to enable or disable it. The choice is remembered in browser storage;
active-passage highlighting continues in either mode.

## Run

```bash
cd immersive_reader
python3 serve_reader.py
```

Open `http://127.0.0.1:8765`, choose a generated chapter folder such as
`chapter1_audio`, and grant the browser access to that folder. It must contain:

```text
chapter1_audio/
├── manifest.json
├── chapter_1.wav
└── clips/
    ├── 0001_narration_....wav
    └── ...
```

The browser matches each manifest segment to its clip filename. Files stay on
the device; nothing is uploaded.

## Complete fiction workflow

See [WORKFLOW.md](WORKFLOW.md) for the TEI speaker-identification review loop,
gender-based multi-voice generation, configuration reference, batch chapter
commands, and code customization points. New manifests may contain a
`voice_options` array; the reader exposes those choices in the **Current voice**
menu and remembers the chosen voice for each speaker. Older single-voice
manifests remain supported.

## Synchronization

Clip boundaries give exact segment-level synchronization. Within the active
segment, word highlighting is estimated proportionally from playback time. For
word-perfect synchronization, extend the generation pipeline with forced
alignment and store per-word start/end times in the manifest.
