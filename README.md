# OpenBooks Flask

Python Flask web app with a **native Python IRC client** (no external binary)
that searches and downloads ebooks from irc.irchighway.net #ebooks via a web UI,
with a persistent library catalog, download queue, and Google Drive uploads.

## Architecture

```
Browser ──HTTP──▶ Flask app (port $PORT) ──IRC/TLS──▶ irc.irchighway.net:6697 #ebooks
                                              └──DCC──▶ book file transfer
```

The app's own [`irc_client.py`](irc_client.py) speaks IRC directly (a Python port
of openbooks' Go core): `@search` queries to the channel's search bot, DCC file
transfer downloads, zip extraction of search results, and the same search-result
line parser. Books are saved under `OPENBOOKS_DOWNLOAD_DIR/books` and served via
`/api/books/<name>`, or uploaded to Google Drive.

## Table alignment

Tables use a single shared renderer with fixed column layout — long titles/
errors are truncated with ellipsis so all library tables (queue, books, backlog)
stay aligned.

## Run locally

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python app.py
```

API:
- `GET  /` — web UI
- `POST /api/search`  `{"query": "..."}`
- `POST /api/download` `{"book": "<full string from search result>"}`
- `GET  /api/books` — downloaded files
- `GET  /api/books/<name>` — download a book file

## Deploy to Heroku (git push, no container)

The repo is a standard Python buildpack app (pure Python — no binary needed);
`Procfile` runs `python app.py` (honors `$PORT`), and `runtime.txt` pins Python 3.12.

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

### Google Drive uploads

The app supports either a service account (`GDRIVE_CREDENTIALS`) or user OAuth
credentials. A service account can upload to a folder shared with it only when
Google Drive permits that account to store files; Editor access alone does not
give a service account My Drive storage quota. For a personal My Drive folder,
configure OAuth credentials so uploads are stored as your Google user:

```text
GDRIVE_OAUTH_CLIENT_ID
GDRIVE_OAUTH_CLIENT_SECRET
GDRIVE_OAUTH_REFRESH_TOKEN
```

Create an OAuth client in Google Cloud Console, then use Google's OAuth 2.0
Playground with that client's credentials and the
`https://www.googleapis.com/auth/drive` scope. Authorize the Google account that
owns the Library folder, exchange the authorization code, and copy the returned
refresh token. Set all three values as Heroku config vars; never commit them.
When all three OAuth values are present they take precedence over
`GDRIVE_CREDENTIALS`. If Drive reports a service-account quota error, switching
to user OAuth is required; retrying the same service-account upload will not
resolve it.
