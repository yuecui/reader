const state = {
  manifest: null,
  segments: [],
  files: new Map(),
  baseUrl: null,
  manifestUrl: null,
  index: 0,
  loadedIndex: -1,
  autoScroll: true,
  wordTracking: getStoredPreference("living-pages-word-tracking") !== "off",
  session: [],
  objectUrl: null,
};

const $ = (id) => document.getElementById(id);
const audio = $("audio");

function getStoredPreference(key) {
  try { return window.localStorage.getItem(key); }
  catch { return null; }
}

function setStoredPreference(key, value) {
  try { window.localStorage.setItem(key, value); }
  catch { /* Reading still works when storage is blocked. */ }
}

document.querySelectorAll("[data-folder-input], #folderInput").forEach(input => {
  input.addEventListener("change", event => loadFolder([...event.target.files]));
});

async function loadFolder(files) {
  try {
    const manifestFile = files.find(file => file.name === "manifest.json");
    if (!manifestFile) throw new Error("No manifest.json was found in that folder.");
    const manifest = JSON.parse(await manifestFile.text());
    if (!Array.isArray(manifest.segments) || !manifest.segments.length) {
      throw new Error("The manifest contains no audio segments.");
    }
    state.manifest = manifest;
    state.segments = manifest.segments;
    state.baseUrl = null;
    state.manifestUrl = null;
    state.files.clear();
    for (const file of files) {
      if (/\.(wav|mp3|m4a|ogg)$/i.test(file.name)) state.files.set(file.name, file);
    }
    const matched = state.segments.filter(segment => findClip(segment)).length;
    if (!matched) throw new Error("Manifest loaded, but no matching files were found in clips/.");
    renderReader();
    selectSegment(0, false);
    toast(`Loaded ${state.segments.length} passages · ${matched} audio clips matched`);
  } catch (error) {
    toast(error.message || String(error));
  }
}

async function loadManifestUrl(value) {
  try {
    const url = new URL(value, window.location.href);
    toast("Loading hosted chapter…");
    const response = await fetch(url.href, { cache: "no-store" });
    if (!response.ok) throw new Error(`Manifest request failed: ${response.status} ${response.statusText}`);
    const manifest = await response.json();
    if (!Array.isArray(manifest.segments) || !manifest.segments.length) {
      throw new Error("The hosted manifest contains no audio segments.");
    }
    state.manifest = manifest;
    state.segments = manifest.segments;
    state.files.clear();
    state.baseUrl = new URL(".", url.href);
    state.manifestUrl = url.href;
    renderReader();
    selectSegment(0, false);
    const pageUrl = new URL(window.location.href);
    pageUrl.searchParams.set("manifest", url.href);
    history.replaceState(null, "", pageUrl);
    setUrlDialogOpen(false);
    toast(`Loaded ${state.segments.length} hosted passages`);
  } catch (error) {
    toast(error.message || String(error));
  }
}

function clipBasename(segment) {
  const value = segment.clip || "";
  return value.split(/[\\/]/).pop();
}

function findClip(segment) {
  if (state.baseUrl) {
    if (segment.audio_url) return new URL(segment.audio_url, state.baseUrl).href;
    const name = clipBasename(segment);
    return name ? new URL(`clips/${encodeURIComponent(name)}`, state.baseUrl).href : null;
  }
  return state.files.get(clipBasename(segment));
}

function renderReader() {
  $("welcome").hidden = true;
  $("reader").hidden = false;
  $("player").hidden = false;
  const chapterName = inferChapterName();
  $("chapterLabel").textContent = chapterName;
  $("chapterKicker").textContent = chapterName;
  const first = state.segments[0]?.text || "Chapter";
  $("chapterTitle").textContent = first.length < 80 ? first : "The Workshop";
  const container = $("passages");
  container.replaceChildren();
  state.segments.forEach((segment, index) => {
    const passage = document.createElement("section");
    passage.className = `passage ${segment.kind === "dialogue" ? "dialogue" : "narration"}`;
    passage.dataset.index = index;
    passage.tabIndex = 0;
    passage.setAttribute("role", "button");
    passage.setAttribute("aria-label", `Play passage ${index + 1}`);
    if (segment.kind !== "narration") {
      const tag = document.createElement("span");
      tag.className = "speaker-tag";
      tag.textContent = displaySpeaker(segment);
      passage.append(tag);
    }
    const words = String(segment.text || "").split(/(\s+)/);
    words.forEach(value => {
      if (/^\s+$/.test(value)) return passage.append(document.createTextNode(value));
      const word = document.createElement("span");
      word.className = "word";
      word.textContent = value;
      passage.append(word);
    });
    passage.addEventListener("click", () => selectSegment(index, true));
    passage.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") selectSegment(index, true);
    });
    container.append(passage);
  });
}

