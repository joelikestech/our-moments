# Changelog

All notable changes to the personal site project.

## [3.1] - 2026-09

### Added
- Optional HTTPS via self-signed certificate: `scripts/gen-self-signed-cert.sh`
  generates a 10-year RSA-2048 cert with SANs for your hostname + IP
- Dev server auto-detects `certs/cert.pem`/`certs/key.pem` (or `SSL_CERT_FILE`/
  `SSL_KEY_FILE`) and serves HTTPS on 8443 automatically
- `systemd/personal-site.service` — ready-made gunicorn unit with TLS on 8443
- `docker-compose.https.yml` + env-driven TLS in the Dockerfile (`HTTPS=1`),
  cert dir mounted read-only; healthcheck uses `wget --no-check-certificate`
- `certs/` added to .gitignore (private keys never reach the repo)

### Changed
- Merged the June 2026 audit-fix release: flash-message system now centralised
  in `templates/_flash.html` (shown on every page), app.py/media.py audit
  fixes, entrypoint writes data directly in `DATA_DIR` (no more symlinks in
  `/app`), gunicorn unprivileged `app` user, video viewer fixes
- README: HTTPS section; removed stale Sortable.min.js reference

### Security
- Docker images now run the app as an unprivileged user (first start chowns
  the data volume once)
- Private TLS key gets `chmod 600` and stays on the host (never in the repo)

## [3.0] - 2025-12

### Added
- Multi-user system with registration, login/logout, and role-based access (admin/user)
- Events management with create/edit/delete, recurring yearly events, and comments
- Calendar view powered by FullCalendar
- Media uploads (photos/videos) with automatic iOS Safari H.264 transcoding
- Admin panel with tabbed interface (Appearance, Content, Color Presets)
- Custom font upload (OTF/TTF/WOFF)
- 6 built-in themes (Default, Dark, Forest, Sunset, Ocean, Lavender)
- Data portability (JSON export/import)
- Docker support (Dockerfile + docker-compose.yml)
- CSRF protection via flask-wtf
- Rate limiting via flask-limiter
- Configurable session lifetime (1-90 days)
- Emergency "Reset Colors" button in admin panel

### Changed
- All JS/CSS assets bundled locally (offline/LAN ready)
- Auto-generated Flask secret key (stored in .flask_secret_key)
- Passwords hashed with scrypt via werkzeug

### Security
- Secure session cookies (HttpOnly, SameSite=Lax)
- Login rate limiting (5/minute, 20/hour)
- Admin re-authentication for sensitive actions

## [2.0] - 2024-12

### Added
- Font customization (8 playful fonts, separate body/button fonts)
- Improved admin panel with gradient header
- Live font preview in font selection
- Text content fields (welcome message, footer, labels)
- Theme presets that preserve custom text content

## [1.0] - Initial Release

### Added
- Basic Flask personal website
- Home page with photo gallery
- Simple settings management
- Color customization
