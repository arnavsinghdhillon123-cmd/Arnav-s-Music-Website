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
from urllib import error as urlerror
from urllib import request as urlrequest


ROOT = Path(__file__).resolve().parent
STEM_JOBS_DIR = ROOT / ".stem-jobs"
STEM_JOBS_DIR.mkdir(exist_ok=True)
STEM_CACHE_DIR = STEM_JOBS_DIR / "cache"
STEM_CACHE_DIR.mkdir(exist_ok=True)
CLIP_PROCESS_CACHE_DIR = STEM_JOBS_DIR / "clip-cache"
CLIP_PROCESS_CACHE_DIR.mkdir(exist_ok=True)
DEFAULT_MODEL = "demucs:htdemucs"
AUDIOSHAKE_DEFAULT_MODEL = "audioshake:music-5stem"
AUDIOSHAKE_TARGETS = [
    ("vocals", "Vocals"),
    ("drums", "Drums"),
    ("bass", "Bass"),
    ("piano", "Keys"),
    ("other", "Other"),
]
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
    return "cpu"


def demucs_model(requested_model):
    configured = os.environ.get("DEMUCS_MODEL")
    if configured:
        return configured
    if requested_model.startswith("demucs:"):
        return requested_model.split(":", 1)[1]
    return "htdemucs"


def audioshake_api_key():
    return (os.environ.get("AUDIOSHAKE_API_KEY") or "").strip()


def audioshake_base_url():
    return (os.environ.get("AUDIOSHAKE_BASE_URL") or "https://api.audioshake.ai").rstrip("/")


def audioshake_configured():
    return bool(audioshake_api_key())


def default_stem_model_for_engine(engine):
    if engine == "audioshake":
        return AUDIOSHAKE_DEFAULT_MODEL
    if engine == "demucs":
        return f"demucs:{demucs_model(DEFAULT_MODEL)}"
    if engine == "spleeter":
        return "spleeter:5stems"
    return DEFAULT_MODEL


def available_stem_engine():
    if audioshake_configured():
        return "audioshake"
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


def clip_process_cache_key(file_hash, tempo_ratio, pitch_semitones, reverse):
    key = f"{file_hash}_tempo-{tempo_ratio:.6f}_pitch-{pitch_semitones:.6f}_reverse-{int(bool(reverse))}"
    return CLIP_PROCESS_CACHE_DIR / key


def ensure_parent(path):
    path.parent.mkdir(parents=True, exist_ok=True)


def ffmpeg_supports_rubberband():
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        return False
    try:
        completed = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-h", "filter=rubberband"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return completed.returncode == 0 and "rubberband" in (completed.stdout or completed.stderr or "")


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


