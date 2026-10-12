#!/bin/sh
set -e

# Create persistent data directory structure
mkdir -p /app/data/uploads
mkdir -p /app/data/event_photos
mkdir -p /app/data/event_videos
mkdir -p /app/data/event_videos_web
mkdir -p /app/data/thumbnails
mkdir -p /app/data/viewer_images
mkdir -p /app/data/mobile_fullscreen

# Seed default JSON files on first run (when data/ is empty)
if [ ! -f /app/data/settings.json ]; then
  cp /defaults/settings.json /app/data/settings.json
fi
if [ ! -f /app/data/events.json ]; then
  cp /defaults/events.json /app/data/events.json
fi
if [ ! -f /app/data/upload_metadata.json ]; then
  cp /defaults/upload_metadata.json /app/data/upload_metadata.json
fi
if [ ! -f /app/data/layout.json ]; then
  cp /defaults/layout.json /app/data/layout.json
fi

# Generate secret key if not present (owner-only permissions)
if [ ! -f /app/data/.flask_secret_key ]; then
  (umask 077; python -c "import secrets; print(secrets.token_hex(32))" > /app/data/.flask_secret_key)
fi

# Data files are read/written directly in DATA_DIR (/app/data, see Dockerfile) — no symlinks.
# (The old symlink approach lost data: the app's atomic os.replace() writes replaced the symlink
# itself instead of the file in the volume, so changes vanished on every container restart.)
# One-time migration for containers created with the old layout: if a real file was left behind in
# /app, it holds the newest data — move it into the volume.
for f in settings.json users.json events.json upload_metadata.json layout.json; do
  if [ -f "/app/$f" ] && [ ! -L "/app/$f" ]; then
    mv -f "/app/$f" "/app/data/$f"
  fi
  rm -f "/app/$f"
done
rm -f /app/.flask_secret_key

# Symlink media directories (remove existing dirs first so symlinks can be created)
rm -rf /app/static/uploads /app/static/event_photos /app/static/event_videos /app/static/event_videos_web /app/static/thumbnails /app/static/viewer_images /app/static/mobile_fullscreen
ln -sf /app/data/uploads /app/static/uploads
ln -sf /app/data/event_photos /app/static/event_photos
ln -sf /app/data/event_videos /app/static/event_videos
ln -sf /app/data/event_videos_web /app/static/event_videos_web
ln -sf /app/data/thumbnails /app/static/thumbnails
ln -sf /app/data/viewer_images /app/static/viewer_images
ln -sf /app/data/mobile_fullscreen /app/static/mobile_fullscreen

# Run as the unprivileged "app" user. The first start after upgrading chowns the whole data
# volume once (marker file); later starts only fix the top level.
if [ ! -f /app/data/.owned_by_app ]; then
  chown -R app:app /app/data && touch /app/data/.owned_by_app
else
  find /app/data -maxdepth 1 -exec chown app:app {} + 2>/dev/null || true
fi
chown -R app:app /app/static/fonts 2>/dev/null || true

echo "Entrypoint complete - starting application..."
exec su-exec app "$@"
