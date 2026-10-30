# OpenBooks Flask

Python Flask web app wrapping the [openbooks](https://github.com/evan-buss/openbooks) binary —
search and download ebooks from irc.irchighway.net via a simple web UI.

## Architecture

```
Browser ──HTTP──▶ Flask app (port $PORT) ──WebSocket──▶ openbooks binary (port 8080, internal)
                                                        └──IRC/TLS──▶ irc.irchighway.net:6697
                                                        └──DCC────▶  book file transfer
```

The Flask app launches the `openbooks server` binary as a subprocess, connects as its single
WebSocket client, and multiplexes requests from its own HTTP API. Books are saved under
`OPENBOOKS_DOWNLOAD_DIR/books` and served via `/api/books/<name>`.

## Run locally

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py            # assumes ./openbooks binary next to app.py
```

API:
- `GET  /` — web UI
- `POST /api/search`  `{"query": "..."}`
- `POST /api/download` `{"book": "<full string from search result>"}`
- `GET  /api/books` — downloaded files
- `GET  /api/books/<name>` — download a book file

## Deploy to Heroku (git push, no container)

The repo is a standard Python buildpack app: `openbooks` release binary (~16MB, Linux amd64)
is committed to the repo, `Procfile` runs `python app.py` (honors `$PORT`), and
`runtime.txt` pins Python 3.12.

**Via Heroku CLI:**

```bash
heroku login
heroku create my-openbooks-app
# use heroku-22 stack (matches the binary's amd64 Linux build environment)
heroku stack:set heroku-22 -a my-openbooks-app
heroku git:remote -a my-openbooks-app
git push heroku main
heroku open -a my-openbooks-app
```

**Via the Heroku dashboard (GitHub Connect):**
1. Push this repo to GitHub (`git remote add origin ... && git push -u origin main`).
2. In the Heroku dashboard: *New → Create new app* → under **Deployment method** choose
   **GitHub**, connect your account, pick this repo, then **Enable Automatic Deploys**
   (or use *Manual deploy*).
3. Under **Settings**, confirm the `heroku/python` buildpack is detected (it is, via
   `requirements.txt`). No other buildpack needed — the binary ships in the repo.

No Dockerfile is used for this method (the [Dockerfile](Dockerfile) remains as an alternative).

## Deploy to Heroku (Docker, alternative)

```bash
heroku container:login
heroku create my-openbooks-app
heroku container:push web -a my-openbooks-app
heroku container:release web -a my-openbooks-app
heroku open -a my-openbooks-app
```

Notes:
- Heroku dynos allow outbound TCP, so IRC (port 6697) and DCC transfers work there
  (they may be blocked behind corporate proxies).
- `--persist` is enabled so books are kept; on Heroku the filesystem is ephemeral
  and resets on dyno restart — download promptly.
## Optional: app bootstrapping

Set config vars as needed (all optional; defaults target irc.irchighway.net with TLS on):

```bash
heroku config:set OPENBOOKS_IRC_SERVER=irc.irchighway.net:6667 OPENBOOKS_TLS=true -a my-openbooks-app
```
