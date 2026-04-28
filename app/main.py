from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

DATA_DIR = Path(os.environ.get("DATA_DIR", "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
CACHE_DIR = DATA_DIR / "cache"

MAX_UPLOAD_FILES = int(os.environ.get("MAX_UPLOAD_FILES", "100"))
MAX_TOTAL_CHARS = int(os.environ.get("MAX_TOTAL_CHARS", "1000000"))
MAX_CHARS_PER_CHUNK = int(os.environ.get("MAX_CHARS_PER_CHUNK", "4500"))
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "1"))

DEFAULT_VOICE = os.environ.get("DEFAULT_VOICE", "en-US-AriaNeural")

for directory in (DATA_DIR, JOBS_DIR, CACHE_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="StoryCast", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_job_semaphore = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

FALLBACK_VOICES = [
    {"short_name": "en-US-AriaNeural", "display_name": "Aria", "locale": "en-US", "gender": "Female"},
    {"short_name": "en-US-GuyNeural", "display_name": "Guy", "locale": "en-US", "gender": "Male"},
    {"short_name": "en-US-JennyNeural", "display_name": "Jenny", "locale": "en-US", "gender": "Female"},
    {"short_name": "en-US-DavisNeural", "display_name": "Davis", "locale": "en-US", "gender": "Male"},
    {"short_name": "en-US-MichelleNeural", "display_name": "Michelle", "locale": "en-US", "gender": "Female"},
    {"short_name": "en-GB-SoniaNeural", "display_name": "Sonia", "locale": "en-GB", "gender": "Female"},
    {"short_name": "en-GB-RyanNeural", "display_name": "Ryan", "locale": "en-GB", "gender": "Male"},
    {"short_name": "en-AU-NatashaNeural", "display_name": "Natasha", "locale": "en-AU", "gender": "Female"},
    {"short_name": "en-AU-WilliamNeural", "display_name": "William", "locale": "en-AU", "gender": "Male"},
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def slugify_filename(name: str, fallback: str = "file") -> str:
    stem = Path(name).stem or fallback
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-") or fallback
    return stem[:120]


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def split_long_unit(unit: str, max_chars: int) -> list[str]:
    if len(unit) <= max_chars:
        return [unit]

    sentence_parts = re.split(r"(?<=[.!?])\s+", unit)
    chunks: list[str] = []
    current = ""

    for sentence in sentence_parts:
        sentence = sentence.strip()
        if not sentence:
            continue

        if len(sentence) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            words = sentence.split()
            word_chunk = ""
            for word in words:
                candidate = f"{word_chunk} {word}".strip()
                if len(candidate) <= max_chars:
                    word_chunk = candidate
                else:
                    if word_chunk:
                        chunks.append(word_chunk.strip())
                    word_chunk = word
            if word_chunk:
                chunks.append(word_chunk.strip())
            continue

        candidate = f"{current} {sentence}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            current = sentence

    if current:
        chunks.append(current.strip())
    return chunks


def split_text(text: str, max_chars: int = MAX_CHARS_PER_CHUNK) -> list[str]:
    text = normalize_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > max_chars:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.extend(split_long_unit(paragraph, max_chars))
            continue

        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            current = paragraph

    if current:
        chunks.append(current.strip())
    return chunks


def job_dir(job_id: str) -> Path:
    safe = re.sub(r"[^a-f0-9]", "", job_id.lower())
    if not safe:
        raise HTTPException(status_code=404, detail="Invalid job id")
    return JOBS_DIR / safe


def manifest_path(job_id: str) -> Path:
    return job_dir(job_id) / "manifest.json"


def read_manifest(job_id: str) -> dict[str, Any]:
    path = manifest_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(manifest: dict[str, Any]) -> None:
    path = manifest_path(manifest["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def update_manifest(job_id: str, **updates: Any) -> dict[str, Any]:
    manifest = read_manifest(job_id)
    manifest.update(updates)
    manifest["updated_at"] = now_iso()
    write_manifest(manifest)
    return manifest


def cache_key_for(text: str, options: dict[str, Any]) -> str:
    payload = {
        "provider": "edge-tts",
        "voice": options["voice"],
        "rate": options["rate"],
        "volume": options["volume"],
        "pitch": options["pitch"],
        "generate_subtitles": bool(options.get("generate_subtitles", True)),
        "text": text,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def ffconcat_escape(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


def run_command(args: list[str], description: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{description} failed because a required executable was not found: {args[0]}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or str(exc)
        raise RuntimeError(f"{description} failed: {detail}") from exc


def ffprobe_duration_ms(path: Path) -> int:
    result = run_command(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        "Reading audio duration",
    )
    value = result.stdout.strip()
    try:
        return max(0, int(float(value) * 1000))
    except ValueError as exc:
        raise RuntimeError(f"Could not parse audio duration for {path.name}: {value}") from exc


def make_silence_mp3(path: Path, silence_ms: int) -> None:
    seconds = max(0.0, silence_ms / 1000.0)
    if seconds <= 0:
        return
    run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=24000:cl=mono",
            "-t",
            f"{seconds:.3f}",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "6",
            str(path),
        ],
        "Creating silence audio",
    )


def concat_audio(input_files: list[Path], output_path: Path, work_dir: Path, silence_ms: int = 0) -> None:
    if not input_files:
        raise RuntimeError("No audio files were supplied for concatenation")

    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    concat_list: list[Path] = []
    silence_path = work_dir / f"silence_{silence_ms}ms.mp3"
    if silence_ms > 0 and len(input_files) > 1:
        if not silence_path.exists():
            make_silence_mp3(silence_path, silence_ms)
        for index, file_path in enumerate(input_files):
            concat_list.append(file_path)
            if index < len(input_files) - 1:
                concat_list.append(silence_path)
    else:
        concat_list = input_files[:]

    list_path = work_dir / f"concat_{int(time.time() * 1000)}.txt"
    list_path.write_text(
        "\n".join(f"file '{ffconcat_escape(path)}'" for path in concat_list) + "\n",
        encoding="utf-8",
    )

    run_command(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-vn",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "3",
            str(output_path),
        ],
        "Combining audio files",
    )


def parse_srt_timestamp(value: str) -> int:
    match = re.match(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", value.strip())
    if not match:
        raise ValueError(f"Invalid SRT timestamp: {value}")
    hours, minutes, seconds, millis = [int(item) for item in match.groups()]
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def format_srt_timestamp(ms: int) -> str:
    ms = max(0, int(ms))
    millis = ms % 1000
    total_seconds = ms // 1000
    seconds = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02}:{minutes:02}:{seconds:02},{millis:03}"


def extract_srt_cues(srt_text: str) -> list[tuple[int, int, str]]:
    blocks = re.split(r"\n\s*\n", srt_text.strip())
    cues: list[tuple[int, int, str]] = []
    timing_re = re.compile(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})")

    for block in blocks:
        lines = [line.rstrip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        timing_index = next((idx for idx, line in enumerate(lines) if timing_re.search(line)), None)
        if timing_index is None:
            continue
        match = timing_re.search(lines[timing_index])
        if not match:
            continue
        text = "\n".join(lines[timing_index + 1 :]).strip()
        if not text:
            continue
        cues.append((parse_srt_timestamp(match.group(1)), parse_srt_timestamp(match.group(2)), text))
    return cues


def concat_srts(srt_files: list[Path], durations_ms: list[int], output_path: Path, silence_ms: int = 0) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cue_index = 1
    offset = 0
    lines: list[str] = []

    for index, srt_file in enumerate(srt_files):
        if srt_file.exists():
            for start_ms, end_ms, cue_text in extract_srt_cues(srt_file.read_text(encoding="utf-8")):
                lines.append(str(cue_index))
                lines.append(f"{format_srt_timestamp(start_ms + offset)} --> {format_srt_timestamp(end_ms + offset)}")
                lines.append(cue_text)
                lines.append("")
                cue_index += 1
        offset += durations_ms[index]
        if index < len(srt_files) - 1:
            offset += max(0, silence_ms)

    output_path.write_text("\n".join(lines), encoding="utf-8")


async def synthesize_chunk_with_edge_tts(text: str, mp3_path: Path, srt_path: Path, options: dict[str, Any]) -> None:
    import edge_tts

    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.parent.mkdir(parents=True, exist_ok=True)

    communicate = edge_tts.Communicate(
        text,
        options["voice"],
        rate=options["rate"],
        volume=options["volume"],
        pitch=options["pitch"],
    )
    submaker = edge_tts.SubMaker()

    with mp3_path.open("wb") as audio_file:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_file.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                submaker.feed(chunk)

    if mp3_path.stat().st_size < 100:
        raise RuntimeError("No audio was received from edge-tts")

    if options.get("generate_subtitles", True):
        srt_path.write_text(submaker.get_srt(), encoding="utf-8")
    else:
        srt_path.write_text("", encoding="utf-8")


async def synthesize_chunk_cached(text: str, mp3_path: Path, srt_path: Path, options: dict[str, Any]) -> None:
    key = cache_key_for(text, options)
    cache_mp3 = CACHE_DIR / f"{key}.mp3"
    cache_srt = CACHE_DIR / f"{key}.srt"

    mp3_path.parent.mkdir(parents=True, exist_ok=True)
    srt_path.parent.mkdir(parents=True, exist_ok=True)

    if cache_mp3.exists() and cache_srt.exists():
        shutil.copy2(cache_mp3, mp3_path)
        shutil.copy2(cache_srt, srt_path)
        return

    tmp_mp3 = mp3_path.with_suffix(".tmp.mp3")
    tmp_srt = srt_path.with_suffix(".tmp.srt")
    await synthesize_chunk_with_edge_tts(text, tmp_mp3, tmp_srt, options)

    tmp_mp3.replace(mp3_path)
    tmp_srt.replace(srt_path)
    shutil.copy2(mp3_path, cache_mp3)
    shutil.copy2(srt_path, cache_srt)


async def synthesize_file_segment(
    input_record: dict[str, Any],
    job_id: str,
    options: dict[str, Any],
) -> dict[str, Any]:
    base_dir = job_dir(job_id)
    chunks_dir = base_dir / "chunks"
    segments_dir = base_dir / "segments"
    subtitles_dir = base_dir / "subtitles"
    work_dir = base_dir / "work"

    source_path = base_dir / input_record["input_path"]
    text = source_path.read_text(encoding="utf-8")
    chunks = split_text(text, MAX_CHARS_PER_CHUNK)
    if not chunks:
        raise RuntimeError(f"{input_record['original_name']} is empty after normalization")

    slug = input_record["slug"]
    chunk_mp3s: list[Path] = []
    chunk_srts: list[Path] = []
    chunk_durations: list[int] = []

    for chunk_index, chunk in enumerate(chunks, start=1):
        chunk_slug = f"{slug}.part{chunk_index:03}"
        chunk_mp3 = chunks_dir / f"{chunk_slug}.mp3"
        chunk_srt = chunks_dir / f"{chunk_slug}.srt"
        await synthesize_chunk_cached(chunk, chunk_mp3, chunk_srt, options)
        chunk_mp3s.append(chunk_mp3)
        chunk_srts.append(chunk_srt)
        chunk_durations.append(ffprobe_duration_ms(chunk_mp3))

    segment_mp3 = segments_dir / f"{slug}.mp3"
    segment_srt = subtitles_dir / f"{slug}.srt"

    if len(chunk_mp3s) == 1:
        segments_dir.mkdir(parents=True, exist_ok=True)
        subtitles_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(chunk_mp3s[0], segment_mp3)
        shutil.copy2(chunk_srts[0], segment_srt)
    else:
        await asyncio.to_thread(concat_audio, chunk_mp3s, segment_mp3, work_dir / f"{slug}_chunks", 0)
        await asyncio.to_thread(concat_srts, chunk_srts, chunk_durations, segment_srt, 0)

    duration_ms = ffprobe_duration_ms(segment_mp3)
    return {
        **input_record,
        "status": "done",
        "chunk_count": len(chunks),
        "segment_path": str(segment_mp3.relative_to(base_dir)),
        "subtitle_path": str(segment_srt.relative_to(base_dir)),
        "duration_ms": duration_ms,
        "duration_seconds": round(duration_ms / 1000, 3),
    }


def create_outputs_zip(job_id: str, output_name: str, include_combined: bool = True) -> str:
    base_dir = job_dir(job_id)
    outputs_dir = base_dir / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    zip_path = outputs_dir / output_name

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative_dir in ("segments", "subtitles"):
            directory = base_dir / relative_dir
            if directory.exists():
                for file_path in sorted(directory.glob("*"), key=lambda path: natural_key(path.name)):
                    if file_path.is_file():
                        archive.write(file_path, file_path.relative_to(base_dir))

        if include_combined:
            combined_dir = base_dir / "combined"
            if combined_dir.exists():
                for file_path in sorted(combined_dir.glob("*"), key=lambda path: natural_key(path.name)):
                    if file_path.is_file():
                        archive.write(file_path, file_path.relative_to(base_dir))

        manifest = manifest_path(job_id)
        if manifest.exists():
            archive.write(manifest, "manifest.json")

    return str(zip_path.relative_to(base_dir))


def combine_job_segments(job_id: str, manifest: dict[str, Any]) -> dict[str, str]:
    base_dir = job_dir(job_id)
    combined_dir = base_dir / "combined"
    combined_dir.mkdir(parents=True, exist_ok=True)

    segment_paths = [base_dir / item["segment_path"] for item in manifest["inputs"] if item.get("segment_path")]
    srt_paths = [base_dir / item["subtitle_path"] for item in manifest["inputs"] if item.get("subtitle_path")]
    durations = [int(item.get("duration_ms", 0)) for item in manifest["inputs"] if item.get("segment_path")]

    silence_ms = int(manifest["options"].get("silence_ms", 1000))
    combined_mp3 = combined_dir / "story_combined.mp3"
    combined_srt = combined_dir / "story_combined.srt"

    concat_audio(segment_paths, combined_mp3, base_dir / "work" / "combined", silence_ms)
    concat_srts(srt_paths, durations, combined_srt, silence_ms)

    return {
        "combined_mp3": str(combined_mp3.relative_to(base_dir)),
        "combined_srt": str(combined_srt.relative_to(base_dir)),
    }


async def process_job(job_id: str) -> None:
    async with _job_semaphore:
        try:
            manifest = update_manifest(
                job_id,
                status="running",
                stage="Starting narration job",
                progress=1,
                errors=[],
            )

            input_count = len(manifest["inputs"])
            updated_inputs: list[dict[str, Any]] = []

            for index, input_record in enumerate(manifest["inputs"], start=1):
                stage = f"Generating {index}/{input_count}: {input_record['original_name']}"
                update_manifest(
                    job_id,
                    stage=stage,
                    progress=max(1, int(((index - 1) / max(input_count, 1)) * 75)),
                )
                completed = await synthesize_file_segment(input_record, job_id, manifest["options"])
                updated_inputs.append(completed)
                manifest = update_manifest(
                    job_id,
                    inputs=updated_inputs + manifest["inputs"][index:],
                    stage=stage,
                    progress=max(5, int((index / max(input_count, 1)) * 75)),
                )

            outputs: dict[str, str] = {}
            mode = manifest["options"].get("output_mode", "both")

            if mode in ("separate", "both"):
                update_manifest(job_id, stage="Creating ZIP of separate files", progress=82)
                outputs["separate_zip"] = create_outputs_zip(job_id, "separate_voice_files.zip", include_combined=False)

            if mode in ("combined", "both"):
                update_manifest(job_id, stage="Combining generated audio", progress=88, outputs=outputs)
                manifest = read_manifest(job_id)
                outputs.update(await asyncio.to_thread(combine_job_segments, job_id, manifest))

            update_manifest(job_id, stage="Creating ZIP of all outputs", progress=94, outputs=outputs)
            outputs["all_outputs_zip"] = create_outputs_zip(job_id, "all_outputs.zip", include_combined=True)
            outputs["manifest"] = "manifest.json"

            update_manifest(
                job_id,
                status="done",
                stage="Done",
                progress=100,
                outputs=outputs,
                completed_at=now_iso(),
            )
        except Exception as exc:  # noqa: BLE001 - surface background job error in manifest
            current = read_manifest(job_id)
            errors = current.get("errors", [])
            errors.append(str(exc))
            update_manifest(
                job_id,
                status="failed",
                stage="Failed",
                progress=current.get("progress", 0),
                errors=errors,
                completed_at=now_iso(),
            )


async def collect_uploaded_texts(files: list[UploadFile]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []

    if not files:
        raise HTTPException(status_code=400, detail="Upload at least one .txt file or a .zip containing .txt files")

    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail=f"Too many uploaded files. Limit: {MAX_UPLOAD_FILES}")

    for upload in files:
        filename = Path(upload.filename or "uploaded.txt").name
        data = await upload.read()
        if not data:
            continue

        if filename.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    txt_names = [name for name in archive.namelist() if name.lower().endswith(".txt") and not name.endswith("/")]
                    for name in sorted(txt_names, key=natural_key):
                        member_name = Path(name).name
                        member_data = archive.read(name)
                        text = normalize_text(decode_text(member_data))
                        if text:
                            records.append({"original_name": member_name, "text": text})
            except zipfile.BadZipFile as exc:
                raise HTTPException(status_code=400, detail=f"Invalid zip file: {filename}") from exc
        elif filename.lower().endswith(".txt"):
            text = normalize_text(decode_text(data))
            if text:
                records.append({"original_name": filename, "text": text})
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {filename}. Use .txt or .zip")

    if not records:
        raise HTTPException(status_code=400, detail="No non-empty .txt files were found")

    records = sorted(records, key=lambda item: natural_key(item["original_name"]))
    total_chars = sum(len(item["text"]) for item in records)
    if total_chars > MAX_TOTAL_CHARS:
        raise HTTPException(status_code=400, detail=f"Total text is too large. Limit: {MAX_TOTAL_CHARS:,} characters")

    return records


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "data_dir": str(DATA_DIR), "max_concurrent_jobs": MAX_CONCURRENT_JOBS}


@app.get("/api/voices")
async def voices() -> dict[str, Any]:
    try:
        import edge_tts

        raw_voices = await edge_tts.list_voices()
        voices_list = [
            {
                "short_name": voice.get("ShortName"),
                "display_name": voice.get("FriendlyName") or voice.get("LocalName") or voice.get("ShortName"),
                "locale": voice.get("Locale"),
                "gender": voice.get("Gender"),
            }
            for voice in raw_voices
            if voice.get("ShortName")
        ]
        voices_list = sorted(voices_list, key=lambda item: (item.get("locale") or "", item.get("short_name") or ""))
        return {"source": "edge-tts", "voices": voices_list}
    except Exception as exc:  # noqa: BLE001 - fallback keeps UI usable offline
        return {"source": "fallback", "error": str(exc), "voices": FALLBACK_VOICES}


@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    output_mode: str = Form("both"),
    voice: str = Form(DEFAULT_VOICE),
    rate: str = Form("+0%"),
    volume: str = Form("+0%"),
    pitch: str = Form("+0Hz"),
    silence_ms: int = Form(1000),
    generate_subtitles: bool = Form(True),
) -> JSONResponse:
    if output_mode not in {"separate", "combined", "both"}:
        raise HTTPException(status_code=400, detail="output_mode must be separate, combined, or both")

    text_records = await collect_uploaded_texts(files)

    job_id = uuid.uuid4().hex
    base_dir = job_dir(job_id)
    input_dir = base_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)

    input_manifest: list[dict[str, Any]] = []
    for index, record in enumerate(text_records, start=1):
        slug = f"{index:03}_{slugify_filename(record['original_name'], f'file_{index:03}')}"
        input_path = input_dir / f"{slug}.txt"
        input_path.write_text(record["text"], encoding="utf-8")
        input_manifest.append(
            {
                "index": index,
                "original_name": record["original_name"],
                "slug": slug,
                "input_path": str(input_path.relative_to(base_dir)),
                "char_count": len(record["text"]),
                "status": "queued",
            }
        )

    manifest = {
        "id": job_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "status": "queued",
        "stage": "Queued",
        "progress": 0,
        "options": {
            "provider": "edge-tts",
            "output_mode": output_mode,
            "voice": voice,
            "rate": rate,
            "volume": volume,
            "pitch": pitch,
            "silence_ms": max(0, min(int(silence_ms), 10000)),
            "generate_subtitles": bool(generate_subtitles),
            "max_chars_per_chunk": MAX_CHARS_PER_CHUNK,
        },
        "inputs": input_manifest,
        "outputs": {},
        "errors": [],
        "stats": {
            "file_count": len(input_manifest),
            "total_chars": sum(item["char_count"] for item in input_manifest),
        },
    }
    write_manifest(manifest)

    background_tasks.add_task(process_job, job_id)

    return JSONResponse({"id": job_id, "status_url": f"/api/jobs/{job_id}"})


@app.get("/api/jobs")
async def list_jobs(limit: int = 20) -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    for path in sorted(JOBS_DIR.glob("*/manifest.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            manifests.append(
                {
                    "id": data.get("id"),
                    "created_at": data.get("created_at"),
                    "status": data.get("status"),
                    "progress": data.get("progress"),
                    "stage": data.get("stage"),
                    "file_count": data.get("stats", {}).get("file_count"),
                }
            )
            if len(manifests) >= limit:
                break
        except Exception:
            continue
    return {"jobs": manifests}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    manifest = read_manifest(job_id)
    links: dict[str, str] = {}
    for key, relative_path in manifest.get("outputs", {}).items():
        links[key] = f"/api/jobs/{job_id}/files/{relative_path}"
    manifest["links"] = links
    return manifest


@app.get("/api/jobs/{job_id}/files/{relative_path:path}")
async def download_file(job_id: str, relative_path: str) -> FileResponse:
    base_dir = job_dir(job_id).resolve()
    target = (base_dir / relative_path).resolve()
    try:
        target.relative_to(base_dir)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc

    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    return FileResponse(target, filename=target.name)
