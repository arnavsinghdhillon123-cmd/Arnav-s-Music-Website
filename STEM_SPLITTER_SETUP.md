# Stem Splitter Setup

This DAW now includes a `Split to Stems` action for audio clips.

## What it does

- Right-click an audio region in the timeline.
- Choose `Split to Stems`.
- The app uploads that region to the local backend.
- The backend runs AudioShake if `AUDIOSHAKE_API_KEY` is configured.
- Otherwise it falls back to a local engine like Demucs.
- The returned stems are added as new audio tracks:
  - `Vocals`
  - `Drums`
  - `Bass`
  - `Other`

## Fastest hosted option: AudioShake

If you want much faster and more predictable stem splitting, set an AudioShake API key:

```bash
export AUDIOSHAKE_API_KEY=your_api_key_here
```

With that present, `/api/stem-health` will report `engine: "audioshake"` and the DAW will use the hosted API instead of local Demucs.

AudioShake Tasks are asynchronous and can return stems like:

- `Vocals`
- `Drums`
- `Bass`
- `Keys`
- `Other`

## Install the local stem backend

Create the dedicated stem environment and install the dependencies:

```bash
/opt/homebrew/bin/python3.10 -m venv .venv-stems
.venv-stems/bin/python -m pip install -r requirements-stems.txt
```

`ffmpeg` must also be available on your system PATH.

## Run the app

Serve both the webpage and the stem API from the same process:

```bash
python3 server.py
```

Then open:

```text
http://127.0.0.1:8000
```

## Using a hosted backend (Netlify frontend)

If your frontend is deployed on Netlify (or any static host), point it to your hosted stem API:

- One-time URL override:

```text
https://your-site.netlify.app/?stemApi=https://your-stem-backend.example.com
```

- Or set it globally before the main script (advanced):

```html
<script>
  window.STEM_API_BASE = "https://your-stem-backend.example.com";
</script>
```

The app stores the last working API base in `localStorage`, so you usually only need to set this once.

## Simple deploy steps (Netlify + Render)

Use this when you want a public site:

1. Push this project to GitHub.
2. Deploy backend on Render:
   - In Render, create a new service from your GitHub repo.
   - Render will detect `render.yaml`.
   - Add `AUDIOSHAKE_API_KEY` in the Render environment if you want the hosted splitter.
   - Wait for deploy to finish.
   - Open `https://<your-render-service>.onrender.com/api/stem-health`.
   - Confirm it returns JSON with `"ready": true`.
3. Deploy frontend on Netlify:
   - In Netlify, import the same GitHub repo.
   - Publish directory: repo root (where `index.html` is).
   - Build command: leave empty for static deploy.
4. Connect frontend to backend:
   - Open your Netlify URL with:
   - `https://<your-netlify-site>.netlify.app/?stemApi=https://<your-render-service>.onrender.com`
   - Once it works once, the app remembers that backend URL.
5. Test from Netlify:
   - Right click an audio clip -> `Split to Stems`.
   - If it says backend offline, re-check Render URL and `/api/stem-health`.

## Notes

- Stem splitting is synchronous in this first version, so large files can take a while.
- If `AUDIOSHAKE_API_KEY` is set, AudioShake is preferred over local engines.
- The local Demucs fallback model is set to `htdemucs` to avoid the extra `diffq` dependency required by quantized `mdx_q`.
- Repeat splits of the same clip and model are cached on the backend and should return much faster.
- Generated stem files are stored temporarily in `.stem-jobs/`.
- If no supported stem engine is installed, the DAW will show a setup error when you try to split a clip.