def http_json_request(method, url, headers=None, body=None, timeout=120):
    request = urlrequest.Request(url, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    data = None
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = body
        else:
            data = json.dumps(body).encode("utf-8")
            if not any(str(key).lower() == "content-type" for key in (headers or {})):
                request.add_header("Content-Type", "application/json")
    try:
        with urlrequest.urlopen(request, data=data, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
            return response.getcode(), json.loads(payload) if payload else {}
    except urlerror.HTTPError as error:
        payload = error.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(payload) if payload else {}
        except json.JSONDecodeError:
            parsed = {"message": payload}
        return error.code, parsed


def encode_multipart_formdata(fields, files):
    boundary = f"----WaveforgeBoundary{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for file_item in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_item["field"]}"; '
                f'filename="{file_item["filename"]}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f'Content-Type: {file_item.get("content_type") or "application/octet-stream"}\r\n\r\n'.encode("utf-8"))
        body.extend(file_item["data"])
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    return f"multipart/form-data; boundary={boundary}", bytes(body)


def audioshake_upload_asset(input_path):
    content_type = mimetypes.guess_type(str(input_path))[0] or "application/octet-stream"
    multipart_type, body = encode_multipart_formdata(
        {},
        [
            {
                "field": "file",
                "filename": input_path.name,
                "content_type": content_type,
                "data": input_path.read_bytes(),
            }
        ],
    )
    status, payload = http_json_request(
        "POST",
        f"{audioshake_base_url()}/assets",
        headers={
            "x-api-key": audioshake_api_key(),
            "Content-Type": multipart_type,
        },
        body=body,
        timeout=300,
    )
    if status >= 400 or not payload.get("id"):
        raise RuntimeError(payload.get("message") or payload.get("error") or "AudioShake asset upload failed.")
    return payload["id"]


def audioshake_create_task(asset_id):
    targets = [{"model": model_key, "formats": ["wav"]} for model_key, _label in AUDIOSHAKE_TARGETS]
    status, payload = http_json_request(
        "POST",
        f"{audioshake_base_url()}/tasks",
        headers={
            "x-api-key": audioshake_api_key(),
            "Content-Type": "application/json",
        },
        body={
            "assetId": asset_id,
            "targets": targets,
        },
        timeout=120,
    )
    if status >= 400 or not payload.get("id"):
        raise RuntimeError(payload.get("message") or payload.get("error") or "AudioShake task creation failed.")
    return payload


def audioshake_poll_task(task_id, timeout_seconds=30 * 60, poll_interval=5):
    deadline = time.time() + timeout_seconds
    last_payload = None
    while time.time() < deadline:
        status, payload = http_json_request(
            "GET",
            f"{audioshake_base_url()}/tasks/{task_id}",
            headers={"x-api-key": audioshake_api_key()},
            timeout=120,
        )
        if status >= 400:
            raise RuntimeError(payload.get("message") or payload.get("error") or "AudioShake task polling failed.")
        last_payload = payload
        targets = payload.get("targets") or []
        if targets and all(str(target.get("status", "")).lower() in {"completed", "error"} for target in targets):
            return payload
        time.sleep(poll_interval)
    raise TimeoutError("AudioShake stem splitting timed out.")


def audioshake_download_stems(task_payload, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    stem_files = []
    for target in task_payload.get("targets") or []:
        model_key = str(target.get("model") or "").strip().lower()
        if not model_key:
            continue
        status = str(target.get("status") or "").lower()
        if status == "error":
            error_info = target.get("error") or {}
            raise RuntimeError(error_info.get("message") or f"AudioShake target {model_key} failed.")
        if status != "completed":
            continue
        outputs = target.get("output") or []
        wav_output = next((item for item in outputs if str(item.get("format") or "").lower() == "wav"), outputs[0] if outputs else None)
        if not wav_output or not wav_output.get("link"):
            continue
        destination = output_dir / f"{model_key}.wav"
        request = urlrequest.Request(str(wav_output["link"]), method="GET")
        with urlrequest.urlopen(request, timeout=300) as response, destination.open("wb") as handle:
            shutil.copyfileobj(response, handle)
        stem_files.append(destination)
    return stem_files


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
                    "defaultModel": default_stem_model_for_engine(engine) if engine else DEFAULT_MODEL,
                    "python": stem_runtime_python(),
                    "ffmpeg": shutil.which("ffmpeg"),
                    "clipProcessorReady": bool(shutil.which("ffmpeg") and ffmpeg_supports_rubberband()),
                }
            )
            return
        if parsed.path.startswith("/api/stems/"):
            self.serve_stem_file(parsed.path)
            return
        if parsed.path.startswith("/api/processed/"):
            self.serve_processed_file(parsed.path)
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/split-stems":
            self.handle_split_stems()
            return
        if parsed.path == "/api/process-audio-clip":
            self.handle_process_audio_clip()
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

    def serve_processed_file(self, path):
        relative = Path(unquote(path[len("/api/processed/"):]))
        target = (STEM_JOBS_DIR / relative).resolve()
        if STEM_JOBS_DIR.resolve() not in target.parents or not target.is_file():
            self.send_json({"error": "Processed audio file not found."}, status=404)
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

        if engine == "audioshake":
            model = AUDIOSHAKE_DEFAULT_MODEL
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
            try:
                asset_id = audioshake_upload_asset(input_path)
                task_payload = audioshake_create_task(asset_id)
                task_payload = audioshake_poll_task(task_payload["id"])
                stem_files = audioshake_download_stems(task_payload, output_dir)
            except TimeoutError:
                self.send_json({"error": "AudioShake stem splitting timed out."}, status=504)
                return
            except Exception as error:
                self.send_json({"error": "AudioShake failed to split this file.", "details": str(error)}, status=500)
                return
            if not stem_files:
                self.send_json({"error": "AudioShake finished but no stem files were produced."}, status=500)
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
            return

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

    def handle_process_audio_clip(self):
        if not shutil.which("ffmpeg"):
            self.send_json({"error": "ffmpeg is required for clip processing but was not found on PATH."}, status=503)
            return
        if not ffmpeg_supports_rubberband():
            self.send_json({"error": "ffmpeg rubberband support is not available on this server."}, status=503)
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

        try:
            tempo_ratio = float(form.getfirst("tempoRatio", "1"))
        except (TypeError, ValueError):
            tempo_ratio = 1.0
        tempo_ratio = max(0.25, min(8.0, tempo_ratio))

        try:
            pitch_semitones = float(form.getfirst("pitchSemitones", "0"))
        except (TypeError, ValueError):
            pitch_semitones = 0.0
        pitch_semitones = max(-24.0, min(24.0, pitch_semitones))

        reverse = str(form.getfirst("reverse", "0")).strip().lower() in {"1", "true", "yes", "on"}

        cleanup_old_jobs()
        job_id = uuid.uuid4().hex
        job_dir = STEM_JOBS_DIR / job_id
        input_dir = job_dir / "input"
        output_dir = job_dir / "processed"
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
        cache_dir = clip_process_cache_key(file_hash, tempo_ratio, pitch_semitones, reverse)
        cached_output = cache_dir / "processed.wav"
        if cached_output.exists():
            output_path = output_dir / "processed.wav"
            ensure_parent(output_path)
            shutil.copy2(cached_output, output_path)
            relative = output_path.relative_to(STEM_JOBS_DIR).as_posix()
            try:
                input_path.unlink()
            except FileNotFoundError:
                pass
            self.send_json(
                {
                    "jobId": job_id,
                    "cached": True,
                    "url": f"/api/processed/{relative}",
                }
            )
            return

        pitch_ratio = 2 ** (pitch_semitones / 12.0)
        filter_parts = [
            "rubberband="
            f"tempo={tempo_ratio:.10f}:"
            f"pitch={pitch_ratio:.10f}:"
            "transients=smooth:"
            "detector=compound:"
            "phase=laminar:"
            "window=standard:"
            "smoothing=on:"
            "formant=preserved:"
            "pitchq=quality:"
            "channels=together"
        ]
        if reverse:
            filter_parts.append("areverse")
        filtergraph = ",".join(filter_parts)

        output_path = output_dir / "processed.wav"
        command = [
            shutil.which("ffmpeg") or "ffmpeg",
            "-y",
            "-i",
            str(input_path),
            "-vn",
            "-af",
            filtergraph,
            "-acodec",
            "pcm_s16le",
            str(output_path),
        ]

        try:
            subprocess.run(
                command,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=10 * 60,
                check=True,
            )
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or error.stdout or "").strip()
            self.send_json(
                {
                    "error": "Clip processing failed.",
                    "details": stderr[-1200:],
                },
                status=500,
            )
            return
        except subprocess.TimeoutExpired:
            self.send_json({"error": "Clip processing timed out."}, status=504)
            return

        cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(output_path, cached_output)
        relative = output_path.relative_to(STEM_JOBS_DIR).as_posix()
        try:
            input_path.unlink()
        except FileNotFoundError:
            pass

        self.send_json(
            {
                "jobId": job_id,
                "cached": False,
                "url": f"/api/processed/{relative}",
            }
        )


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