function inferChapterName() {
  if (state.manifestUrl) {
    const match = state.manifestUrl.match(/chapter[_ -]?(\d+)/i);
    return match ? `Chapter ${Number(match[1])}` : "Hosted chapter";
  }
  const path = [...state.files.values()][0]?.webkitRelativePath || "";
  const match = path.match(/chapter[_ -]?(\d+)/i);
  return match ? `Chapter ${Number(match[1])}` : "Chapter reading";
}

function displaySpeaker(segment) {
  if (segment.kind === "narration") return "Narrator";
  if (segment.kind === "unresolved-quote") return "Unresolved voice";
  return String(segment.speaker_id || "Character").replaceAll("-", " ").replace(/\b\w/g, c => c.toUpperCase());
}

function selectSegment(index, autoplay) {
  index = Math.max(0, Math.min(index, state.segments.length - 1));
  const segment = state.segments[index];
  const clip = findClip(segment);
  if (!clip) return toast(`Audio clip missing for passage ${index + 1}: ${clipBasename(segment)}`);
  state.index = index;
  document.querySelectorAll(".passage.active").forEach(el => el.classList.remove("active"));
  document.querySelectorAll(".word.spoken").forEach(el => el.classList.remove("spoken"));
  const passage = document.querySelector(`.passage[data-index="${index}"]`);
  passage?.classList.add("active");
  if (state.autoScroll) passage?.scrollIntoView({ behavior: "smooth", block: "center" });
  if (state.loadedIndex !== index) {
    if (state.objectUrl) URL.revokeObjectURL(state.objectUrl);
    if (typeof clip === "string") {
      state.objectUrl = null;
      audio.src = clip;
    } else {
      state.objectUrl = URL.createObjectURL(clip);
      audio.src = state.objectUrl;
    }
    audio.playbackRate = Number($("speed").value);
    state.loadedIndex = index;
  }
  updateMetadata();
  state.session.push({ at: new Date().toISOString(), action: autoplay ? "play" : "select", index: index + 1, speaker: segment.speaker_id, profile: segment.profile, quote_id: segment.quote_id });
  if (autoplay) audio.play().catch(error => toast(error.message));
}

function updateMetadata() {
  const segment = state.segments[state.index];
  const speaker = displaySpeaker(segment);
  const initial = (segment.profile || speaker || "V")[0].toUpperCase();
  $("voiceOrb").textContent = initial;
  $("miniOrb").textContent = initial;
  $("currentVoice").textContent = segment.profile || "Unknown";
  $("currentSpeaker").textContent = speaker;
  $("segmentNumber").textContent = `${state.index + 1} / ${state.segments.length}`;
  $("segmentKind").textContent = String(segment.kind || "passage").replaceAll("-", " ");
  $("quoteId").textContent = segment.quote_id || "—";
  $("confidence").textContent = segment.confidence == null ? "—" : `${Math.round(segment.confidence * 100)}%`;
  $("trackSpeaker").textContent = `${speaker} · ${segment.profile || "Unknown"}`;
  $("trackText").textContent = segment.text;
}

function updateProgress() {
  const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
  const ratio = duration ? audio.currentTime / duration : 0;
  $("seek").value = String(Math.round(ratio * 1000));
  $("currentTime").textContent = formatTime(audio.currentTime);
  $("duration").textContent = formatTime(duration);
  const words = [...document.querySelectorAll(`.passage[data-index="${state.index}"] .word`)];
  const spoken = state.wordTracking ? Math.ceil(words.length * ratio) : 0;
  words.forEach((word, index) => word.classList.toggle("spoken", state.wordTracking && index < spoken));
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds)) return "0:00";
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;
}

