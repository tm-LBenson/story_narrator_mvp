# StoryCast

StoryCast is a lightweight web app for turning batches of text files into narrated audio.

It is built around a simple production-friendly workflow:

1. Upload one or more `.txt` files, or a `.zip` containing `.txt` files.
2. Each text file becomes one reusable narration segment.
3. StoryCast generates MP3 audio and optional SRT subtitles for each segment.
4. Export separate voice files, one combined story file, or both.

StoryCast uses [`edge-tts`](https://github.com/rany2/edge-tts) as the included text-to-speech provider because it is free to start with, has good voices, and does not require an API key. The container still needs internet access because speech synthesis happens through the online voice service.

## What is included

```text
storycast/
  app/
    main.py              FastAPI app and narration pipeline
    static/
      index.html         Customer-facing web UI
      styles.css         Lightweight responsive styling
      app.js             Browser-side form, menus, polling, downloads
  sample_texts/          Two small test files
  Dockerfile             Python + ffmpeg image
  docker-compose.yml     Localhost deployment
  requirements.txt       Python dependencies
  .env.example           Config examples
  README.md              This file
```

## Features

- Clean browser UI with guided steps.
- Upload multiple `.txt` files at once.
- Upload a `.zip` containing `.txt` files.
- Natural filename sorting, so files like `001_intro.txt`, `002_scene.txt`, and `010_finale.txt` stay in order.
- Generate separate MP3 files per uploaded text file.
- Generate one combined MP3 file.
- Generate SRT subtitles per file and for the combined story.
- Add configurable silence between files in the combined MP3.
- Tune voice speed, pitch, and volume from a lightweight voice menu.
- Cache generated chunks so repeated text with the same voice/speed/pitch/volume does not get regenerated.
- Split long text files into smaller chunks before synthesis.
- Dockerized with ffmpeg included.

## Run with Docker Compose

From this project folder:

```bash
docker compose up --build
```

Open:

```text
http://localhost:8080
```

The compose file binds the app to `127.0.0.1:8080`, so it is local-only by default.

Generated files are stored in:

```text
./data/jobs/
./data/cache/
```

## Test it quickly

After starting the app:

1. Open `http://localhost:8080`.
2. Upload the files in `sample_texts/`.
3. Keep export set to **Both**.
4. Click **Create narration**.
5. Download the combined MP3, separate files ZIP, or all outputs ZIP.

## Local Python development

You need Python 3.12+ and ffmpeg installed.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export DATA_DIR="$PWD/data"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

On Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DATA_DIR="$PWD\data"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8080
```

## Configuration

The app reads these environment variables:

| Variable | Default | Purpose |
|---|---:|---|
| `DATA_DIR` | `/data` | Where jobs, cache, and outputs are stored |
| `DEFAULT_VOICE` | `en-US-AriaNeural` | Default voice shown in the UI |
| `MAX_UPLOAD_FILES` | `100` | Max uploaded files per job |
| `MAX_TOTAL_CHARS` | `1000000` | Max total characters per job |
| `MAX_CHARS_PER_CHUNK` | `4500` | Long text files are split into chunks around this size |
| `MAX_CONCURRENT_JOBS` | `1` | Number of narration jobs allowed to run at once |

## Output layout

A completed job looks like this:

```text
data/jobs/<job_id>/
  input/
    001_intro.txt
    002_chapter_one.txt
  chunks/
    001_intro.part001.mp3
    001_intro.part001.srt
  segments/
    001_intro.mp3
    002_chapter_one.mp3
  subtitles/
    001_intro.srt
    002_chapter_one.srt
  combined/
    story_combined.mp3
    story_combined.srt
  outputs/
    separate_voice_files.zip
    all_outputs.zip
  manifest.json
```

## How caching works

The app hashes this data for each text chunk:

```text
provider + voice + rate + volume + pitch + text
```

If the same chunk is generated again with the same settings, StoryCast reuses the cached MP3/SRT instead of calling the TTS provider again.

## Hosting notes

For private hosting, put StoryCast behind normal web-app protections before sharing it outside your own network:

- login/authentication,
- request rate limiting,
- disk cleanup for old jobs,
- stricter upload size limits,
- HTTPS through a reverse proxy,
- monitoring/logging,
- and a clear rights/commercial-use decision for generated audio.

`edge-tts` is useful for localhost tools and private use. For a public or commercial app, consider swapping the provider layer to an official TTS API such as Azure Speech, Google Cloud Text-to-Speech, Amazon Polly, or another provider whose terms clearly fit your use case.

## Troubleshooting

### The app says ffmpeg or ffprobe is missing

Use Docker, or install ffmpeg locally.

### The voice list does not load

The UI falls back to a small built-in voice list. Full voice loading requires the container/server to reach the online voice service.

### A job fails with “No audio was received”

This usually means the remote TTS service did not return audio for that request. Try a different voice, shorter text, a slower speed, or rerun the job.

### Combined output is not created

Combined output uses ffmpeg. Check that ffmpeg is installed and that the per-file MP3 files were generated successfully.

## Development notes

The TTS logic is isolated around `synthesize_chunk_with_edge_tts()` and `synthesize_chunk_cached()` in `app/main.py`. To add another provider later, replace or wrap those functions and include the provider name in the cache key.
