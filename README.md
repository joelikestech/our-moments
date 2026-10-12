# Personal Site

A feature-rich, self-hostable personal website built with Flask — think of it as a
family photo + events hub. Paginated photo/video feed, an events manager with
recurring dates, a synced calendar, media uploads with auto-transcoding,
multi-user accounts, a theme/color/font admin panel, and full offline/LAN
support (no CDNs).

> ## ⚠️ Read this before you run it
>
> **This project is vibecoded.** A large chunk of it was built with AI
> assistance, iterating with an LLM in a "vibe" workflow rather than written
> line-by-line by hand. It works, it's used daily, and a lot of real, human
> care went into it — but it was *shaped* by a machine.
>
> **A human put a lot of effort into this.** The code is AI-written, but a
> person designed the features, decided how everything should work, and put
> in a lot of effort testing it — exercising the edge cases (transcoding,
> uploads, session handling, the admin panel) and fixing what came out wrong.
> It is a human-designed, human-tested project that was written by AI.
>
> **Use at your own risk.** There is no warranty, no SLA, and no formal
> license. It is a personal project, not a hardened product. Before you point
> it at real data:
> - read the [Production Checklist](#production-checklist) and the
>   [Security Defaults](#security-defaults) sections,
> - change the default admin password (first login forces it),
> - keep `users.json` backed up separately — it is *not* part of exports,
> - treat any bug you find as a thing to report, not a thing to blame the
>   author over.
>
> If you run it on a home LAN you'll be fine. If you run it in production,
> you are a brave person and the checklist above is your friend. 🫡

---

## Features

- **Home Page** — Paginated photo/video feed with drag-and-drop reorder, editable captions, and a configurable welcome message
- **Events** — Create, edit, and manage events with title, date, description, photos, and videos. Supports recurring yearly events, comments, and event detail pages
- **Calendar** — FullCalendar-powered monthly/week/day view synced with the events data
- **Media Uploads** — Photos and videos validated by content, lazily generated thumbnails/poster frames, automatic H.264 transcoding for all browsers, upload metadata tracking, and per-item captions
- **Multi-User System** — Registration, login/logout, role-based access (admin/user), password management, and user administration
- **Admin Panel** — Tabbed interface for appearance (themes, colors, fonts), content (editable text labels), and color presets (save/load custom color schemes)
- **Custom Fonts** — Upload custom OTF/TTF/WOFF fonts, assign to body and button text
- **6 Built-in Themes** — Default (pastel), Dark, Forest, Sunset, Ocean, Lavender
- **Data Portability** — ZIP export/import (background jobs) for settings, events, upload metadata and media. User accounts are *not* exported.
- **Offline/LAN Ready** — All JS/CSS assets bundled locally; no CDN required
- **Docker Support** — Dockerfile and docker-compose.yml included
- **Responsive Design** — Compact pinned header, mobile-optimized layouts
- **Security** — CSRF protection, rate limiting, password hashing, secure session cookies, auto-generated secret key, configurable session lifetime

## Quick Start

### Requirements
- Python 3.8+
- Dependencies listed in `requirements.txt`
- ffmpeg/ffprobe (for video transcoding)

### Installation

```bash
# Clone or extract the project
cd personal_site

# Install dependencies
pip install -r requirements.txt

# Run the application
python app.py
```

The site starts at `http://localhost:5000`.

### Docker

```bash
docker compose up -d
```

## Default Access

- **Home:** `http://localhost:5000`
- **Login:** `http://localhost:5000/login`
- **Admin Panel:** `http://localhost:5000/admin/settings`

First run creates a default admin user (`admin`) with a random temporary
password, printed once to the console/log (`docker compose logs` for Docker).
You must change it on first login.

## Project Structure

```
personal_site/
├── app.py                      # Main Flask application (routes)
├── media.py                    # Upload validation, thumbnails/posters, transcoding, video serving
├── requirements.txt            # Python dependencies
├── Dockerfile                  # Docker container definition
├── docker-compose.yml          # Docker Compose configuration
├── .dockerignore               # Docker build exclusions
├── .gitignore                  # Git exclusions
├── settings.json               # Site settings (colors, fonts, text, themes)
├── users.json                  # User accounts (auto-created)
├── events.json                 # Events data (auto-created)
├── upload_metadata.json        # Upload tracking (auto-created)
├── layout.json                 # Home page layout order (auto-created)
├── static/
│   ├── css/
│   │   ├── base.css            # Main stylesheet
│   │   └── fullcalendar.min.css # Calendar styling
│   ├── js/
│   │   ├── events.js           # Fullscreen viewer + events page JavaScript
│   │   ├── header-clearance.js # Keeps content clear of the fixed header
│   │   └── fullcalendar.min.js # Minimal month-view calendar (offline)
│   ├── fonts/
│   │   ├── fonts.css           # Font definitions
│   │   ├── handcaps.otf        # Bundled custom font
│   │   ├── Milkyway_DEMO.ttf   # Bundled custom font
│   │   └── OutlineBubble-Regular.ttf # Bundled custom font
│   ├── uploads/                # Home feed uploads (served only via /photo and /video, never /static)
│   ├── event_photos/           # Event photo uploads
│   ├── event_videos/           # Event video uploads (originals preserved)
│   ├── event_videos_web/       # H.264 web derivatives (auto-generated)
│   └── thumbnails/<size>/<folder>/  # Lazily generated thumbnails / poster frames (sm, md, xl)
└── templates/
    ├── index.html              # Home page
    ├── events.html             # Events list
    ├── event_detail.html       # Single event view
    ├── edit_event.html         # Create/edit event
    ├── calendar.html           # Calendar view
    ├── login.html              # Login page
    ├── register.html           # Registration page
    ├── change_password.html    # Password change
    ├── admin_settings.html     # Admin panel
    ├── manage_users.html       # User management
    ├── header.html             # Shared header
    └── footer.html             # Shared footer
```

## Configuration

### Settings (`settings.json`)

Key settings managed through the admin panel:

| Setting | Description |
|---------|-------------|
| `site_title` | Website title |
| `theme` | Active theme preset |
| `body_font` / `button_font` | Font assignments |
| `welcome_message` | Home page welcome text |
| `session_lifetime_days` | Session duration (1–90 days) |
| Color fields | `background_color`, `primary_color`, `secondary_color`, `text_color`, `card_color` |

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `SECRET_KEY` | Override auto-generated Flask secret key |
| `DATA_DIR` | Directory for all mutable state (`users.json`, `events.json`, `settings.json`, `upload_metadata.json`, `layout.json`, `jobs.json`, `.flask_secret_key`, temp dirs). Defaults to the working directory; Docker uses `/app/data`. |
| `PORT` | Listen port (default 5000 for `python app.py`, 5050 in Docker) |
| `COOKIE_SECURE` | Set to `true` when served over HTTPS to mark the session cookie `Secure` |
| `FLASK_ENV` | `production` runs gunicorn in Docker (otherwise the dev server) |
| `FLASK_DEBUG` | `1` enables the Werkzeug debugger; the dev server then binds to 127.0.0.1 only |
| `TRUSTED_PROXY_COUNT` | Number of reverse proxies in front of the app (set `1` behind nginx/Caddy) so rate limits see real client IPs |
| `MAX_UPLOAD_GB` | Request body limit for uploads (default 2) — enforced while reading, so chunked uploads are covered too |
| `MAX_IMPORT_GB` | Body limit for the admin import endpoints (default 100) |
| `MIN_FREE_DISK_MB` | Uploads/imports are refused when they would leave less free space than this (default 512) |
| `LOG_LEVEL` | Python log level (default `INFO`) |
| `DISABLE_SCHEDULER` | Set to `1` to turn off the built-in recurring-events timer (e.g. if you run `process_repeating_events` from cron) |

### Registration

Self-registration is **closed by default**. An admin can switch it to *invite
code* or *open* under Admin Settings → Security. Uploads are validated by
content (only real photos and videos are stored) and user media is only served
through the authenticated `/photo/…` and `/video/…` routes, never from
`/static/`.

### Security Defaults

- CSRF protection enabled via `flask-wtf`
- Rate limiting via `flask-limiter`
- Secure session cookies (`HttpOnly`, `SameSite=Lax`)
- Auto-generated secret key stored in `.flask_secret_key`
- Passwords hashed with `werkzeug.security`

## Admin Panel

Accessible at `/admin/settings` for admin users only. Three tabs:

1. **Appearance** — Theme presets, custom colors, font selection, custom font upload
2. **Content** — Editable text for welcome message, labels, footer, and about text
3. **Color Presets** — Save and load custom color schemes

Includes an emergency "Reset Colors" button that restores visibility if colors
are accidentally set to unreadable values.

## Data Management

### Export
Admin Settings → Import/Export → **Start Export** builds a ZIP (data.json +
all media) in the background and offers it for download.
`/admin/export/json` returns the data-only JSON. User accounts (`users.json`)
are not part of the export — back that file up separately.

### Import
Upload a previous export (ZIP or JSON) or pull the JSON export from another
instance over HTTPS. Imports run in the background, validate every event,
refuse path-traversal / zip-bomb archives, and never overwrite this instance's
registration or session settings.

## Video Transcoding

Videos uploaded in non-H.264 formats (HEVC/H.265, AV1, VP9, etc.) are
automatically transcoded to H.264 for cross-browser compatibility. Originals
are preserved (unless "keep originals" is disabled in the admin Media tab).
Poster frames and photo thumbnails are generated on first request with
ffmpeg / Pillow.

- **On upload:** A background thread builds the H.264 derivative so it's ready before anyone views it
- **On demand:** If a derivative doesn't exist yet, it's built on first request and cached
- **Codec detection:** Cached by file path, size, and modification time to avoid redundant ffprobe calls
- **Derivatives stored in:** `static/event_videos_web/`

### Batch Transcode Existing Videos

After deploying, run this once to pre-build H.264 derivatives for all existing
videos:

```bash
python app.py rebuild-web-videos
```

This scans `static/event_videos/`, skips files already H.264, and builds
derivatives for unsupported codecs (HEVC, AV1, VP9, etc.).

### Requirements
- ffmpeg and ffprobe must be installed on the host. On Ubuntu: `sudo apt install ffmpeg`

## Deployment

### Systemd Service

```ini
[Unit]
Description=Personal Site
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/path/to/personal_site
Environment=DATA_DIR=/path/to/personal_site/data
Environment=FLASK_ENV=production
Environment=COOKIE_SECURE=true
Environment=TRUSTED_PROXY_COUNT=1
ExecStart=/path/to/venv/bin/gunicorn --bind 127.0.0.1:5050 --workers 2 --threads 4 --timeout 120 app:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### Production Checklist

- [ ] Set `SECRET_KEY` (or keep the generated `.flask_secret_key` in `DATA_DIR` backed up)
- [ ] Change the default admin password (the first login forces this)
- [ ] Set `COOKIE_SECURE=true` behind HTTPS (also enables HSTS)
- [ ] Place behind a reverse proxy (nginx/Caddy) and set `TRUSTED_PROXY_COUNT=1`
- [ ] Decide on registration mode (closed by default; invite code or open under Admin → Security)
- [ ] Configure appropriate file permissions on data directories
- [ ] Install ffmpeg if not already present (needed for video transcoding)
- [ ] Run `python app.py rebuild-web-videos` after initial deploy

### HTTPS (self-signed certificate)

Serve over TLS without a public CA — great for a home LAN. Three supported
paths:

**1. Dev server (simplest):** just generate a cert next to the app and run
`python app.py`. It auto-detects `certs/cert.pem` + `certs/key.pem` (or
`SSL_CERT_FILE`/`SSL_KEY_FILE` env vars) and switches to HTTPS on port 8443
automatically. No cert → plain HTTP, exactly as before.

**2. Gunicorn + systemd (host install):** use the ready-made unit in
`systemd/personal-site.service`, which binds `https://0.0.0.0:8443` with the
cert files. See the header of that file for install steps.

**3. Docker:** `docker compose -f docker-compose.https.yml up -d --build`
with `./certs` mounted read-only (see `docker-compose.https.yml` for the
exact env).

Generate the certificate:

```bash
./scripts/gen-self-signed-cert.sh [cert-dir] [san-list] [days]
# default:  certs  personal-site.local,127.0.0.1  3650 days
# typical:  ./scripts/gen-self-signed-cert.sh certs personal-site.local,192.168.1.10
```

This writes `cert.pem` / `key.pem` (RSA-2048, 10 years, SANs for your
hostname + IP). `certs/` is git-ignored so the private key never reaches the
repo.

**Making browsers stop complaining (optional):** open `certs/cert.pem` in the
browser → *Install Certificate* → Current User → put it in *Trusted Root
Certification Authorities*. After that the site loads with a green lock and
no warning.

Notes:
- Self-signed = your own machine is the CA. Fine for a trusted home network; not for public sites.
- If you'd rather use a real CA cert (Let's Encrypt), drop it into the same `certs/` dir
  (`cert.pem` + `key.pem`) — everything works the same.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Colors made text invisible | Use the "Reset Colors" button in admin panel |
| Calendar not loading | Ensure `fullcalendar.min.js` and `fullcalendar.min.css` are in `static/` |
| Fonts not displaying | Check `static/fonts/` directory and `fonts.css` |
| Videos show blank frame with audio | Install ffmpeg/ffprobe, then run `python app.py rebuild-web-videos` to build H.264 derivatives |
| Videos not playing on iOS | Wait for auto-transcode on first request, or upload H.264 videos |

## Contact / Issues

Found a bug, or want to talk about a feature? Reach out at
**jo.tech.repo@gmail.com** — or file an issue on this repo.

## License

Personal project — no formal license. Use it however you like, no warranty of
any kind, see the [notice above](#read-this-before-you-run-it).

---

**Version:** 3.1 | **Python:** 3.8+ | **Flask:** 2.0+ | **Built:** AI-written, human-designed & human-tested
