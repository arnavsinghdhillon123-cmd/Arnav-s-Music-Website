# Desktop App Setup

This project now has a desktop wrapper that keeps the existing web version intact.

The desktop app:

- starts the existing `server.py` backend automatically
- waits for `http://127.0.0.1:8000`
- opens the DAW in an Electron window

## What stays unchanged

- `index.html` remains the web app
- `server.py` remains the backend
- You can still run the site in the browser exactly as before

## Requirements

Install these on your computer:

- Node.js 20+
- Python 3

Check them:

```bash
node -v
python3 --version
```

On Windows, `python --version` or `py -3 --version` is also fine.

## Install desktop dependencies

From the project folder:

```bash
npm install
```

## Run the desktop app

From the project folder:

```bash
npm run app
```

The Electron app will:

1. start the local backend if it is not already running
2. wait for the backend to become ready
3. open the DAW as a desktop window

## Keep using the web version

Browser version:

```bash
python3 server.py
```

Then open:

```text
http://127.0.0.1:8000
```

## macOS notes

- If Python 3 is installed as `python3`, the app should start normally.
- If macOS blocks Electron the first time, allow it in System Settings.

## Windows notes

- The desktop wrapper tries these commands in order:
  - `py -3 server.py`
  - `python server.py`
  - `python3 server.py`
- So any normal Python 3 install should work.

## Files added for the desktop app

- `package.json`
- `desktop/main.js`
- `desktop/preload.js`
- `DESKTOP_APP_SETUP.md`