audio.addEventListener("timeupdate", updateProgress);
audio.addEventListener("loadedmetadata", updateProgress);
audio.addEventListener("play", () => { $("playPause").textContent = "Ⅱ"; $("playPause").setAttribute("aria-label", "Pause"); });
audio.addEventListener("pause", () => { $("playPause").textContent = "▶"; $("playPause").setAttribute("aria-label", "Play"); });
audio.addEventListener("ended", () => {
  if (state.index < state.segments.length - 1) selectSegment(state.index + 1, true);
  else toast("Chapter complete");
});
audio.addEventListener("error", () => {
  if (audio.src) toast(`Unable to load audio for passage ${state.index + 1}. Check the hosted clips/ path.`);
});

$("playPause").addEventListener("click", () => {
  if (!state.segments.length) return;
  if (state.loadedIndex < 0) selectSegment(0, true);
  else if (audio.paused) audio.play(); else audio.pause();
});
$("previous").addEventListener("click", () => selectSegment(state.index - 1, true));
$("next").addEventListener("click", () => selectSegment(state.index + 1, true));
$("seek").addEventListener("input", event => {
  if (Number.isFinite(audio.duration)) audio.currentTime = audio.duration * Number(event.target.value) / 1000;
});
$("speed").addEventListener("change", event => { audio.playbackRate = Number(event.target.value); });
$("autoScroll").addEventListener("click", event => {
  state.autoScroll = !state.autoScroll;
  event.currentTarget.classList.toggle("active", state.autoScroll);
  event.currentTarget.setAttribute("aria-pressed", String(state.autoScroll));
});
$("wordTracking").classList.toggle("active", state.wordTracking);
$("wordTracking").setAttribute("aria-pressed", String(state.wordTracking));
$("wordTracking").addEventListener("click", event => {
  state.wordTracking = !state.wordTracking;
  setStoredPreference("living-pages-word-tracking", state.wordTracking ? "on" : "off");
  event.currentTarget.classList.toggle("active", state.wordTracking);
  event.currentTarget.setAttribute("aria-pressed", String(state.wordTracking));
  if (!state.wordTracking) {
    document.querySelectorAll(".word.spoken").forEach(word => word.classList.remove("spoken"));
  } else {
    updateProgress();
  }
});
$("focusToggle").addEventListener("click", event => {
  const active = document.body.classList.toggle("focus-mode");
  event.currentTarget.setAttribute("aria-pressed", String(active));
});
$("focusExit").addEventListener("click", exitFocusMode);

function openUrlDialog() {
  setUrlDialogOpen(true);
  setTimeout(() => $("manifestUrl").focus(), 0);
}

function setUrlDialogOpen(open) {
  const dialog = $("urlDialog");
  if (open) {
    try {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    } catch {
      dialog.setAttribute("open", "");
    }
    dialog.classList.add("dialog-visible");
    document.body.classList.add("dialog-open");
    return;
  }
  if (typeof dialog.close === "function" && dialog.open) dialog.close();
  else dialog.removeAttribute("open");
  dialog.classList.remove("dialog-visible");
  document.body.classList.remove("dialog-open");
}
$("urlToggle").addEventListener("click", openUrlDialog);
$("urlHeroButton").addEventListener("click", openUrlDialog);
$("dialogClose").addEventListener("click", () => setUrlDialogOpen(false));
$("urlForm").addEventListener("submit", event => {
  event.preventDefault();
  loadManifestUrl($("manifestUrl").value);
});

function exitFocusMode() {
  document.body.classList.remove("focus-mode");
  $("focusToggle").setAttribute("aria-pressed", "false");
  $("focusToggle").focus();
}
$("downloadLog").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify({ exported_at: new Date().toISOString(), session: state.session }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = Object.assign(document.createElement("a"), { href: url, download: "immersive-reader-session.json" });
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 500);
});

document.addEventListener("keydown", event => {
  if (event.key === "Escape" && document.body.classList.contains("focus-mode")) {
    exitFocusMode();
    return;
  }
  if (event.target.matches("input, select, button")) return;
  if (event.code === "Space") { event.preventDefault(); $("playPause").click(); }
  if (event.key === "ArrowRight") selectSegment(state.index + 1, true);
  if (event.key === "ArrowLeft") selectSegment(state.index - 1, true);
});

let toastTimer;
function toast(message) {
  const element = $("toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => element.classList.remove("show"), 4200);
}

const initialManifestUrl = new URL(window.location.href).searchParams.get("manifest");
if (initialManifestUrl) {
  $("manifestUrl").value = initialManifestUrl;
  loadManifestUrl(initialManifestUrl);
}
