#!/usr/bin/env python3
import cgi
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
STEM_JOBS_DIR = ROOT / ".stem-jobs"
STEM_JOBS_DIR.mkdir(exist_ok=True)
STEM_CACHE_DIR = STEM_JOBS_DIR / "cache"
STEM_CACHE_DIR.mkdir(exist_ok=True)
DEFAULT_MODEL = "demucs:mdx_q"
STEM_LABELS = {
    "vocals": "Vocals",
    "drums": "Drums",
    "bass": "Bass",
    "guitar": "Guitar",
    "piano": "Keys",
    "other": "Other",
}
STEM_ORDER = {name: index for index, name in enumerate(["vocals", "drums", "bass", "guitar", "piano", "other"])}
ALLOWED_AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".aif", ".aiff", ".opus", ".webm"}
DEFAULT_STEM_RUNTIME = ROOT / ".venv-stems" / "bin" / "python"


def stem_runtime_python():
    configured = os.environ.get("STEM_RUNTIME_PYTHON")
    if configured:
        return configured
    if DEFAULT_STEM_RUNTIME.exists():
        return str(DEFAULT_STEM_RUNTIME)
    return sys.executable


def module_available(module_name):
    command = [
        stem_runtime_python(),
        "-c",
        (
            "import importlib.util, sys; "
            f"sys.exit(0 if importlib.util.find_spec({module_name!r}) else 1)"
        ),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return completed.returncode == 0


def torch_mps_available():
    command = [
        stem_runtime_python(),
        "-c",
        (
            "import sys; "
            "try:\n"
            "  import torch\n"
            "  sys.exit(0 if getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available() else 1)\n"
            "except Exception:\n"
            "  sys.exit(1)\n"
        ),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return completed.returncode == 0


def demucs_device():
    configured = os.environ.get("DEMUCS_DEVICE")
    if configured:
        return configured
    return "mps" if torch_mps_available() else "cpu"


def demucs_model(requested_model):
    configured = os.environ.get("DEMUCS_MODEL")
    if configured:
        return configured
    if requested_model.startswith("demucs:"):
        return requested_model.split(":", 1)[1]
    return "mdx_q"


def available_stem_engine():
    if module_available("demucs"):
        return "demucs"
    if module_available("spleeter"):
        return "spleeter"
    return None


def collect_stem_files(output_dir):
    stem_files = []
    for stem_file in output_dir.rglob("*"):
        if not stem_file.is_file():
            continue
        if stem_file.suffix.lower() not in ALLOWED_AUDIO_SUFFIXES:
            continue
        stem_key = stem_file.stem.lower()
        if stem_key not in STEM_LABELS and stem_key not in STEM_ORDER:
            continue
        stem_files.append(stem_file)
    return sorted(stem_files, key=lambda path: STEM_ORDER.get(path.stem.lower(), 999))


def hash_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def cache_key_for(engine, model, file_hash):
    safe_model = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(model))
    return STEM_CACHE_DIR / engine / safe_model / file_hash


def ensure_parent(path):
    path.parent.mkdir(parents=True, exist_ok=True)


def build_stem_payload(job_root, stem_files):
    stems = []
    for stem_file in stem_files:
        stem_key = stem_file.stem.lower()
        relative = stem_file.relative_to(STEM_JOBS_DIR).as_posix()
        stems.append(
            {
                "stem": stem_key,
                "label": STEM_LABELS.get(stem_key, stem_key.title()),
                "url": f"/api/stems/{relative}",
            }
        )
    return stems


def copy_cached_stems(cache_dir, job_output_dir):
    job_output_dir.mkdir(parents=True, exist_ok=True)
    copied_files = []
    for stem_file in collect_stem_files(cache_dir):
        destination = job_output_dir / stem_file.name
        ensure_parent(destination)
        shutil.copy2(stem_file, destination)
        copied_files.append(destination)
    return copied_files


def populate_cache(cache_dir, stem_files):
    cache_dir.mkdir(parents=True, exist_ok=True)
    for stem_file in stem_files:
        destination = cache_dir / stem_file.name
        ensure_parent(destination)
        shutil.copy2(stem_file, destination)


def cleanup_old_jobs(max_age_seconds=24 * 60 * 60):
    cutoff = time.time() - max_age_seconds
    for job_dir in STEM_JOBS_DIR.iterdir():
        try:
            if not job_dir.is_dir():
                continue
            if job_dir == STEM_CACHE_DIR:
                continue
            if job_dir.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(job_dir, ignore_errors=True)
        except FileNotFoundError:
            continue


class StemHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.send_response(204)
            self.send_cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/stem-health":
            engine = available_stem_engine()
            self.send_json(
                {
                    "ready": bool(engine),
                    "engine": engine,
                    "python": stem_runtime_python(),
                    "ffmpeg": shutil.which("ffmpeg"),
                }
            )
            return
        if parsed.path.startswith("/api/stems/"):
            self.serve_stem_file(parsed.path)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/split-stems":
            self.handle_split_stems()
            return
        self.send_json({"error": "Not found."}, status=404)

    def serve_stem_file(self, path):
        relative = Path(unquote(path[len("/api/stems/"):]))
        target = (STEM_JOBS_DIR / relative).resolve()
        if STEM_JOBS_DIR.resolve() not in target.parents or not target.is_file():
            self.send_json({"error": "Stem file not found."}, status=404)
            return

        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        self.send_response(200)
        self.send_cors_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with target.open("rb") as source:
            shutil.copyfileobj(source, self.wfile)

    def handle_split_stems(self):
        engine = available_stem_engine()
        if not engine:
            self.send_json(
                {
                    "error": "No supported stem engine is installed. Install Demucs or Spleeter, then restart this server."
                },
                status=503,
            )
            return
        if not shutil.which("ffmpeg"):
            self.send_json({"error": "ffmpeg is required for stem splitting but was not found on PATH."}, status=503)
            return

        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self.send_json({"error": "Expected multipart form upload."}, status=400)
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": content_type,
            },
        )
        upload = form["audio"] if "audio" in form else None
        if upload is None or not getattr(upload, "file", None):
            self.send_json({"error": "Missing uploaded audio file."}, status=400)
            return

        requested_model = form.getfirst("model", DEFAULT_MODEL)

        cleanup_old_jobs()
        job_id = uuid.uuid4().hex
        job_dir = STEM_JOBS_DIR / job_id
        input_dir = job_dir / "input"
        output_dir = job_dir / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        original_name = Path(upload.filename or "audio.wav")
        suffix = original_name.suffix.lower()
        if suffix not in ALLOWED_AUDIO_SUFFIXES:
            suffix = ".wav"
        input_path = input_dir / f"source{suffix}"
        with input_path.open("wb") as destination:
            shutil.copyfileobj(upload.file, destination)

        file_hash = hash_file(input_path)

        if engine == "demucs":
            model = demucs_model(requested_model)
            device = demucs_device()
            cache_dir = cache_key_for(engine, model, file_hash)
            if cache_dir.exists():
                cached_files = copy_cached_stems(cache_dir, output_dir)
                stems = build_stem_payload(job_dir, cached_files)
                if stems:
                    try:
                        input_path.unlink()
                    except FileNotFoundError:
                        pass
                    self.send_json({"jobId": job_id, "engine": engine, "model": model, "cached": True, "stems": stems})
                    return
            command = [
                stem_runtime_python(),
                "-m",
                "demucs.separate",
                "-n",
                model,
                "-d",
                device,
                "-o",
                str(output_dir),
                str(input_path),
            ]
        else:
            model = requested_model if requested_model.startswith("spleeter:") else "spleeter:5stems"
            if model not in {"spleeter:4stems", "spleeter:5stems"}:
                self.send_json({"error": "Unsupported Spleeter model requested."}, status=400)
                return
            cache_dir = cache_key_for(engine, model, file_hash)
            if cache_dir.exists():
                cached_files = copy_cached_stems(cache_dir, output_dir)
                stems = build_stem_payload(job_dir, cached_files)
                if stems:
                    try:
                        input_path.unlink()
                    except FileNotFoundError:
                        pass
                    self.send_json({"jobId": job_id, "engine": engine, "model": model, "cached": True, "stems": stems})
                    return
            command = [
                stem_runtime_python(),
                "-m",
                "spleeter",
                "separate",
                "-p",
                model,
                "-o",
                str(output_dir),
                str(input_path),
            ]

        try:
            completed = subprocess.run(
                command,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=30 * 60,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or error.stdout or "").strip()
            self.send_json(
                {
                    "error": f"{engine.title()} failed to split this file.",
                    "details": stderr[-1200:],
                },
                status=500,
            )
            return
        except subprocess.TimeoutExpired:
            self.send_json({"error": "Stem splitting timed out."}, status=504)
            return

        stem_files = collect_stem_files(output_dir)
        if not stem_files:
            self.send_json(
                {
                    "error": f"{engine.title()} finished but no stem files were produced.",
                    "details": (completed.stderr or completed.stdout or "").strip()[-1200:],
                },
                status=500,
            )
            return

        populate_cache(cache_dir, stem_files)
        stems = build_stem_payload(job_dir, stem_files)

        if not stems:
            self.send_json({"error": "No supported audio stems were produced."}, status=500)
            return

        try:
            input_path.unlink()
        except FileNotFoundError:
            pass

        self.send_json({"jobId": job_id, "engine": engine, "model": model, "stems": stems})


def main():
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), StemHandler)
    print(f"WaveForge server running at http://{host}:{port}")
    print(f"Stem runtime python: {stem_runtime_python()}")
    print("Static app and /api/split-stems are served from this process.")
    server.serve_forever()


if __name__ == "__main__":
    main()
