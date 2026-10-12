from flask import Flask, render_template, request, redirect, jsonify, send_file, Response, session, flash, url_for, g, abort
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import os, json, subprocess, re, threading, html
import uuid
import time
import secrets
from filelock import FileLock
import logging
import gzip
from io import BytesIO
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix

# Media helpers (video codec detection, transcoding, image processing, streaming)
from media import (
    get_video_codec, _cached_codec, _web_derivative_path,
    build_web_video, build_web_video_async,
    _is_video_file, _is_image_file, _is_heic_image,
    convert_heic_to_jpeg,
    stream_video, get_video_mimetype,
    sniff_upload, safe_stem, IMAGE_MIMETYPES,
    MEDIA_FOLDERS, THUMB_SIZES, get_thumbnail, prebuild_thumbnails, delete_derivatives, has_free_space,
    UPLOAD_FOLDER, EVENT_PHOTO_FOLDER, EVENT_VIDEO_FOLDER,
    EVENT_VIDEO_WEB_FOLDER, VIEWER_IMAGE_FOLDER,
    VIEWER_IMAGE_MAX_SIDE, VIEWER_IMAGE_QUALITY,
    _NON_WEB_SAFE_CODECS,
)
# Register HEIC/HEIF support for image conversion
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass  # HEIC conversion will be silently unavailable

app = Flask(__name__)

# All mutable state (JSON data files, jobs.json, secret key, temp dirs) lives in DATA_DIR.
# Defaults to the current working directory (previous behaviour); Docker sets DATA_DIR=/app/data
# so the files are written straight into the persistent volume (no symlink tricks).
DATA_DIR = os.path.abspath(os.environ.get('DATA_DIR', '.'))
os.makedirs(DATA_DIR, exist_ok=True)
USERS_FILE = os.path.join(DATA_DIR, 'users.json')
EVENTS_FILE = os.path.join(DATA_DIR, 'events.json')
SETTINGS_FILE = os.path.join(DATA_DIR, 'settings.json')
UPLOAD_META_FILE = os.path.join(DATA_DIR, 'upload_metadata.json')
LAYOUT_FILE = os.path.join(DATA_DIR, 'layout.json')

# Secret key: use env var, or generate a one-time key and store it, never hardcoded
_secret_key_path = os.path.join(DATA_DIR, '.flask_secret_key')
if os.environ.get('SECRET_KEY'):
    app.secret_key = os.environ['SECRET_KEY'].encode()
elif os.path.exists(_secret_key_path):
    with open(_secret_key_path, 'r') as f:
        app.secret_key = f.read().strip().encode()
else:
    _key = secrets.token_bytes(32).hex()
    _fd = os.open(_secret_key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # owner-only
    with os.fdopen(_fd, 'w') as f:
        f.write(_key)
    app.secret_key = _key.encode()

app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 3600  # Cache static files for 1 hour
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
# Secure cookies when behind HTTPS reverse proxy (env-driven)
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('COOKIE_SECURE', '').lower() in ('true', '1', 'yes')
# Behind a reverse proxy (nginx/Caddy) set TRUSTED_PROXY_COUNT=1 so rate limits and redirects see
# the real client address / scheme instead of the proxy's.
_proxy_count = int(os.environ.get('TRUSTED_PROXY_COUNT', '0') or 0)
if _proxy_count > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=_proxy_count, x_proto=_proxy_count, x_host=_proxy_count)
# Request body limits. The default applies to every request (uploads included) and is enforced
# by Werkzeug while the body is read, so it also covers chunked uploads without Content-Length.
# The admin import endpoints raise their own limit per request (see before_request).
MAX_UPLOAD_BYTES = int(float(os.environ.get('MAX_UPLOAD_GB', '2')) * 1024 * 1024 * 1024)
IMPORT_MAX_BYTES = int(float(os.environ.get('MAX_IMPORT_GB', '100')) * 1024 * 1024 * 1024)
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES

# Session lifetime: loaded dynamically from settings (default 30 days)
def _get_session_lifetime():
    try:
        settings = load_settings()
        days = min(int(settings.get('session_lifetime_days', 30)), 90)  # Cap at 90 days
        return timedelta(days=max(days, 1))  # Minimum 1 day
    except Exception:
        return timedelta(days=30)

app.config['PERMANENT_SESSION_LIFETIME'] = _get_session_lifetime()

# Dynamically update session lifetime on each request
def _update_session_lifetime():
    if 'user_id' in session:
        session.permanent = True
    app.config['PERMANENT_SESSION_LIFETIME'] = _get_session_lifetime()

# User-generated media lives under static/ (see media.py) but must never be served by the
# unauthenticated default static route; only /photo/<folder>/... and /video/<folder>/... may serve it.
_PRIVATE_STATIC_PREFIXES = ('uploads/', 'event_photos/', 'event_videos/', 'event_videos_web/',
                            'viewer_images/', 'mobile_fullscreen/', 'thumbnails/')

def _load_current_user():
    """Resolve the logged-in user from users.json on every request.

    Authorization is never trusted from the session cookie: a deleted user, or a user whose
    password/role changed (session_version bumped), is logged out immediately."""
    g.user = None
    user_id = session.get('user_id')
    if not user_id:
        return
    user = get_user_by_id(user_id)
    if not user or int(user.get('session_version', 0)) != int(session.get('session_version', 0)):
        session.clear()
        return
    g.user = user

def _is_admin():
    return bool(g.get('user') and g.user.get('is_admin'))

def _is_owner():
    return bool(g.get('user') and g.user.get('is_owner'))

def _bump_session_version(user):
    """Invalidate every existing session of this user (call before save_users)."""
    user['session_version'] = int(user.get('session_version', 0)) + 1
    return user['session_version']

@app.before_request
def before_request():
    if request.endpoint == 'static':
        filename = (request.view_args or {}).get('filename', '')
        if filename.startswith(_PRIVATE_STATIC_PREFIXES):
            abort(404)
    # Per-endpoint body limits
    if request.endpoint in ('import_start', 'import_data'):
        request.max_content_length = IMPORT_MAX_BYTES
    elif request.endpoint == 'save_layout':
        request.max_content_length = 1024 * 1024
    _update_session_lifetime()
    _load_current_user()
    # A temporary / admin-reset password must be replaced before using the site
    if (g.get('user') and g.user.get('must_change_password')
            and request.endpoint not in ('change_password', 'logout', 'login', 'static')):
        flash('Please choose a new password before continuing.', 'warning')
        return redirect(url_for('change_password'))

def _wants_json():
    """True for fetch/XHR callers that expect a JSON body instead of a redirect + flash."""
    return (request.is_json or 'X-CSRFToken' in request.headers
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest')

def _back(default_endpoint='index'):
    ref = request.referrer or ''
    from urllib.parse import urlparse
    parsed = urlparse(ref)
    if ref and parsed.netloc == request.host and parsed.path:
        return redirect(parsed.path + ('?' + parsed.query if parsed.query else ''))
    return redirect(url_for(default_endpoint))

@app.errorhandler(413)
def handle_too_large(e):
    limit = request.max_content_length or app.config['MAX_CONTENT_LENGTH'] or 0
    human = f'{limit / 1024 ** 3:g} GB' if limit >= 1024 ** 3 else f'{limit / 1024 ** 2:g} MB'
    msg = f'Upload too large. Maximum {human} per request.'
    if _wants_json():
        return jsonify({'error': msg}), 413
    flash(msg, 'danger')
    return _back()

# CSRF protection
csrf = CSRFProtect(app)

# Handle CSRF errors as JSON for AJAX requests
@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    msg = 'Your session expired or the form was out of date. Please reload the page and try again.'
    if _wants_json():
        return jsonify({'error': msg}), 400
    flash(msg, 'danger')
    return _back('index' if g.get('user') else 'login')

@app.errorhandler(429)
def handle_rate_limit(e):
    msg = 'Too many attempts. Please wait a little and try again.'
    if _wants_json():
        return jsonify({'error': msg}), 429
    flash(msg, 'warning')
    return _back('login')

# Rate limiting
limiter = Limiter(app=app, key_func=get_remote_address, default_limits=[])

_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")

# Gzip compression middleware
@app.after_request
def after_request_handler(response):
    # Add cache headers for static assets
    if request.path.startswith('/static/'):
        if any(request.path.endswith(ext) for ext in ('.otf', '.ttf', '.woff', '.woff2')):
            response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        elif any(request.path.endswith(ext) for ext in ('.css', '.js')):
            response.headers['Cache-Control'] = 'public, max-age=3600'
        elif any(request.path.endswith(ext) for ext in ('.jpg', '.jpeg', '.png', '.gif', '.webp')):
            response.headers['Cache-Control'] = 'public, max-age=86400'
    
    # Basic hardening headers (media routes add their own Content-Type handling)
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    # Content Security Policy. Templates still use inline <script> blocks and onclick= handlers,
    # so script/style keep 'unsafe-inline'; everything else (objects, frames, form targets,
    # remote scripts/images) is locked to this origin.
    response.headers.setdefault('Content-Security-Policy', _CSP)
    if app.config.get('SESSION_COOKIE_SECURE'):
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')

    # Gzip compression
    # Don't compress if already compressed, streamed (SSE / generators would be buffered forever),
    # too small, or client doesn't accept gzip
    if (response.direct_passthrough or
        response.is_streamed or
        response.mimetype == 'text/event-stream' or
        response.status_code < 200 or
        response.status_code >= 300 or
        'Content-Encoding' in response.headers or
        'gzip' not in request.headers.get('Accept-Encoding', '').lower()):
        return response
    
    data = response.get_data()
    if len(data) < 500:
        return response
    
    # Don't compress images/videos (already compressed)
    content_type = response.content_type or ''
    if any(t in content_type for t in ['image/', 'video/', 'audio/']):
        return response
    
    # Compress the response
    gzip_buffer = BytesIO()
    with gzip.GzipFile(mode='wb', fileobj=gzip_buffer, compresslevel=6) as gz:
        gz.write(data)
    
    response.set_data(gzip_buffer.getvalue())
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Length'] = len(response.get_data())
    response.headers['Vary'] = 'Accept-Encoding'
    
    return response

# Configure logging
is_production = os.environ.get('FLASK_ENV') == 'production'
logging.basicConfig(level=getattr(logging, os.environ.get('LOG_LEVEL', 'INFO').upper(), logging.INFO))

# Folder constants imported from media.py (directories already created there)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['EVENT_PHOTO_FOLDER'] = EVENT_PHOTO_FOLDER
app.config['EVENT_VIDEO_FOLDER'] = EVENT_VIDEO_FOLDER
app.config['EVENT_VIDEO_WEB_FOLDER'] = EVENT_VIDEO_WEB_FOLDER

# Temporary directory for large imports (avoids /tmp RAM disk limits)
IMPORT_TEMP_DIR = os.path.join(DATA_DIR, 'tmp_import')
os.makedirs(IMPORT_TEMP_DIR, exist_ok=True)
# Also redirect Python's tempfile module to avoid /tmp filling up during large uploads
os.environ['TMPDIR'] = IMPORT_TEMP_DIR


# (All media helpers moved to media.py)

# Authentication decorators
def _login_redirect():
    # Relative path only (request.url is absolute and would be rejected by the open-redirect check)
    return redirect(url_for('login', next=request.full_path.rstrip('?')))

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not g.get('user'):
            flash('Please log in to access this page.', 'warning')
            return _login_redirect()
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not g.get('user'):
            flash('Please log in to access this page.', 'warning')
            return _login_redirect()
        if not _is_admin():
            flash('Admin access required.', 'danger')
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# Admin reauthentication: checks if admin session is "fresh" enough for sensitive operations
def admin_fresh_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not g.get('user'):
            flash('Please log in to access this page.', 'warning')
            return _login_redirect()
        if not _is_admin():
            flash('Admin access required.', 'danger')
            return redirect(url_for('index'))
        
        if request.method == 'GET':
            return f(*args, **kwargs)  # viewing admin pages only needs an admin session

        # Check session freshness for changes
        try:
            settings = load_settings()
            reauth_hours = min(int(settings.get('admin_reauth_hours', 4)), 24)  # Cap at 24 hours
        except Exception:
            reauth_hours = 4
        
        login_time = session.get('login_timestamp')
        if login_time:
            elapsed = (time.time() - login_time) / 3600  # hours
            if elapsed > reauth_hours:
                flash('Your admin session has expired. Please log in again to make changes.', 'warning')
                return _login_redirect()
        else:
            # No timestamp recorded (old session), require re-login
            flash('Please log in again to verify your identity.', 'warning')
            return _login_redirect()
        
        return f(*args, **kwargs)
    return decorated_function

# User management functions
def load_users():
    try:
        if not os.path.exists(USERS_FILE):
            # Generate a random initial password and print it once
            initial_password = secrets.token_urlsafe(8)
            default_users = [{
                "id": str(uuid.uuid4()),
                "username": "admin",
                "password": generate_password_hash(initial_password),
                "is_admin": True,
                "is_owner": True,
                "must_change_password": True,
                "created_at": datetime.now().isoformat()
            }]
            save_users(default_users)
            app.logger.warning(
                f"First run: created default admin user.\n"
                f"Username: admin\n"
                f"Temporary password: {initial_password}\n"
                f"Please change this password after first login."
            )
            return default_users
        with open(USERS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        app.logger.error(f"Error loading users.json: {e}")
        return []

def save_users(users):
    try:
        with FileLock(USERS_FILE + '.lock'):
            tmp = USERS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(users, f, indent=2, ensure_ascii=False)
            os.replace(tmp, USERS_FILE)  # atomic on Linux
    except Exception as e:
        app.logger.error(f"Error saving users: {e}")
        raise

def get_user_by_username(username):
    users = load_users()
    return next((u for u in users if u['username'] == username), None)

def get_user_by_id(user_id):
    users = load_users()
    return next((u for u in users if u['id'] == user_id), None)

# Upload metadata functions
def load_upload_metadata():
    try:
        if not os.path.exists(UPLOAD_META_FILE):
            return {}
        with open(UPLOAD_META_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        app.logger.error(f"Error loading upload_metadata.json: {e}")
        return {}

def save_upload_metadata(metadata):
    try:
        with FileLock(UPLOAD_META_FILE + '.lock'):
            tmp = UPLOAD_META_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            os.replace(tmp, UPLOAD_META_FILE)
    except Exception as e:
        app.logger.error(f"Error saving upload metadata: {e}")
        raise

def can_delete_upload(filename, metadata=None):
    """Check if current user can delete this upload"""
    if _is_admin():
        return True
    if metadata is None:
        metadata = load_upload_metadata()
    file_info = metadata.get(filename, {})
    return file_info.get('user_id') == session.get('user_id')

# Auth routes
# Rate limit login attempts: 5 per minute, 20 per hour
def _login_key():
    # Per IP *and* username, so one household member's typos don't lock the others out;
    # the plain per-IP limit below still caps brute force across usernames.
    return f"{get_remote_address()}|{(request.form.get('username') or '').strip().lower()[:64]}"

# Timing-equalising hash for unknown usernames (prevents username enumeration via response time)
_DUMMY_PASSWORD_HASH = generate_password_hash(secrets.token_urlsafe(16))

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5/minute", methods=['POST'], key_func=_login_key)
@limiter.limit("20/hour", methods=['POST'], key_func=_login_key)
@limiter.limit("30/minute", methods=['POST'])
def login():
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        
        user = get_user_by_username(username)
        password_ok = check_password_hash(user['password'] if user else _DUMMY_PASSWORD_HASH, password)
        
        if user and password_ok:
            session.clear()  # fresh session on login (no fixation / stale flashes)
            session.permanent = True
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['session_version'] = int(user.get('session_version', 0))
            session['login_timestamp'] = time.time()  # Track when admin logged in

            if user.get('must_change_password'):
                flash('Please set a new password to continue.', 'warning')
                return redirect(url_for('change_password'))

            flash(f'Welcome back, {username}!', 'success')
            
            next_page = request.args.get('next')
            # Prevent open redirect: only allow relative URLs
            if next_page and (not next_page.startswith('/') or next_page.startswith('//')):
                next_page = None
            return redirect(next_page or url_for('index'))
        else:
            flash('Invalid username or password.', 'danger')
    
    return render_template('login.html')

@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    flash('You have been logged out.', 'info')
    return redirect(url_for('login'))

# Rate limit registration: 3 per hour
@app.route('/register', methods=['GET', 'POST'])
@limiter.limit("3/hour", methods=['POST'])
def register():
    settings = load_settings()
    mode = settings.get('registration_mode', 'closed')
    if mode not in ('open', 'invite'):
        flash('Registration is closed. Ask the site admin for an account or an invite code.', 'warning')
        return redirect(url_for('login'))

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if mode == 'invite':
            expected = str(settings.get('registration_invite_code') or '')
            supplied = request.form.get('invite_code', '')
            if not expected or not secrets.compare_digest(supplied, expected):
                flash('Invalid invite code.', 'danger')
                return render_template('register.html')
        
        if not username or not password:
            flash('Username and password are required.', 'danger')
            return render_template('register.html')

        if not re.fullmatch(r'[A-Za-z0-9_.-]{2,32}', username):
            flash('Username must be 2-32 characters: letters, digits, dot, dash or underscore.', 'danger')
            return render_template('register.html')
        
        if password != confirm_password:
            flash('Passwords do not match.', 'danger')
            return render_template('register.html')
        
        if len(password) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return render_template('register.html')
        
        if any(u['username'].lower() == username.lower() for u in load_users()):
            flash('Username already exists.', 'danger')
            return render_template('register.html')
        
        users = load_users()
        new_user = {
            "id": str(uuid.uuid4()),
            "username": username,
            "password": generate_password_hash(password),
            "is_admin": False,
            "created_at": datetime.now().isoformat()
        }
        users.append(new_user)
        save_users(users)
        
        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))
    
    return render_template('register.html')

@app.route('/users')
@admin_required
def manage_users():
    users = load_users()
    return render_template('manage_users.html', users=users)

# Rate limit sensitive admin actions
@app.route('/users/toggle_admin/<user_id>', methods=['POST'])
@limiter.limit("10/hour")
@admin_fresh_required
def toggle_admin(user_id):
    users = load_users()
    user = next((u for u in users if u['id'] == user_id), None)
    
    if user:
        if user['id'] == session['user_id']:
            flash('You cannot change your own admin status.', 'warning')
        elif user.get('is_owner') and not _is_owner():
            flash('Only the owner can modify owner settings.', 'danger')
        else:
            user['is_admin'] = not user.get('is_admin', False)
            _bump_session_version(user)  # role change takes effect on their next request
            save_users(users)
            flash(f"Admin status updated for {user['username']}.", 'success')
    
    return redirect(url_for('manage_users'))

@app.route('/users/delete/<user_id>', methods=['POST'])
@limiter.limit("10/hour")
@admin_fresh_required
def delete_user(user_id):
    if user_id == session['user_id']:
        flash('You cannot delete your own account.', 'warning')
        return redirect(url_for('manage_users'))
    
    users = load_users()
    target_user = next((u for u in users if u['id'] == user_id), None)
    
    if target_user and target_user.get('is_owner'):
        flash('The owner account cannot be deleted.', 'danger')
        return redirect(url_for('manage_users'))
    
    users = [u for u in users if u['id'] != user_id]
    save_users(users)
    flash('User deleted successfully.', 'success')
    return redirect(url_for('manage_users'))

@app.route('/admin/reset_password/<user_id>', methods=['POST'])
@limiter.limit("10/hour")
@admin_fresh_required
def reset_user_password(user_id):
    if user_id == session['user_id']:
        flash('Use the Change Password page to change your own password.', 'warning')
        return redirect(url_for('manage_users'))
    
    users = load_users()
    user = next((u for u in users if u['id'] == user_id), None)
    
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('manage_users'))
    
    # Protect owner from other admins
    if user.get('is_owner') and not _is_owner():
        flash('Only the owner can reset the owner password.', 'danger')
        return redirect(url_for('manage_users'))
    
    # Generate a cryptographically secure random password
    new_password = secrets.token_urlsafe(8)
    
    # Update user's password
    for u in users:
        if u['id'] == user_id:
            u['password'] = generate_password_hash(new_password)
            u['must_change_password'] = True
            _bump_session_version(u)  # log out their existing sessions
            break
    
    save_users(users)
    flash(f'Password for {user["username"]} has been reset to: {new_password}', 'success')
    return redirect(url_for('manage_users'))

# Rate limit password changes: 5 per hour
@app.route('/change_password', methods=['GET', 'POST'])
@limiter.limit("5/hour", methods=['POST'])
@login_required
def change_password():
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        
        user = get_user_by_id(session['user_id'])
        
        if not check_password_hash(user['password'], current_password):
            flash('Current password is incorrect.', 'danger')
            return render_template('change_password.html')
        
        if new_password != confirm_password:
            flash('New passwords do not match.', 'danger')
            return render_template('change_password.html')
        
        if len(new_password) < 8:
            flash('Password must be at least 8 characters.', 'danger')
            return render_template('change_password.html')
        if new_password == current_password:
            flash('New password must be different from the current one.', 'danger')
            return render_template('change_password.html')
        
        users = load_users()
        for u in users:
            if u['id'] == session['user_id']:
                u['password'] = generate_password_hash(new_password)
                u['must_change_password'] = False  # Clear flag after password change
                session['session_version'] = _bump_session_version(u)  # keep this session, drop others
                break
        
        save_users(users)
        flash('Password changed successfully!', 'success')
        return redirect(url_for('index'))
    
    return render_template('change_password.html')

# Original routes
@app.route('/')
@login_required
def index():
    images = os.listdir(UPLOAD_FOLDER)
    image_content = {}
    layout_order = {}

    if os.path.exists(LAYOUT_FILE):
        with open(LAYOUT_FILE) as f:
            layout_order = json.load(f)

    def sort_key(img):
        if img in layout_order:
            return layout_order[img]['order']
        try:
            return -int(img.split('_')[0])
        except (ValueError, IndexError):
            return 0

    images = sorted(images, key=sort_key)

    # Pagination (the feed used to render every upload on one page)
    per_page = 24
    total_items = len(images)
    total_pages = max((total_items + per_page - 1) // per_page, 1)
    page = request.args.get('page', 1, type=int) or 1
    page = min(max(page, 1), total_pages)
    images = images[(page - 1) * per_page: page * per_page]

    for img in images:
        if img.endswith('.txt'):
            try:
                with open(os.path.join(UPLOAD_FOLDER, img)) as f:
                    image_content[img] = f.read()
            except Exception as e:
                app.logger.error(f"Error reading note {img}: {e}")
                image_content[img] = "(Error reading note)"

    # Add ownership info to each image
    file_permissions = {}
    upload_metadata = load_upload_metadata()
    for img in images:
        file_permissions[img] = can_delete_upload(img, upload_metadata)

    return render_template('index.html', images=images, image_content=image_content, 
                         layout=layout_order, file_permissions=file_permissions, 
                         upload_metadata=upload_metadata,
                         current_page=page, total_pages=total_pages, total_items=total_items)

@app.route('/upload', methods=['POST'])
@login_required
def upload():
    if not has_free_space(UPLOAD_FOLDER, request.content_length):
        flash('Not enough free disk space on the server for this upload.', 'danger')
        return redirect(url_for('index'))
    media_files = request.files.getlist('media')
    content = request.form.get('content', '').strip()
    timestamp = str(int(time.time()))
    metadata = load_upload_metadata()
    new_filenames = []

    # Filter out empty files
    valid_media = [f for f in media_files if f and f.filename]

    # Load layout
    layout_order = {}
    if os.path.exists(LAYOUT_FILE):
        with open(LAYOUT_FILE) as f:
            layout_order = json.load(f)

    if valid_media:
        for media in valid_media:
            sniffed = sniff_upload(media)
            if not sniffed:
                flash(f'"{media.filename}" was skipped: only photo and video files are allowed.', 'danger')
                continue
            kind, ext = sniffed
            # Server-generated name: timestamp + random + sanitised original stem + *sniffed* extension
            filename = f"{timestamp}_{uuid.uuid4().hex[:8]}_{safe_stem(media.filename)}.{ext}"
            saved_path = os.path.join(UPLOAD_FOLDER, filename)
            media.save(saved_path)
            # Auto-convert HEIC images to JPEG
            if _is_heic_image(filename):
                jpeg_path = convert_heic_to_jpeg(saved_path)
                if jpeg_path:
                    os.remove(saved_path)
                    filename = os.path.basename(jpeg_path)
            metadata[filename] = {
                'user_id': session['user_id'],
                'username': session['username'],
                'uploaded_at': datetime.now().isoformat(),
                'caption': content if content else ''
            }
            new_filenames.append(filename)
            # Trigger async H.264 derivative for videos
            if kind == 'video':
                full_path = os.path.join(UPLOAD_FOLDER, filename)
                build_web_video_async(full_path)
            # Pre-build feed thumbnails / poster in the background
            prebuild_thumbnails('uploads', filename, sizes=('sm', 'md'))
            # Increment timestamp to avoid collisions in batch uploads
            timestamp = str(int(time.time()) + len(new_filenames))

    if content and not valid_media:
        # Text-only post
        text_filename = f"{timestamp}_note.txt"
        with open(os.path.join(UPLOAD_FOLDER, text_filename), 'w') as f:
            f.write(content)
        metadata[text_filename] = {
            'user_id': session['user_id'],
            'username': session['username'],
            'uploaded_at': datetime.now().isoformat()
        }
        new_filenames.append(text_filename)

    save_upload_metadata(metadata)

    # Add new items to top of layout
    if new_filenames:
        # Shift all existing items down
        shift = len(new_filenames)
        for item in layout_order:
            layout_order[item]['order'] += shift

        # Add new items at top (most recent first)
        for i, fname in enumerate(reversed(new_filenames)):
            layout_order[fname] = {'order': i}

        # Normalize and save
        layout_order = normalize_layout(layout_order)
        _write_json_atomic(LAYOUT_FILE, layout_order)

    return redirect('/')

@app.route('/delete', methods=['POST'])
@login_required
def delete():
    filename = secure_filename(request.form.get('filename', ''))
    
    if not filename:
        flash('Invalid filename.', 'danger')
        return redirect('/')
    
    if not can_delete_upload(filename):
        flash('You do not have permission to delete this item.', 'danger')
        return redirect('/')
    
    path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(path):
        os.remove(path)
        delete_derivatives('uploads', filename)  # thumbnails, poster, H.264 copy
        
        # Remove from metadata
        metadata = load_upload_metadata()
        if filename in metadata:
            del metadata[filename]
            save_upload_metadata(metadata)

        # Remove from layout so layout.json doesn't grow forever
        with FileLock(LAYOUT_FILE + '.lock'):
            layout_order = _read_json(LAYOUT_FILE, {})
            if filename in layout_order:
                layout_order.pop(filename, None)
                _write_json_atomic(LAYOUT_FILE, layout_order, locked=True)
    
    return redirect('/')

@app.route('/edit_caption', methods=['POST'])
@login_required
def edit_caption():
    filename = secure_filename(request.form.get('filename', ''))
    new_caption = request.form.get('caption', '')
    
    if not filename:
        flash('Invalid filename.', 'danger')
        return redirect('/')
    
    if not can_delete_upload(filename):  # Only owner can edit caption
        flash('You do not have permission to edit this caption.', 'danger')
        return redirect('/')
    
    metadata = load_upload_metadata()
    if filename in metadata:
        metadata[filename]['caption'] = new_caption
        save_upload_metadata(metadata)
        flash('Caption updated!', 'success')
    
    return redirect('/')

@app.route('/save_layout', methods=['POST'])
@login_required
def save_layout():
    """Reorder feed items. The client posts {filename: {order: n}} for the items it shows
    (one page); those items are re-ordered within the positions they already occupy, so the
    rest of the layout is untouched. Input is validated: any other shape is rejected."""
    posted = request.get_json(silent=True)
    if not isinstance(posted, dict) or len(posted) > 5000:
        return jsonify({'error': 'Invalid layout payload'}), 400
    try:
        posted_sorted = sorted(
            ((str(k), int(v['order'])) for k, v in posted.items() if isinstance(v, dict)),
            key=lambda kv: kv[1])
    except (KeyError, TypeError, ValueError):
        return jsonify({'error': 'Invalid layout payload'}), 400
    if len(posted_sorted) != len(posted):
        return jsonify({'error': 'Invalid layout payload'}), 400
    posted_keys = [k for k, _ in posted_sorted]
    posted_set = set(posted_keys)

    existing_files = set(os.listdir(UPLOAD_FOLDER))
    if not posted_set <= existing_files:
        return jsonify({'error': 'Unknown item in layout'}), 400

    with FileLock(LAYOUT_FILE + '.lock'):
        layout = _read_json(LAYOUT_FILE, {})
        if not isinstance(layout, dict):
            layout = {}
        # Full current ordering: known items by order, then unknown files by timestamp (newest first)
        known = [k for k, _ in sorted(((k, v.get('order', 0)) for k, v in layout.items()
                                       if isinstance(v, dict) and k in existing_files), key=lambda kv: kv[1])]
        rest = sorted(existing_files - set(known), key=lambda f: -_leading_int(f))
        ordering = known + rest
        # Replace the positions occupied by the posted items with the posted order
        positions = [i for i, k in enumerate(ordering) if k in posted_set]
        for pos, key in zip(positions, posted_keys):
            ordering[pos] = key
        layout = {k: {'order': i} for i, k in enumerate(ordering)}
        _write_json_atomic(LAYOUT_FILE, layout, locked=True)
    return jsonify({'ok': True})

def _leading_int(name):
    try:
        return int(name.split('_')[0])
    except (ValueError, IndexError):
        return 0

def _read_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def _write_json_atomic(path, data, locked=False):
    """tmp + rename write; takes the file lock unless the caller already holds it."""
    def _do():
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    if locked:
        _do()
    else:
        with FileLock(path + '.lock'):
            _do()

def normalize_layout(layout):
    ordered = sorted(layout.items(), key=lambda item: item[1]['order'])
    seen = set()
    for i, (key, val) in enumerate(ordered):
        while val['order'] in seen:
            val['order'] += 1
        seen.add(val['order'])
    return dict(ordered)

@app.route('/layout_stream')
@login_required
def layout_stream():
    def event_stream():
        last_mtime = -1
        try:
            while True:
                try:
                    current_mtime = os.path.getmtime(LAYOUT_FILE)
                    if current_mtime != last_mtime:
                        with open(LAYOUT_FILE) as f:
                            data = f.read()
                        yield f"data: {data}\n\n"
                        last_mtime = current_mtime
                except FileNotFoundError:
                    yield "data: {}\n\n"
                except Exception:
                    yield "data: {}\n\n"
                time.sleep(1)
        except GeneratorExit:
            pass  # client disconnected; clean exit
    return Response(event_stream(), mimetype='text/event-stream')

# Photo serving route (authenticated, prevents unauthenticated access to static photos)
_PHOTO_DIRS = {
    'event_photos': EVENT_PHOTO_FOLDER,
    'uploads': UPLOAD_FOLDER,
    'viewer_images': VIEWER_IMAGE_FOLDER,
    'mobile_fullscreen': 'static/mobile_fullscreen',
}

@app.route('/photo/<folder>/<path:filename>')
@login_required
def serve_photo(folder, filename):
    """Serve photos through an authenticated route.
    Prevents unauthenticated access to static photo directories."""
    base = _PHOTO_DIRS.get(folder)
    if base is None:
        return "Not found", 404
    filename = secure_filename(filename)
    filepath = os.path.join(base, filename)
    if not os.path.exists(filepath):
        return "Not found", 404
    # Only image types are served here, with an explicit Content-Type (never guessed from the
    # extension) so a stray .html/.svg in the folder can never execute on this origin.
    ext = os.path.splitext(filename)[1].lower().lstrip('.')
    mimetype = IMAGE_MIMETYPES.get(ext)
    if mimetype is None:
        return "Not found", 404
    response = send_file(filepath, mimetype=mimetype, conditional=True)
    response.headers['Cache-Control'] = 'private, max-age=3600'
    response.headers['Content-Disposition'] = 'inline'
    return response

# Video serving route (Range request support, derivative-aware)
_VIDEO_DIRS = {
    'event_videos': EVENT_VIDEO_FOLDER,
    'uploads': UPLOAD_FOLDER,
}

@app.route('/video/<folder>/<path:filename>')
@login_required
def serve_video(folder, filename):
    """Serve videos with Range request support.
    Serves prebuilt H.264 derivative when available, falls back to original.
    If original is missing (transcoded with keep_originals=False), serves derivative."""
    base = _VIDEO_DIRS.get(folder)
    if base is None:
        return "Not found", 404
    filename = secure_filename(filename)
    original = os.path.join(base, filename)
    derivative = _web_derivative_path(original)
    has_deriv = os.path.exists(derivative) and os.path.getsize(derivative) > 0
    if os.path.exists(original):
        target = derivative if has_deriv else original
    elif has_deriv:
        target = derivative
    else:
        return "Not found", 404
    # send_file(conditional=True) implements Range/If-Range/ETag correctly (suffix and multi
    # ranges included) — the hand-rolled parser used to 500 on anything but "bytes=a-b".
    response = send_file(target, mimetype=get_video_mimetype(target), conditional=True)
    response.headers['Cache-Control'] = 'private, max-age=86400'
    return response

@app.route('/thumb/<size>/<folder>/<path:filename>')
@login_required
def serve_thumb(size, folder, filename):
    """Resized JPEG for photos, poster frame for videos (built on first request, then cached).
    sizes: sm (strips), md (feed cards), xl (fullscreen viewer)."""
    if size not in THUMB_SIZES or folder not in MEDIA_FOLDERS:
        return "Not found", 404
    filename = secure_filename(filename)
    path = get_thumbnail(folder, filename, size)
    if path is None:
        src = os.path.join(MEDIA_FOLDERS[folder], filename)
        ext = os.path.splitext(filename)[1].lower().lstrip('.')
        if os.path.isfile(src) and ext in IMAGE_MIMETYPES and folder in _PHOTO_DIRS:
            return redirect(url_for('serve_photo', folder=folder, filename=filename))  # fall back to original
        return "Not found", 404
    response = send_file(path, mimetype='image/jpeg', conditional=True)
    response.headers['Cache-Control'] = 'private, max-age=86400'
    return response

@app.route('/calendar')
@login_required
def calendar():
    events = load_events()
    # Escape titles to prevent stored XSS in FullCalendar (renders with innerHTML)
    for event in events:
        if event.get('title'):
            event['title'] = html.escape(event['title'])
    return render_template('calendar.html', events=events)

@app.route('/add_event', methods=['POST'])
@login_required
def add_event():
    if not has_free_space(EVENT_PHOTO_FOLDER, request.content_length):
        if _wants_json():
            return jsonify({"error": "Not enough free disk space on the server."}), 507
        flash('Not enough free disk space on the server for this upload.', 'danger')
        return redirect('/calendar')
    try:
        data, err = _parse_event_form(request.form)
        if err:
            return _event_error(err)
        media_files = request.files.getlist('media')

        photos, videos = [], []
        skipped = []
        for media in media_files:
            if media and media.filename:
                sniffed = sniff_upload(media)
                if not sniffed:
                    skipped.append(media.filename)
                    continue
                kind, ext = sniffed
                filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_stem(media.filename)}.{ext}"
                if kind == 'image':
                    saved_path = os.path.join(EVENT_PHOTO_FOLDER, filename)
                    media.save(saved_path)
                    # Auto-convert HEIC images to JPEG
                    if _is_heic_image(filename):
                        jpeg_path = convert_heic_to_jpeg(saved_path)
                        if jpeg_path:
                            # Remove original HEIC, use JPEG instead
                            os.remove(saved_path)
                            filename = os.path.basename(jpeg_path)
                            saved_path = os.path.join(EVENT_PHOTO_FOLDER, filename)
                    prebuild_thumbnails('event_photos', filename)
                    photos.append({
                        'filename': filename,
                        'uploaded_by': session['username'],
                        'user_id': session['user_id']
                    })
                elif kind == 'video':
                    media.save(os.path.join(EVENT_VIDEO_FOLDER, filename))
                    build_web_video_async(filename)
                    prebuild_thumbnails('event_videos', filename, sizes=('sm',))
                    videos.append({
                        'filename': filename,
                        'uploaded_by': session['username'],
                        'user_id': session['user_id']
                    })

        event = {
            "id": str(uuid.uuid4()),
            "title": data['title'],
            "start": data['start'],
            "end": data['end'],
            "notes": data['notes'],
            "repeat_yearly": data['repeat_yearly'],
            "photos": photos,
            "videos": videos,
            "comments": [],  # User comments/notes
            "user_id": session['user_id'],
            "username": session['username']
        }

        events = load_events()
        events.append(event)
        save_events(events)
        if skipped:
            flash('Skipped (not a photo/video): ' + ', '.join(skipped), 'warning')
        
        # Check if this is an AJAX request or form submission
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"success": True, "redirect": "/calendar"})
        else:
            return redirect('/calendar')
    except Exception as e:
        app.logger.error(f"Error adding event: {e}", exc_info=True)
        return _event_error('Could not save the event. Please try again.', 500)

def _parse_event_form(form):
    """Validate the shared event fields. Returns (data, error_message)."""
    title = (form.get('title') or '').strip()
    start = (form.get('start') or '').strip()
    end = (form.get('end') or '').strip()
    notes = (form.get('notes') or '').strip()
    if not title or not start:
        return None, 'Title and start time are required.'
    if len(title) > 200:
        return None, 'Title is too long (200 characters max).'
    if len(notes) > 20000:
        return None, 'Notes are too long (20,000 characters max).'
    try:
        start_dt = datetime.fromisoformat(start.replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(end.replace('Z', '+00:00')) if end else None
        if end_dt is not None and end_dt < start_dt:
            return None, 'End time must be after the start time.'
    except (ValueError, TypeError):
        return None, 'Invalid date/time. Use the date picker (YYYY-MM-DDTHH:MM).'
    return {'title': title, 'start': start, 'end': end, 'notes': notes,
            'repeat_yearly': form.get('repeat_yearly') == 'on'}, None

def _event_error(message, code=400, back='/calendar'):
    if _wants_json():
        return jsonify({'error': message}), code
    flash(message, 'danger')
    return redirect(back)

def normalize_media_list(items):
    """Convert old string format media items to dict format (backward compatibility).
    
    Old format: ['photo1.jpg', 'photo2.jpg']
    New format: [{'filename': 'photo1.jpg', 'uploaded_by': 'unknown', 'user_id': None}]
    """
    if not items:
        return items
    return [
        item if isinstance(item, dict)
        else {'filename': item, 'uploaded_by': 'unknown', 'user_id': None}
        for item in items
    ]

def can_edit_event(event):
    """Check if current user can edit this event (legacy - kept for compatibility)"""
    if _is_admin():
        return True
    return event.get('user_id') == session.get('user_id')

def can_add_to_event(event):
    """Check if user can add media/notes to event - anyone can!"""
    return True  # Anyone logged in can add to any event

def can_edit_event_details(event):
    """Check if user can edit event title/dates/main details"""
    if _is_admin():
        return True
    return event.get('user_id') == session.get('user_id')

def can_delete_event_media(media_item):
    """Check if user can delete a specific media item"""
    if _is_admin():
        return True
    if isinstance(media_item, dict):
        return media_item.get('user_id') == session.get('user_id')
    return False  # Old format without owner info - only admin can delete

@app.route('/event/<event_id>')
@login_required
def view_event(event_id):
    try:
        events = load_events()
        event = next((e for e in events if e['id'] == event_id), None)
        if not event:
            return render_template('event_detail.html', event=None)
        
        # Normalize photos and videos to new format (backward compatibility)
        if event.get('photos'):
            normalized_photos = []
            for photo in normalize_media_list(event['photos']):
                photo_filename = photo.get('filename', '')
                if os.path.exists(os.path.join(EVENT_PHOTO_FOLDER, photo_filename)):
                    normalized_photos.append(photo)
            event['photos'] = normalized_photos
        
        if event.get('videos'):
            normalized_videos = []
            for video in normalize_media_list(event['videos']):
                video_filename = video.get('filename', '')
                original_path = os.path.join(EVENT_VIDEO_FOLDER, video_filename)
                derivative_path = _web_derivative_path(original_path)
                # Check original OR derivative (derivative may exist if original was deleted after transcode)
                if os.path.exists(original_path) or (os.path.exists(derivative_path) and os.path.getsize(derivative_path) > 0):
                    normalized_videos.append(video)
            event['videos'] = normalized_videos
        
        event['can_edit'] = can_edit_event(event)
        return render_template('event_detail.html', event=event)
    except Exception as e:
        app.logger.error(f"Error rendering event {event_id}: {e}")
        return "Internal server error", 500

@app.route('/edit_event/<event_id>', methods=['GET', 'POST'])
@login_required
def edit_event(event_id):
    try:
        events = load_events()
        event = next((e for e in events if e['id'] == event_id), None)
        if not event:
            return "Event not found", 404
        
        # Anyone can access edit page to add media
        can_edit_details = can_edit_event_details(event)

        if request.method == 'POST':
            if not has_free_space(EVENT_PHOTO_FOLDER, request.content_length):
                flash('Not enough free disk space on the server for this upload.', 'danger')
                return redirect(url_for('edit_event', event_id=event_id))
            # Only event creator/admin can edit main details
            if can_edit_details:
                data, err = _parse_event_form(request.form)
                if err:
                    flash(err, 'danger')
                    return redirect(url_for('edit_event', event_id=event_id))
                event.update(data)

            delete_photos = request.form.getlist('delete_photos')
            delete_videos = request.form.getlist('delete_videos')
            
            # Handle both old format (strings) and new format (objects)
            updated_photos = []
            for p in event.get('photos', []):
                photo_filename = p if isinstance(p, str) else p.get('filename')
                # Check if user wants to delete and has permission
                if photo_filename in delete_photos:
                    if can_delete_event_media(p):
                        # Delete the file
                        path = os.path.join(EVENT_PHOTO_FOLDER, photo_filename)
                        if os.path.exists(path):
                            os.remove(path)
                        delete_derivatives('event_photos', photo_filename)
                        continue  # Skip adding to updated list
                    else:
                        # No permission - keep the photo and show warning
                        flash('You can only delete media you uploaded.', 'warning')
                
                # Keep this photo (normalize format)
                updated_photos.extend(normalize_media_list([p]))
            event['photos'] = updated_photos
            
            updated_videos = []
            for v in event.get('videos', []):
                video_filename = v if isinstance(v, str) else v.get('filename')
                # Check if user wants to delete and has permission
                if video_filename in delete_videos:
                    if can_delete_event_media(v):
                        # Delete the file
                        path = os.path.join(EVENT_VIDEO_FOLDER, video_filename)
                        if os.path.exists(path):
                            os.remove(path)
                        delete_derivatives('event_videos', video_filename)  # incl. H.264 derivative
                        continue  # Skip adding to updated list
                    else:
                        # No permission - keep the video and show warning
                        flash('You can only delete media you uploaded.', 'warning')
                
                # Keep this video (normalize format)
                updated_videos.extend(normalize_media_list([v]))
            event['videos'] = updated_videos

            # Anyone can add new media
            media_files = request.files.getlist('media')
            for media in media_files:
                if media and media.filename:
                    sniffed = sniff_upload(media)
                    if not sniffed:
                        flash(f'"{media.filename}" was skipped: only photo and video files are allowed.', 'warning')
                        continue
                    kind, ext = sniffed
                    filename = f"{int(time.time())}_{uuid.uuid4().hex[:8]}_{safe_stem(media.filename)}.{ext}"
                    if kind == 'image':
                        saved_path = os.path.join(EVENT_PHOTO_FOLDER, filename)
                        media.save(saved_path)
                        # Auto-convert HEIC images to JPEG (parity with add_event)
                        if _is_heic_image(filename):
                            jpeg_path = convert_heic_to_jpeg(saved_path)
                            if jpeg_path:
                                os.remove(saved_path)
                                filename = os.path.basename(jpeg_path)
                                saved_path = os.path.join(EVENT_PHOTO_FOLDER, filename)
                        prebuild_thumbnails('event_photos', filename)
                        event.setdefault('photos', []).append({
                            'filename': filename,
                            'uploaded_by': session['username'],
                            'user_id': session['user_id']
                        })
                    elif kind == 'video':
                        media.save(os.path.join(EVENT_VIDEO_FOLDER, filename))
                        build_web_video_async(filename)
                        prebuild_thumbnails('event_videos', filename, sizes=('sm',))
                        event.setdefault('videos', []).append({
                            'filename': filename,
                            'uploaded_by': session['username'],
                            'user_id': session['user_id']
                        })

            save_events(events)
            return redirect(f'/event/{event_id}')

        # Calculate permissions for each media item
        photo_permissions = {}
        for photo in event.get('photos', []):
            filename = photo if isinstance(photo, str) else photo.get('filename')
            photo_permissions[filename] = can_delete_event_media(photo)
        
        video_permissions = {}
        for video in event.get('videos', []):
            filename = video if isinstance(video, str) else video.get('filename')
            video_permissions[filename] = can_delete_event_media(video)

        # Calculate permissions for comments
        comment_permissions = {}
        for i, comment in enumerate(event.get('comments', [])):
            # Comment owner can edit, event creator and comment owner can delete
            if comment.get('user_id') == session['user_id']:
                comment_permissions[i] = 'edit'  # Can edit and delete
            elif event.get('user_id') == session['user_id'] or _is_admin():
                comment_permissions[i] = 'delete'  # Event creator / admin can moderate
            else:
                comment_permissions[i] = None  # No permissions

        return render_template('edit_event.html', 
                             event=event, 
                             can_edit_details=can_edit_details,
                             photo_permissions=photo_permissions,
                             video_permissions=video_permissions,
                             comment_permissions=comment_permissions)
    except Exception as e:
        app.logger.error(f"Error editing event {event_id}: {e}")
        return "Internal server error", 500

@app.route('/delete_event', methods=['POST'])
@login_required
def delete_event():
    try:
        event_id = request.form.get('id')
        events = load_events()
        event = next((e for e in events if e['id'] == event_id), None)
        
        if not event:
            flash('Event not found.', 'danger')
            return redirect('/calendar')
        
        if not can_edit_event(event):
            flash('You do not have permission to delete this event.', 'danger')
            return redirect('/calendar')
        
        # Delete associated photo files from disk
        for photo in event.get('photos', []):
            photo_filename = photo if isinstance(photo, str) else photo.get('filename')
            if photo_filename:
                photo_path = os.path.join(EVENT_PHOTO_FOLDER, photo_filename)
                if os.path.exists(photo_path):
                    try:
                        os.remove(photo_path)
                        app.logger.info(f"Deleted photo file: {photo_path}")
                    except Exception as e:
                        app.logger.error(f"Error deleting photo {photo_path}: {e}")
                delete_derivatives('event_photos', photo_filename)
        
        # Delete associated video files from disk
        for video in event.get('videos', []):
            video_filename = video if isinstance(video, str) else video.get('filename')
            if video_filename:
                video_path = os.path.join(EVENT_VIDEO_FOLDER, video_filename)
                if os.path.exists(video_path):
                    try:
                        os.remove(video_path)
                        app.logger.info(f"Deleted video file: {video_path}")
                    except Exception as e:
                        app.logger.error(f"Error deleting video {video_path}: {e}")
                delete_derivatives('event_videos', video_filename)
        
        events = [e for e in events if e['id'] != event_id]
        save_events(events)
        flash('Event and associated media deleted successfully.', 'success')
        return redirect('/calendar')
    except Exception as e:
        app.logger.error(f"Error deleting event {event_id}: {e}")
        return "Internal server error", 500

@app.route('/add_event_comment/<event_id>', methods=['POST'])
@login_required
def add_event_comment(event_id):
    try:
        comment_text = request.form.get('comment')
        if not comment_text or not comment_text.strip():
            flash('Comment cannot be empty.', 'warning')
            return redirect(f'/event/{event_id}')
        
        events = load_events()
        event = next((e for e in events if e['id'] == event_id), None)
        if not event:
            flash('Event not found.', 'danger')
            return redirect('/events')
        
        # Initialize comments array if it doesn't exist
        if 'comments' not in event:
            event['comments'] = []
        
        # Add new comment
        comment = {
            'text': comment_text.strip(),
            'username': session['username'],
            'user_id': session['user_id'],
            'timestamp': datetime.now().isoformat()
        }
        event['comments'].append(comment)
        
        save_events(events)
        flash('Comment added!', 'success')
        return redirect(f'/event/{event_id}')
    except Exception as e:
        app.logger.error(f"Error adding comment to event {event_id}: {e}")
        return "Internal server error", 500

@app.route('/edit_event_comment', methods=['POST'])
@login_required
def edit_event_comment():
    try:
        event_id = request.form.get('event_id')
        comment_index = request.form.get('comment_index', type=int)
        new_text = request.form.get('new_text', '').strip()
        
        if not new_text:
            return "Comment cannot be empty", 400
        
        events = load_events()
        event = next((e for e in events if e['id'] == event_id), None)
        if not event:
            return "Event not found", 404
        
        if comment_index is None or 'comments' not in event or comment_index < 0 or comment_index >= len(event['comments']):
            return "Comment not found", 404
        
        comment = event['comments'][comment_index]
        
        # Only the comment owner can edit
        if comment.get('user_id') != session['user_id']:
            return "Unauthorized", 403
        
        comment['text'] = new_text
        comment['edited_at'] = datetime.now().isoformat()
        
        save_events(events)
        return "Success", 200
    except Exception as e:
        app.logger.error(f"Error editing comment: {e}")
        return "Internal server error", 500

@app.route('/delete_event_comment', methods=['POST'])
@login_required
def delete_event_comment():
    try:
        event_id = request.form.get('event_id')
        comment_index = request.form.get('comment_index', type=int)
        
        events = load_events()
        event = next((e for e in events if e['id'] == event_id), None)
        if not event:
            flash('Event not found.', 'danger')
            return redirect('/events')
        
        if comment_index is None or 'comments' not in event or comment_index < 0 or comment_index >= len(event['comments']):
            flash('Comment not found.', 'danger')
            return redirect(f'/edit_event/{event_id}')
        
        comment = event['comments'][comment_index]
        
        # Comment owner, event creator or an admin can delete
        if comment.get('user_id') != session['user_id'] and event.get('user_id') != session['user_id'] and not _is_admin():
            flash('You do not have permission to delete this comment.', 'danger')
            return redirect(f'/edit_event/{event_id}')
        
        event['comments'].pop(comment_index)
        
        save_events(events)
        flash('Comment deleted.', 'success')
        return redirect(f'/edit_event/{event_id}')
    except Exception as e:
        app.logger.error(f"Error deleting comment: {e}")
        return "Internal server error", 500

@app.route('/admin/process_repeating_events', methods=['POST'])
@admin_required
def admin_process_repeating():
    """Manually trigger repeat_yearly event processing - admin only (also runs on a schedule)"""
    try:
        if run_repeat_processing():
            flash('Repeating events processed: new yearly instances were created.', 'success')
        else:
            flash('No new repeat instances needed.', 'info')
    except Exception as e:
        app.logger.error(f"Error processing repeating events: {e}", exc_info=True)
        flash('Error processing repeating events; see the server log.', 'danger')
    
    return redirect(url_for('admin_settings', tab='media'))

def run_repeat_processing():
    """Create missing yearly instances for repeat_yearly events.
    Safe to call from several processes/threads: the whole read-process-write runs under a lock
    and always reads events.json fresh (never the per-request cache)."""
    with FileLock(EVENTS_FILE + '.repeat.lock'):
        events = _read_json(EVENTS_FILE, [])
        if not isinstance(events, list):
            return False
        events, modified = process_repeat_yearly_events(events)
        if modified:
            save_events(events)
        return modified

def _start_repeat_scheduler(interval_seconds=6 * 3600):
    """Background timer so recurring events appear without anyone pressing a button."""
    if os.environ.get('DISABLE_SCHEDULER', '').lower() in ('1', 'true', 'yes'):
        return
    def _loop():
        time.sleep(30)  # let the server finish starting
        while True:
            try:
                with app.app_context():
                    run_repeat_processing()
            except Exception as e:
                app.logger.error(f"Repeat-event scheduler failed: {e}")
            time.sleep(interval_seconds)
    threading.Thread(target=_loop, daemon=True, name='repeat-scheduler').start()

@app.route('/events')
@login_required
def events_page():
    try:
        events = load_events()
        today = datetime.now()
        extended_events = []

        for e in events:
            try:
                start_dt = datetime.fromisoformat(e['start'].replace('Z', '+00:00'))
            except Exception as ex:
                app.logger.error(f"Skipping event due to bad date: {e.get('start')} - {ex}")
                continue

            e['_start'] = start_dt
            e['is_past'] = start_dt.date() < today.date()
            e['can_edit'] = can_edit_event(e)
            extended_events.append(e)

        extended_events = sorted(extended_events, key=lambda e: e['_start'])
        
        # Normalize media format for display (backward compatibility)
        for event in extended_events:
            if event.get('photos'):
                event['photos'] = normalize_media_list(event['photos'])
            
            if event.get('videos'):
                event['videos'] = normalize_media_list(event['videos'])

        show_past = request.args.get('past', '1') == '1'
        if not show_past:
            extended_events = [e for e in extended_events if not e['is_past']]

        start_month_str = request.args.get('start_month', '')
        start_year_str = request.args.get('start_year', '')
        start_month = int(start_month_str) if start_month_str.isdigit() else None
        start_year = int(start_year_str) if start_year_str.isdigit() else None

        if start_year is not None:
            extended_events = [e for e in extended_events if e['_start'].year >= start_year]

        if start_month is not None and start_year is not None:
            extended_events = [e for e in extended_events if not (e['_start'].year == start_year and e['_start'].month < start_month)]

        per_page = 10
        total_events = len(extended_events)
        total_pages = max((total_events + per_page - 1) // per_page, 1)

        # Default to page that centers on today's date
        if 'page' not in request.args:
            today_dt = today.date()
            # Find the index of the first event on or after today
            today_idx = 0
            for i, e in enumerate(extended_events):
                if e['_start'].date() >= today_dt:
                    today_idx = i
                    break
            else:
                today_idx = len(extended_events)
            # Center it: put today_idx roughly in the middle of the page
            default_page = max(1, (today_idx // per_page) + 1)
            default_page = min(default_page, total_pages)
            page = default_page
        else:
            page = request.args.get('page', type=int) or 1
        page = max(page, 1)
        page = min(page, total_pages)
        start_idx = (page - 1) * per_page
        end_idx = min(start_idx + per_page, total_events)
        paginated_events = extended_events[start_idx:end_idx]

        # Pagination window for smart page numbers
        # Show: first page, ellipsis, window around current, ellipsis, last page
        window_size = 2  # pages on each side of current
        page_nums = [1]
        if 1 + window_size + 1 < page:
            page_nums.append('...')
        start = max(2, page - window_size)
        end = min(total_pages - 1, page + window_size)
        for p in range(start, end + 1):
            page_nums.append(p)
        if page + window_size + 1 < total_pages:
            page_nums.append('...')
        if total_pages > 1:
            page_nums.append(total_pages)

        settings = load_settings()
        page_title = settings.get('all_events_page_header', 'All Events') if show_past else settings.get('events_page_header', 'Events')

        return render_template(
            'events.html', 
            events=paginated_events, 
            show_past=show_past,
            start_month=start_month,
            start_year=start_year,
            page_title=page_title,
            current_page=page,
            total_pages=total_pages,
            total_events=total_events,
            per_page=per_page,
            page_nums=page_nums
        )
    except Exception as e:
        app.logger.error(f"Error rendering events page: {e}")
        return "Internal server error", 500

def load_events(skip_repeat_processing=True):
    # Cache events for the duration of the request to avoid redundant file I/O
    # Default skip_repeat_processing=True so reads are read-only.
    # Repeat processing is handled by the admin-only /admin/process_repeating_events route.
    if hasattr(g, '_events'):
        return g._events
    
    try:
        if not os.path.exists(EVENTS_FILE):
            g._events = []
            return []
        with open(EVENTS_FILE, encoding='utf-8') as f:
            events = json.load(f)
        
        # Process repeat_yearly events (but not immediately after saving to avoid duplicates)
        # Only runs once per request due to g._events caching above
        if not skip_repeat_processing:
            events, modified = process_repeat_yearly_events(events)
            
            # Save if we created new instances
            if modified:
                # Use skip flag to prevent infinite loop
                save_events(events, skip_load=True)
        
        g._events = events
        return events
    except Exception as e:
        app.logger.error(f"Error loading events.json: {e}")
        g._events = []
        return []

def process_repeat_yearly_events(events):
    """
    For events with repeat_yearly=true:
    - Keep ALL past occurrences (with their photos, videos, comments)
    - When an event's date passes, create next year's instance
    - New instances do NOT include photos, videos, or comments
    - Never create duplicates for years that already exist
    """
    now = datetime.now()
    modified = False
    new_events = []
    
    for event in events:
        if not event.get('repeat_yearly'):
            continue
        
        # Skip if this is already a generated instance (has parent_event_id)
        # We only process original events to avoid duplicates
        if event.get('parent_event_id'):
            continue
        
        try:
            # Parse the event's start date
            start_dt = datetime.fromisoformat(event['start'].replace('Z', '+00:00'))
            
            # Find all years that already have an instance of this event (original + generated)
            existing_years = set()
            for e in events:
                # Check if this is the same event (same ID or same parent_event_id)
                if e['id'] == event['id'] or e.get('parent_event_id') == event['id']:
                    e_start = datetime.fromisoformat(e['start'].replace('Z', '+00:00'))
                    existing_years.add(e_start.year)
            
            # Only create instances if the ORIGINAL event date has passed
            if start_dt >= now:
                # Event hasn't happened yet - don't create any instances
                continue
            
            # Event has passed - create instances for ALL missing years
            # from the year after the original up to next year
            original_year = start_dt.year
            next_year = now.year + 1
            
            # Create instances for each missing year
            for year in range(original_year + 1, next_year + 1):
                if year not in existing_years:
                    # Create this year's instance WITHOUT media and comments
                    next_instance = {
                        'id': str(uuid.uuid4()),
                        'parent_event_id': event['id'],
                        'title': event['title'],
                        'notes': event.get('notes', ''),
                        'repeat_yearly': True,
                        'user_id': event.get('user_id'),
                        'username': event.get('username'),
                        'photos': [],
                        'videos': [],
                        'comments': []
                    }
                    
                    # Calculate this year's date (same month/day, different year)
                    # Handle Feb 29 -> Feb 28 in non-leap years
                    try:
                        instance_start = start_dt.replace(year=year)
                    except ValueError:
                        instance_start = start_dt.replace(year=year, day=28)
                    next_instance['start'] = instance_start.isoformat()
                    
                    if event.get('end'):
                        end_dt = datetime.fromisoformat(event['end'].replace('Z', '+00:00'))
                        try:
                            instance_end = end_dt.replace(year=year)
                        except ValueError:
                            instance_end = end_dt.replace(year=year, day=28)
                        next_instance['end'] = instance_end.isoformat()
                    else:
                        next_instance['end'] = ''
                    
                    new_events.append(next_instance)
                    modified = True
                    app.logger.info(f"Created repeat instance for '{event['title']}' in year {year}")
        
        except Exception as e:
            app.logger.error(f"Error processing repeat_yearly for event {event.get('id')}: {e}")
            continue
    
    # Add new instances to events list
    if new_events:
        events.extend(new_events)
    
    return events, modified

def save_events(events, skip_load=False):
    try:
        with FileLock(EVENTS_FILE + '.lock'):
            tmp = EVENTS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(events, f, indent=2, ensure_ascii=False)
            os.replace(tmp, EVENTS_FILE)
    except Exception as e:
        app.logger.error(f"Error saving events: {e}")
        raise

# Settings management
def load_settings():
    # Cache settings for the duration of the request to avoid redundant file I/O
    if hasattr(g, '_settings'):
        return g._settings
    
    default_settings = {
        "site_title": "My Personal Site",
        "background_color": "#d0e8f2",
        "primary_color": "#f7b7cd",
        "secondary_color": "#c2f0e6",
        "text_color": "#333333",
        "card_color": "#ffffff",
        "theme": "default",
        "welcome_message": "Welcome to our special corner of the internet! 💕",
        "footer_text": "Made with ❤️",
        "home_label": "🏠 Home",
        "events_label": "📖 Events",
        "calendar_label": "📅 Calendar",
        "events_page_header": "Events",
        "all_events_page_header": "All Events",
        "calendar_page_header": "Calendar",
        "upload_placeholder": "Write something cute here...",
        "body_font": "handcaps",
        "button_font": "comic-sans",
        "session_lifetime_days": 30,
        "admin_reauth_hours": 4,
        "registration_mode": "closed",        # closed | invite | open
        "registration_invite_code": ""
    }
    
    try:
        if not os.path.exists(SETTINGS_FILE):
            app.logger.info("settings.json not found, creating default")
            save_settings(default_settings)
            g._settings = default_settings
            return default_settings
        
        with open(SETTINGS_FILE, encoding='utf-8') as f:
            loaded_settings = json.load(f)
            # Merge with defaults to ensure all keys exist
            for key, value in default_settings.items():
                if key not in loaded_settings:
                    loaded_settings[key] = value
            g._settings = loaded_settings
            return loaded_settings
    except Exception as e:
        app.logger.error(f"Error loading settings.json: {e}")
        # Return defaults instead of empty dict
        g._settings = default_settings
        return default_settings

def save_settings(settings):
    try:
        with FileLock(SETTINGS_FILE + '.lock'):
            tmp = SETTINGS_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        app.logger.error(f"Error saving settings: {e}")
        raise

_COLOR_RE = re.compile(r'^#[0-9a-fA-F]{6}$')

def _color_or(value, default):
    value = (value or '').strip()
    return value if _COLOR_RE.match(value) else (default if _COLOR_RE.match(default or '') else '#ffffff')

def _font_key_or(value, default):
    value = (value or '').strip().lower()
    allowed = set(FONT_FAMILIES) | {f['name'].lower() for f in get_custom_fonts()}
    return value if value in allowed else default

# Rate limit settings saves and color resets
@app.route('/admin/settings', methods=['GET', 'POST'])
@limiter.limit("30/hour", methods=['POST'])
@admin_fresh_required
def admin_settings():
    if request.method == 'POST':
        settings = load_settings()
        
        # Update visual settings
        settings['site_title'] = request.form.get('site_title', settings.get('site_title', 'My Site'))
        # Colours are interpolated into <style> blocks: accept only #rrggbb
        settings['background_color'] = _color_or(request.form.get('background_color'), settings.get('background_color', '#d0e8f2'))
        settings['primary_color'] = _color_or(request.form.get('primary_color'), settings.get('primary_color', '#f7b7cd'))
        settings['secondary_color'] = _color_or(request.form.get('secondary_color'), settings.get('secondary_color', '#c2f0e6'))
        settings['text_color'] = _color_or(request.form.get('text_color'), settings.get('text_color', '#333333'))
        settings['card_color'] = _color_or(request.form.get('card_color'), settings.get('card_color', '#ffffff'))
        
        # Update font settings (must be a known key or an uploaded font)
        settings['body_font'] = _font_key_or(request.form.get('body_font'), settings.get('body_font', 'handcaps'))
        settings['button_font'] = _font_key_or(request.form.get('button_font'), settings.get('button_font', 'comic-sans'))
        
        # Update text content settings
        settings['welcome_message'] = request.form.get('welcome_message', settings.get('welcome_message', 'Welcome to our special corner of the internet! 💕'))
        settings['footer_text'] = request.form.get('footer_text', settings.get('footer_text', 'Made with ❤️'))
        settings['home_label'] = request.form.get('home_label', settings.get('home_label', '🏠 Home'))
        settings['events_label'] = request.form.get('events_label', settings.get('events_label', '📖 Events'))
        settings['calendar_label'] = request.form.get('calendar_label', settings.get('calendar_label', '📅 Calendar'))
        settings['upload_placeholder'] = request.form.get('upload_placeholder', settings.get('upload_placeholder', 'Write something cute here...'))
        
        # Page headers
        settings['events_page_header'] = request.form.get('events_page_header', settings.get('events_page_header', 'Events'))
        settings['all_events_page_header'] = request.form.get('all_events_page_header', settings.get('all_events_page_header', 'All Events'))
        settings['calendar_page_header'] = request.form.get('calendar_page_header', settings.get('calendar_page_header', 'Calendar'))
        
        # Events page labels
        settings['show_past_label'] = request.form.get('show_past_label', settings.get('show_past_label', 'Show All Events'))
        settings['show_upcoming_label'] = request.form.get('show_upcoming_label', settings.get('show_upcoming_label', 'Show Upcoming Events'))
        settings['today_divider_label'] = request.form.get('today_divider_label', settings.get('today_divider_label', '📅 Today'))
        
        # Session & security settings
        try:
            settings['session_lifetime_days'] = min(int(request.form.get('session_lifetime_days', 30)), 90)
        except (ValueError, TypeError):
            settings['session_lifetime_days'] = 30
        try:
            settings['admin_reauth_hours'] = min(int(request.form.get('admin_reauth_hours', 4)), 24)
        except (ValueError, TypeError):
            settings['admin_reauth_hours'] = 4
        reg_mode = request.form.get('registration_mode', settings.get('registration_mode', 'closed'))
        settings['registration_mode'] = reg_mode if reg_mode in ('closed', 'invite', 'open') else 'closed'
        settings['registration_invite_code'] = request.form.get('registration_invite_code', settings.get('registration_invite_code', '')).strip()[:64]
        
        # Apply preset theme if selected
        theme = request.form.get('theme')
        if theme and theme != 'custom':
            themes = {
                'default': {
                    'background_color': '#d0e8f2',
                    'primary_color': '#f7b7cd',
                    'secondary_color': '#c2f0e6',
                    'text_color': '#333333',
                    'card_color': '#ffffff'
                },
                'dark': {
                    'background_color': '#1a1a2e',
                    'primary_color': '#64b5f6',
                    'secondary_color': '#42a5f5',
                    'text_color': '#ffffff',
                    'card_color': '#2d2d2d'
                },
                'forest': {
                    'background_color': '#2d4a3e',
                    'primary_color': '#8fc93a',
                    'secondary_color': '#4a7c59',
                    'text_color': '#e8f5e9',
                    'card_color': '#3d5a4e'
                },
                'sunset': {
                    'background_color': '#ff6b6b',
                    'primary_color': '#feca57',
                    'secondary_color': '#ee5a6f',
                    'text_color': '#2c3e50',
                    'card_color': '#ffe0e0'
                },
                'ocean': {
                    'background_color': '#00b4d8',
                    'primary_color': '#0077b6',
                    'secondary_color': '#90e0ef',
                    'text_color': '#03045e',
                    'card_color': '#caf0f8'
                },
                'lavender': {
                    'background_color': '#e0b0ff',
                    'primary_color': '#c77dff',
                    'secondary_color': '#9d4edd',
                    'text_color': '#240046',
                    'card_color': '#f0e0ff'
                }
            }
            if theme in themes:
                # Preserve text content and fonts when applying theme
                preserved = {
                    'welcome_message': settings.get('welcome_message'),
                    'footer_text': settings.get('footer_text'),
                    'home_label': settings.get('home_label'),
                    'events_label': settings.get('events_label'),
                    'calendar_label': settings.get('calendar_label'),
                    'upload_placeholder': settings.get('upload_placeholder'),
                    'body_font': settings.get('body_font'),
                    'button_font': settings.get('button_font'),
                    'custom_fonts': settings.get('custom_fonts', [])
                }
                settings.update(themes[theme])
                settings.update(preserved)
                settings['theme'] = theme
        else:
            settings['theme'] = 'custom'
        
        save_settings(settings)
        flash('Settings saved successfully!', 'success')
        
        # Get the active tab to redirect back to the same tab
        active_tab = request.form.get('active_tab', 'appearance')
        return redirect(url_for('admin_settings', tab=active_tab))
    
    settings = load_settings()
    # Get list of custom fonts
    custom_fonts = get_custom_fonts()
    return render_template('admin_settings.html', settings=settings, site_settings=settings, custom_fonts=custom_fonts)

@app.route('/admin/save_preset', methods=['POST'])
@admin_fresh_required
def save_color_preset():
    """Save current colors as a custom preset"""
    try:
        data = request.get_json()
        preset_name = data.get('name', '').strip()
        
        if not preset_name:
            return jsonify({'success': False, 'error': 'Preset name is required'})
        
        settings = load_settings()
        presets = settings.get('custom_presets', [])
        
        # Check if preset with same name exists
        presets = [p for p in presets if p['name'] != preset_name]
        
        # Add new preset (validated colours; name length capped)
        presets.append({
            'name': preset_name[:40],
            'background_color': _color_or(data.get('background_color'), '#d0e8f2'),
            'primary_color': _color_or(data.get('primary_color'), '#f7b7cd'),
            'secondary_color': _color_or(data.get('secondary_color'), '#c2f0e6'),
            'text_color': _color_or(data.get('text_color'), '#333333'),
            'card_color': _color_or(data.get('card_color'), '#ffffff')
        })
        
        settings['custom_presets'] = presets
        save_settings(settings)
        
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Error saving preset: {e}")
        return jsonify({'success': False, 'error': 'Request failed; see the server log.'})

@app.route('/admin/get_preset/<preset_name>')
@admin_required
def get_color_preset(preset_name):
    """Get a preset's colors"""
    try:
        settings = load_settings()
        presets = settings.get('custom_presets', [])
        
        preset = next((p for p in presets if p['name'] == preset_name), None)
        
        if not preset:
            return jsonify({'success': False, 'error': 'Preset not found'})
        
        return jsonify({'success': True, 'preset': preset})
    except Exception as e:
        app.logger.error(f"Error getting preset: {e}")
        return jsonify({'success': False, 'error': 'Request failed; see the server log.'})

@app.route('/admin/delete_preset/<preset_name>', methods=['POST'])
@admin_fresh_required
def delete_color_preset(preset_name):
    """Delete a custom preset"""
    try:
        settings = load_settings()
        presets = settings.get('custom_presets', [])
        
        presets = [p for p in presets if p['name'] != preset_name]
        settings['custom_presets'] = presets
        save_settings(settings)
        
        return jsonify({'success': True})
    except Exception as e:
        app.logger.error(f"Error deleting preset: {e}")
        return jsonify({'success': False, 'error': 'Request failed; see the server log.'})

def get_custom_fonts():
    """Get list of custom uploaded fonts"""
    font_dir = os.path.join('static', 'fonts')
    custom_fonts = []
    if os.path.exists(font_dir):
        for f in os.listdir(font_dir):
            if f.lower().endswith(('.ttf', '.otf', '.woff', '.woff2')) and f != 'handcaps.otf':
                font_name = os.path.splitext(f)[0]
                custom_fonts.append({'filename': f, 'name': font_name})
    return custom_fonts

@app.route('/admin/upload_font', methods=['POST'])
@admin_fresh_required
def upload_font():
    """Upload a new custom font"""
    if 'font_file' not in request.files:
        flash('No font file provided', 'danger')
        return redirect(url_for('admin_settings', tab='fonts'))
    
    font_file = request.files['font_file']
    if font_file.filename == '':
        flash('No font file selected', 'danger')
        return redirect(url_for('admin_settings', tab='fonts'))
    
    # Validate file extension
    allowed_extensions = {'.ttf', '.otf', '.woff', '.woff2'}
    ext = os.path.splitext(font_file.filename)[1].lower()
    if ext not in allowed_extensions:
        flash('Invalid font format. Please upload TTF, OTF, WOFF, or WOFF2 files.', 'danger')
        return redirect(url_for('admin_settings', tab='fonts'))

    # Verify the file really is a font (magic bytes; the client's Content-Type is meaningless)
    head = font_file.stream.read(4)
    font_file.stream.seek(0)
    if head not in (b'\x00\x01\x00\x00', b'true', b'OTTO', b'wOFF', b'wOF2', b'ttcf'):
        flash('File content does not appear to be a valid font file.', 'danger')
        return redirect(url_for('admin_settings', tab='fonts'))
    
    # Save font file (never over the bundled default or the generated stylesheet)
    filename = secure_filename(font_file.filename)
    if not filename or filename.lower() in ('handcaps.otf', 'fonts.css'):
        flash('That font name is reserved.', 'danger')
        return redirect(url_for('admin_settings', tab='fonts'))
    font_path = os.path.join('static', 'fonts', filename)
    font_file.save(font_path)
    
    # Update fonts.css to include the new font
    update_fonts_css()
    
    flash(f'Font "{os.path.splitext(filename)[0]}" uploaded successfully!', 'success')
    return redirect(url_for('admin_settings', tab='fonts'))

@app.route('/admin/delete_font/<filename>', methods=['POST'])
@admin_fresh_required
def delete_font(filename):
    """Delete a custom font"""
    filename = secure_filename(filename)  # normalise first: '_handcaps.otf' → 'handcaps.otf'
    # Don't allow deleting the default handcaps font (or anything that isn't a font file)
    if not filename or filename.lower() == 'handcaps.otf' or not filename.lower().endswith(('.ttf', '.otf', '.woff', '.woff2')):
        flash('Cannot delete the default Handcaps font.', 'danger')
        return redirect(url_for('admin_settings', tab='fonts'))
    
    font_path = os.path.join('static', 'fonts', filename)
    if os.path.exists(font_path):
        os.remove(font_path)
        
        # Reset any settings using this font to default
        settings = load_settings()
        font_name = os.path.splitext(filename)[0].lower()
        if settings.get('body_font') == font_name:
            settings['body_font'] = 'comic-sans'
        if settings.get('button_font') == font_name:
            settings['button_font'] = 'comic-sans'
        save_settings(settings)
        
        # Update fonts.css
        update_fonts_css()
        
        flash(f'Font deleted successfully!', 'success')
    else:
        flash('Font file not found.', 'danger')
    
    return redirect(url_for('admin_settings', tab='fonts'))

def update_fonts_css():
    """Regenerate fonts.css with all available fonts"""
    font_dir = os.path.join('static', 'fonts')
    css_content = """/* ============================================
   OFFLINE FONT COLLECTION
   Cute & Playful Fonts for Personal Site
   ============================================ */

"""
    
    # Add each font file
    for f in os.listdir(font_dir):
        if f.lower().endswith(('.ttf', '.otf', '.woff', '.woff2')):
            font_name = os.path.splitext(f)[0]
            ext = os.path.splitext(f)[1].lower()
            
            # Determine format
            format_map = {'.ttf': 'truetype', '.otf': 'opentype', '.woff': 'woff', '.woff2': 'woff2'}
            font_format = format_map.get(ext, 'truetype')
            
            css_content += f"""/* {font_name} font */
@font-face {{
  font-family: '{font_name}';
  font-style: normal;
  font-weight: 100 900;
  font-display: swap;
  src: local('{font_name}'),
       url('/static/fonts/{f}') format('{font_format}');
}}

"""
    
    # Add font stack documentation
    css_content += """/* 
   FONT STACKS (system fonts work offline):
   
   comic-sans:         'Comic Sans MS', 'Chalkboard SE', cursive
   chalkboard:         'Chalkboard SE', 'Comic Sans MS', cursive
   bradley-hand:       'Bradley Hand', 'Brush Script MT', cursive
   marker-felt:        'Marker Felt', 'Comic Sans MS', cursive
   papyrus:            'Papyrus', fantasy
   brush-script:       'Brush Script MT', cursive
   lucida-handwriting: 'Lucida Handwriting', 'Lucida Calligraphy', cursive
   system:             -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif
*/
"""
    
    with open(os.path.join(font_dir, 'fonts.css'), 'w') as f:
        f.write(css_content)

@app.route('/admin/reset_colors', methods=['POST'])
@limiter.limit("5/hour")
@admin_fresh_required
def reset_colors():
    """Reset colors and fonts to safe defaults"""
    settings = load_settings()
    
    # Default color and font values
    default_values = {
        'background_color': '#d0e8f2',
        'primary_color': '#f7b7cd',
        'secondary_color': '#c2f0e6',
        'text_color': '#333333',
        'card_color': '#ffffff',
        'theme': 'default',
        'body_font': 'arial',
        'button_font': 'arial'
    }
    
    settings.update(default_values)
    save_settings(settings)
    flash('Colors and fonts reset to safe defaults!', 'success')
    return redirect(url_for('admin_settings'))

@app.context_processor
def inject_static_url():
    """static_url('css/base.css') → /static/css/base.css?v=<mtime> so browsers pick up new versions
    automatically (the templates used to carry hand-edited ?v=NN numbers)."""
    def static_url(filename):
        try:
            version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
        except OSError:
            version = 0
        return url_for('static', filename=filename, v=version)
    return {'static_url': static_url}

@app.context_processor
def inject_settings():
    """Make settings and the resolved current user available to all templates"""
    return {'site_settings': load_settings(), 'current_user': g.get('user')}

# Font family mapping for templates
FONT_FAMILIES = {
    'comic-sans': "'Comic Sans MS', 'Comic Sans', cursive",
    'arial': "Arial, Helvetica, sans-serif",
    'georgia': "Georgia, 'Times New Roman', serif",
    'times': "'Times New Roman', Times, serif",
    'verdana': "Verdana, Geneva, sans-serif",
    'trebuchet': "'Trebuchet MS', Helvetica, sans-serif",
    'courier': "'Courier New', Courier, monospace",
    'impact': "Impact, Haettenschweiler, sans-serif",
    'palatino': "'Palatino Linotype', 'Book Antiqua', Palatino, serif",
    'tahoma': "Tahoma, Geneva, sans-serif",
    'system': "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    'handcaps': "'Handcaps', 'Comic Sans MS', cursive"
}

@app.template_filter('get_font_family')
def get_font_family(font_key):
    """Convert font key to CSS font-family value"""
    if font_key in FONT_FAMILIES:
        return FONT_FAMILIES[font_key]
    # Check if it's a custom font
    font_dir = os.path.join('static', 'fonts')
    if os.path.exists(font_dir):
        for f in os.listdir(font_dir):
            if f.lower().endswith(('.ttf', '.otf', '.woff', '.woff2')):
                font_name = os.path.splitext(f)[0]
                if font_name.lower() == font_key.lower():
                    return f"'{font_name}', 'Comic Sans MS', cursive"
    return FONT_FAMILIES['comic-sans']

# ============================================
# IMPORT / EXPORT
# ============================================

@app.route('/admin/export/start', methods=['POST'])
@admin_required
def export_data_start():
    """Start an export job in the background. Returns job_id for progress polling."""
    import uuid
    import threading

    job_id = str(uuid.uuid4())[:8]
    _all_jobs['export'][job_id] = {
        'status': 'running',
        'progress': 0,
        'total_files': 0,
        'processed_files': 0,
        'current_file': '',
        'zip_path': None,
        'error': None,
        'done': False,
    }
    _save_jobs()
    threading.Thread(target=_export_worker, args=(job_id,), daemon=True).start()
    return jsonify({'job_id': job_id, 'status': 'started'})


@app.route('/admin/export/progress/<job_id>')
@admin_required
def export_progress(job_id):
    """Poll export job progress."""
    job = _get_job('export', job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({
        'status': job['status'],
        'progress': job['progress'],
        'total_files': job.get('total_files', 0),
        'processed_files': job.get('processed_files', 0),
        'current_file': job.get('current_file', ''),
        'done': job.get('done', False),
        'error': job.get('error'),
    })


def _export_worker(job_id):
    """Background worker that builds the export ZIP with progress tracking."""
    import zipfile
    import shutil

    with app.app_context():
        job = _all_jobs['export'][job_id]
        try:
            # Phase 1: Collect all files and count them
            temp_dir = os.path.join(IMPORT_TEMP_DIR, f'export_{job_id}')
            os.makedirs(temp_dir, exist_ok=True)

            # Build JSON data
            export_data = _build_export_data()

            # Write data.json to temp
            data_json_path = os.path.join(temp_dir, 'data.json')
            with open(data_json_path, 'w') as f:
                json.dump(export_data, f, indent=2)

            # Collect media files
            media_dirs = {
                'static/event_photos': 'media/event_photos',
                'static/event_videos': 'media/event_videos',
                'static/event_videos_web': 'media/event_videos_web',
                'static/uploads': 'media/uploads',
                'static/mobile_fullscreen': 'media/mobile_fullscreen',
            }

            all_files = []
            for local_dir, zip_dir in media_dirs.items():
                if os.path.exists(local_dir):
                    for root, dirs, files in os.walk(local_dir):
                        for filename in files:
                            local_path = os.path.join(root, filename)
                            arc_path = os.path.join(zip_dir, os.path.relpath(local_path, local_dir))
                            all_files.append((local_path, arc_path))

            total = len(all_files) + 1  # +1 for data.json
            job['total_files'] = total
            job['processed_files'] = 0
            job['progress'] = 0
            job['current_file'] = 'Preparing export...'
            _save_jobs()

            # Phase 2: Build ZIP file
            zip_path = os.path.join(temp_dir, f'export_{job_id}.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                # Add data.json first
                zf.write(data_json_path, 'data.json')
                job['processed_files'] = 1
                job['progress'] = round(100 * job['processed_files'] / total, 1)
                job['current_file'] = 'data.json'
                _save_jobs()

                # Add media files
                for local_path, arc_path in all_files:
                    try:
                        zf.write(local_path, arc_path)
                    except Exception as e:
                        app.logger.error(f"Export: failed to add {local_path}: {e}")
                    job['processed_files'] += 1
                    job['progress'] = round(100 * job['processed_files'] / total, 1)
                    job['current_file'] = os.path.basename(local_path)
                    _save_jobs()

            # Mark done
            job['status'] = 'done'
            job['done'] = True
            job['_completed_at'] = time.time()
            job['zip_path'] = zip_path
            job['current_file'] = 'Export complete!'
            job['progress'] = 100
            _save_jobs()

        except Exception as e:
            app.logger.error(f"Export worker failed for {job_id}: {e}")
            job['status'] = 'error'
            job['error'] = 'Export failed; see server log.'
            job['done'] = True
            job['_completed_at'] = time.time()
            _save_jobs()


@app.route('/admin/export/download/<job_id>')
@admin_required
def export_download(job_id):
    """Download the completed export ZIP file, then clean up."""
    import shutil
    from flask import send_file, after_this_request
    
    job = _get_job('export', job_id)
    if not job or not job.get('zip_path'):
        flash('Export file not found.', 'danger')
        return redirect(url_for('admin_settings', tab='import_export'))
    
    zip_path = job['zip_path']
    if not os.path.exists(zip_path):
        flash('Export file no longer available.', 'danger')
        return redirect(url_for('admin_settings', tab='import_export'))
    
    # Clean up after sending the file
    @after_this_request
    def cleanup_export_zip(response):
        try:
            temp_dir = os.path.dirname(zip_path)
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
            _delete_job('export', job_id)
        except Exception as e:
            app.logger.error(f"Cleanup failed: {e}")
        return response
    
    # (direct_passthrough is not a send_file() argument in Flask 3 — it used to raise TypeError here)
    return send_file(
        zip_path,
        mimetype='application/zip',
        as_attachment=True,
        download_name='site_export.zip',
    )


def _build_export_data():
    """Everything except media files and secrets (shared by the ZIP export and /admin/export/json)."""
    settings = dict(load_settings())
    settings.pop('registration_invite_code', None)
    return {
        'version': 1,
        'exported_at': datetime.now().isoformat(),
        'events': load_events(),
        'upload_metadata': load_upload_metadata(),
        'layout': _read_json(LAYOUT_FILE, {}),
        'settings': settings,
    }

@app.route('/admin/export')
@admin_required
def export_data():
    """Legacy URL. The old handler built the whole ZIP (all videos) in memory; exports now run
    as a background job from the Import/Export tab."""
    flash('Use "Start Export": the ZIP is built in the background and offered for download.', 'info')
    return redirect(url_for('admin_settings', tab='import_export'))

@app.route('/admin/export/json')
@admin_required
def export_json():
    """Data-only export (no media) — the format accepted by "Pull from URL" import."""
    return jsonify(_build_export_data())


# ============================================
# IMPORT (background job; shared by the JS flow and the no-JS form fallback)
# ============================================

_IMPORT_MEDIA_MAP = {
    'media/event_photos': EVENT_PHOTO_FOLDER,
    'media/event_videos': EVENT_VIDEO_FOLDER,
    'media/event_videos_web': EVENT_VIDEO_WEB_FOLDER,
    'media/uploads': UPLOAD_FOLDER,
}
_IMPORT_MAX_RATIO = 200                    # zip-bomb guard (uncompressed / compressed)
_IMPORT_URL_MAX_BYTES = 200 * 1024 * 1024  # cap for "Pull from URL"
# Never imported: they control who can log in / how long sessions last on *this* instance
_PROTECTED_SETTINGS = {'registration_mode', 'registration_invite_code', 'session_lifetime_days',
                       'admin_reauth_hours', 'transcode_keep_originals'}
_VISUAL_SETTINGS = {'background_color', 'primary_color', 'secondary_color', 'text_color', 'card_color',
                    'theme', 'body_font', 'button_font', 'custom_presets'}


def _remap_import_user(obj, id_key, name_key):
    """Try to map an imported user_id to a local user by username."""
    if not isinstance(obj, dict):
        return obj
    username = obj.get(name_key)
    if not username or not isinstance(username, str):
        return obj
    local_user = get_user_by_username(username)
    if local_user:
        obj[id_key] = local_user['id']
    return obj


def _fetch_remote_json(url, max_bytes=_IMPORT_URL_MAX_BYTES):
    """Fetch JSON over HTTPS with SSRF guards: every resolved address must be public, redirects
    are not followed, and the body is size-capped."""
    import socket, ipaddress, urllib.request
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != 'https':
        raise ValueError('Import URL must use HTTPS.')
    host = parsed.hostname or ''
    if not host or host.lower() in ('localhost',) or host.endswith('.local') or host.endswith('.internal'):
        raise ValueError('Import URL points to a restricted address.')
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError('Could not resolve the import host.')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError('Import URL points to a restricted address.')

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'personal-site-import'})
    with opener.open(req, timeout=30) as resp:
        if resp.status != 200:
            raise ValueError(f'Remote server returned HTTP {resp.status}.')
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError('Remote export is too large.')
    return json.loads(data)


def _validate_import_events(raw):
    """Keep only well-formed events (id/title/start present, media names plain). Returns (events, skipped)."""
    good, skipped = [], 0
    if not isinstance(raw, list):
        return good, skipped
    for ev in raw:
        if not isinstance(ev, dict):
            skipped += 1
            continue
        eid, title, start = ev.get('id'), ev.get('title'), ev.get('start')
        if (not isinstance(eid, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', eid)
                or not isinstance(title, str) or not title.strip() or not isinstance(start, str)):
            skipped += 1
            continue
        try:
            datetime.fromisoformat(start.replace('Z', '+00:00'))
        except ValueError:
            skipped += 1
            continue
        ev = dict(ev)
        ev['title'] = title.strip()[:200]
        ev['end'] = ev['end'] if isinstance(ev.get('end'), str) else ''
        ev['notes'] = ev['notes'] if isinstance(ev.get('notes'), str) else ''
        ev['repeat_yearly'] = bool(ev.get('repeat_yearly'))
        for key in ('photos', 'videos'):
            cleaned = []
            for item in (ev.get(key) or []) if isinstance(ev.get(key), list) else []:
                fn = item if isinstance(item, str) else (item.get('filename') if isinstance(item, dict) else None)
                if isinstance(fn, str) and fn and secure_filename(fn) == fn:
                    cleaned.append(item)
            ev[key] = cleaned
        ev['comments'] = [c for c in (ev.get('comments') or []) if isinstance(c, dict) and isinstance(c.get('text'), str)] \
            if isinstance(ev.get('comments'), list) else []
        good.append(ev)
    return good, skipped


def _safe_extract_zip(zip_path, dest):
    """Extract regular files only, with path-traversal, zip-bomb and free-space guards."""
    import zipfile
    dest_real = os.path.realpath(dest)
    with zipfile.ZipFile(zip_path) as zf:
        members = [i for i in zf.infolist() if not i.is_dir()]
        total = 0
        for info in members:
            target = os.path.realpath(os.path.join(dest, info.filename))
            if not target.startswith(dest_real + os.sep):
                raise ValueError(f'entry "{info.filename}" attempts path traversal')
            if info.file_size > 1024 * 1024 and info.file_size / max(info.compress_size, 1) > _IMPORT_MAX_RATIO:
                raise ValueError(f'entry "{info.filename}" has a suspicious compression ratio')
            total += info.file_size
        if not has_free_space(dest, total):
            raise ValueError('not enough free disk space to extract this archive')
        for info in members:
            zf.extract(info, dest)


def _start_import_job(mode, strategy, import_events, import_uploads, import_settings, uploaded_path, source_url):
    job_id = str(uuid.uuid4())
    _all_jobs['import'][job_id] = {'progress': 0, 'status': 'Starting...', 'done': False, 'error': None, 'stats': {}}
    _save_jobs()
    threading.Thread(target=_run_import_job, daemon=True, args=(
        job_id, mode, strategy, import_events, import_uploads, import_settings, uploaded_path, source_url)).start()
    return job_id


def _run_import_job(job_id, mode, strategy, import_events, import_uploads, import_settings, uploaded_path, source_url):
    import shutil, zipfile
    with app.app_context():
        job = _all_jobs['import'][job_id]
        temp_dir = None

        def fail(msg):
            job['error'] = msg
            _mark_job_done('import', job_id)
            _cleanup_completed_jobs()

        try:
            source = None
            if mode == 'file':
                if not uploaded_path or not os.path.exists(uploaded_path):
                    return fail('No file uploaded.')
                job['status'] = 'Reading file...'
                job['progress'] = 5
                if zipfile.is_zipfile(uploaded_path):
                    job['status'] = 'Extracting ZIP...'
                    job['progress'] = 10
                    temp_dir = os.path.join(IMPORT_TEMP_DIR, job_id)
                    os.makedirs(temp_dir, exist_ok=True)
                    try:
                        _safe_extract_zip(uploaded_path, temp_dir)
                    except (ValueError, zipfile.BadZipFile) as e:
                        return fail(f'Invalid ZIP: {e}')
                    data_path = os.path.join(temp_dir, 'data.json')
                    if not os.path.exists(data_path):
                        return fail('Invalid export ZIP: missing data.json')
                    try:
                        with open(data_path, encoding='utf-8') as df:
                            source = json.load(df)
                    except (ValueError, UnicodeDecodeError):
                        return fail('Invalid export ZIP: data.json is not valid JSON')
                else:
                    try:
                        with open(uploaded_path, encoding='utf-8') as f:
                            source = json.load(f)
                    except (ValueError, UnicodeDecodeError):
                        return fail('Invalid JSON file.')
            elif mode == 'url':
                if not source_url:
                    return fail('No URL provided.')
                job['status'] = 'Fetching from URL...'
                job['progress'] = 10
                try:
                    source = _fetch_remote_json(source_url)
                except Exception as e:
                    return fail(f'Failed to fetch from URL: {e}')
            else:
                return fail('Invalid import mode.')

            if not isinstance(source, dict) or not source:
                return fail('No data to import.')

            stats = {'events_added': 0, 'events_skipped': 0, 'events_invalid': 0,
                     'uploads_added': 0, 'uploads_skipped': 0,
                     'settings_updated': False, 'media_files': 0}

            # --- Events ---
            if import_events and 'events' in source:
                job['status'] = 'Importing events...'
                job['progress'] = 20
                incoming, stats['events_invalid'] = _validate_import_events(source.get('events'))
                existing = load_events() if strategy == 'merge' else []
                existing_ids = {e.get('id') for e in existing if isinstance(e, dict)}
                for i, ev in enumerate(incoming):
                    if ev['id'] in existing_ids:
                        stats['events_skipped'] += 1
                        continue
                    _remap_import_user(ev, 'user_id', 'username')
                    for item in ev['photos'] + ev['videos']:
                        _remap_import_user(item, 'user_id', 'username')
                    existing.append(ev)
                    existing_ids.add(ev['id'])
                    stats['events_added'] += 1
                    job['progress'] = 20 + int(30 * (i + 1) / max(len(incoming), 1))
                save_events(existing)

            # --- Upload metadata + layout ---
            if import_uploads:
                job['status'] = 'Importing uploads...'
                job['progress'] = 55
                with FileLock(LAYOUT_FILE + '.lock'):
                    existing_meta = load_upload_metadata() if strategy == 'merge' else {}
                    existing_layout = _read_json(LAYOUT_FILE, {}) if strategy == 'merge' else {}
                    if not isinstance(existing_layout, dict):
                        existing_layout = {}
                    new_meta, new_layout = dict(existing_meta), dict(existing_layout)
                    max_order = max((v.get('order', 0) for v in new_layout.values() if isinstance(v, dict)), default=-1)
                    raw_meta = source.get('upload_metadata') if isinstance(source.get('upload_metadata'), dict) else {}
                    items = list(raw_meta.items())
                    for i, (fname, meta) in enumerate(items):
                        if not isinstance(fname, str) or secure_filename(fname) != fname or not isinstance(meta, dict):
                            stats['uploads_skipped'] += 1
                            continue
                        if fname in new_meta:
                            stats['uploads_skipped'] += 1
                            continue
                        new_meta[fname] = _remap_import_user(dict(meta), 'user_id', 'username')
                        max_order += 1
                        new_layout[fname] = {'order': max_order}
                        stats['uploads_added'] += 1
                        job['progress'] = 55 + int(10 * (i + 1) / max(len(items), 1))
                    save_upload_metadata(new_meta)
                    _write_json_atomic(LAYOUT_FILE, new_layout, locked=True)

            # --- Media files from the ZIP ---
            if temp_dir and import_uploads:
                job['status'] = 'Copying media files...'
                job['progress'] = 70
                pending = []
                for zip_dir, local_dir in _IMPORT_MEDIA_MAP.items():
                    src_dir = os.path.join(temp_dir, zip_dir)
                    if os.path.isdir(src_dir):
                        for fname in os.listdir(src_dir):
                            src_file = os.path.join(src_dir, fname)
                            if os.path.isfile(src_file) and secure_filename(fname) == fname:
                                pending.append((src_file, os.path.join(local_dir, fname)))
                for i, (src_file, dst_file) in enumerate(pending):
                    os.makedirs(os.path.dirname(dst_file), exist_ok=True)
                    if not os.path.exists(dst_file):
                        shutil.move(src_file, dst_file)  # no second copy on disk
                        stats['media_files'] += 1
                    job['progress'] = 70 + int(20 * (i + 1) / max(len(pending), 1))

            # --- Settings (visual + security keys are never overwritten) ---
            if import_settings and isinstance(source.get('settings'), dict):
                job['status'] = 'Importing settings...'
                job['progress'] = 95
                current = load_settings()
                skip = _PROTECTED_SETTINGS | (_VISUAL_SETTINGS if strategy == 'merge' else set())
                for k, v in source['settings'].items():
                    if isinstance(k, str) and k not in skip:
                        current[k] = v
                save_settings(current)
                stats['settings_updated'] = True

            msg_parts = []
            if import_events:
                part = f"Events: +{stats['events_added']}, skipped {stats['events_skipped']} duplicates"
                if stats['events_invalid']:
                    part += f", ignored {stats['events_invalid']} malformed"
                msg_parts.append(part)
            if import_uploads:
                msg_parts.append(f"Uploads: +{stats['uploads_added']}, skipped {stats['uploads_skipped']}")
                if stats['media_files']:
                    msg_parts.append(f"Media: +{stats['media_files']} files")
            if import_settings:
                msg_parts.append("Settings updated")
            job['progress'] = 100
            job['status'] = 'Import complete — ' + '; '.join(msg_parts)
            job['stats'] = stats
            _mark_job_done('import', job_id)
            _cleanup_completed_jobs()
        except Exception as e:
            app.logger.error(f"Import job {job_id} failed: {e}", exc_info=True)
            fail('Import failed; see the server log for details.')
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)
            if uploaded_path and os.path.exists(uploaded_path):
                try:
                    os.remove(uploaded_path)
                except OSError:
                    pass


def _import_request_to_job():
    """Parse the import form (shared by the JS and no-JS flows) and start the background job.
    The uploaded file is streamed to disk under DATA_DIR/tmp_import — never held in memory."""
    mode = request.form.get('mode', 'file')
    strategy = 'replace' if request.form.get('strategy') == 'replace' else 'merge'
    import_events = request.form.get('import_events') == 'on'
    import_uploads = request.form.get('import_uploads') == 'on'
    import_settings = request.form.get('import_settings') == 'on'
    uploaded_path = None
    source_url = None
    if mode == 'file':
        f = request.files.get('source_file')
        if f and f.filename:
            if not has_free_space(IMPORT_TEMP_DIR, request.content_length):
                return None, 'Not enough free disk space on the server for this import.'
            os.makedirs(IMPORT_TEMP_DIR, exist_ok=True)
            uploaded_path = os.path.join(IMPORT_TEMP_DIR, f'upload_{uuid.uuid4().hex}')
            f.save(uploaded_path)
    elif mode == 'url':
        source_url = request.form.get('source_url', '').strip()
    job_id = _start_import_job(mode, strategy, import_events, import_uploads, import_settings, uploaded_path, source_url)
    return job_id, None


@app.route('/admin/import', methods=['POST'])
@admin_fresh_required
def import_data():
    """No-JavaScript fallback for the import form: starts the same background job."""
    job_id, err = _import_request_to_job()
    if err:
        flash(err, 'danger')
    else:
        flash('Import started in the background. Reload the Import/Export tab in a moment to see the result.', 'info')
    return redirect(url_for('admin_settings', tab='import_export'))


@app.route('/admin/import/start', methods=['POST'])
@admin_fresh_required
def import_start():
    """Start an import job in the background and return a job_id for polling."""
    job_id, err = _import_request_to_job()
    if err:
        return jsonify({'error': err}), 507
    return jsonify({'job_id': job_id})


@app.route('/admin/import/progress/<job_id>')
@admin_required
def import_progress(job_id):
    """Poll import job progress."""
    job = _get_job('import', job_id)
    if not job:
        return jsonify({'error': 'Job not found — may have been cleared'}), 404
    return jsonify(job)


# ============================================
# MEDIA TRANSCODING MANAGEMENT
# ============================================

# Persistent job tracking (survives restarts).
# jobs.json is the source of truth: gunicorn runs several worker processes, and the worker that
# answers a progress poll is usually not the one running the job, so every read falls back to
# disk and every save merges this process's jobs into the file instead of overwriting it.
_JOBS_FILE = os.path.join(DATA_DIR, 'jobs.json')
_JOB_TYPES = ('transcode', 'import', 'export')

def _read_jobs_file():
    try:
        with open(_JOBS_FILE, 'r', encoding='utf-8') as f:
            jobs = json.load(f)
        if not isinstance(jobs, dict):
            jobs = {}
    except (FileNotFoundError, json.JSONDecodeError):
        jobs = {}
    for jt in _JOB_TYPES:
        jobs.setdefault(jt, {})
    return jobs

def _write_jobs_file(jobs):
    tmp = _JOBS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(jobs, f, indent=2)
    os.replace(tmp, _JOBS_FILE)

def _load_jobs():
    """Load jobs from disk."""
    return _read_jobs_file()

def _snapshot_jobs():
    # Shallow-copy each job so a worker thread mutating it mid-dump can't break json.dump
    return {jt: {jid: dict(job) for jid, job in list(_all_jobs[jt].items())} for jt in _JOB_TYPES}

def _save_jobs(max_age_hours=24):
    """Merge this process's jobs into jobs.json (never clobber other workers' jobs)."""
    try:
        snapshot = _snapshot_jobs()
        cutoff = time.time() - (max_age_hours * 3600)
        with FileLock(_JOBS_FILE + '.lock'):
            disk = _read_jobs_file()
            for jt in _JOB_TYPES:
                disk[jt].update(snapshot[jt])
                for jid, job in list(disk[jt].items()):
                    # prune only jobs that carry a completion timestamp older than the cutoff
                    if job.get('done') and job.get('_completed_at') and job['_completed_at'] < cutoff:
                        disk[jt].pop(jid, None)
            _write_jobs_file(disk)
    except Exception as e:
        app.logger.error(f"Failed to save jobs: {e}")

def _get_job(job_type, job_id):
    """Job state for polling: this process's live object if we own the job, else the on-disk copy."""
    job = _all_jobs[job_type].get(job_id)
    if job is None:
        job = _read_jobs_file().get(job_type, {}).get(job_id)
    return job

def _delete_job(job_type, job_id):
    _all_jobs[job_type].pop(job_id, None)
    try:
        with FileLock(_JOBS_FILE + '.lock'):
            disk = _read_jobs_file()
            disk[job_type].pop(job_id, None)
            _write_jobs_file(disk)
    except Exception as e:
        app.logger.error(f"Failed to delete job {job_id}: {e}")

_all_jobs = _load_jobs()

def _cleanup_completed_jobs(max_age_hours=24):
    """Remove job entries that have been completed for longer than max_age_hours."""
    import time
    cutoff = time.time() - (max_age_hours * 3600)
    for job_type in ('export', 'import', 'transcode'):
        for job_id, job in list(_all_jobs[job_type].items()):
            if job.get('done') and job.get('_completed_at', 0) < cutoff:
                _all_jobs[job_type].pop(job_id, None)
    _save_jobs(max_age_hours)

def _mark_job_done(job_type, job_id):
    """Mark a job as done with a timestamp for later cleanup."""
    import time
    job = _all_jobs[job_type].get(job_id)
    if job:
        job['done'] = True
        job['_completed_at'] = time.time()
        _save_jobs()

def _cleanup_stale_jobs():
    """Remove temp dirs and job entries for completed/failed jobs on startup."""
    import shutil
    for job_type in ('export', 'import', 'transcode'):
        for job_id, job in list(_all_jobs[job_type].items()):
            if job.get('done') or job.get('status') == 'error':
                # Clean up temp dir if it exists
                if job_type == 'export' and job.get('zip_path'):
                    temp_dir = os.path.dirname(job['zip_path'])
                    if os.path.exists(temp_dir):
                        shutil.rmtree(temp_dir, ignore_errors=True)
                if job_type in ('import', 'transcode'):
                    # Clean up any temp dirs associated with the job
                    for temp_root in [IMPORT_TEMP_DIR]:
                        if os.path.exists(temp_root):
                            for entry in os.listdir(temp_root):
                                entry_path = os.path.join(temp_root, entry)
                                if os.path.isdir(entry_path) and job_id in entry:
                                    shutil.rmtree(entry_path, ignore_errors=True)
                _all_jobs[job_type].pop(job_id, None)
    # Startup: the in-memory view (loaded from disk moments ago, minus stale entries) replaces the file
    try:
        with FileLock(_JOBS_FILE + '.lock'):
            _write_jobs_file(_snapshot_jobs())
    except Exception as e:
        app.logger.error(f"Failed to write jobs file at startup: {e}")

_cleanup_stale_jobs()

@app.route('/admin/settings/transcode_keep_originals', methods=['POST'])
@admin_required
def admin_transcode_keep_originals():
    """Save the keep_originals transcoding setting."""
    data = request.get_json(silent=True) or {}
    keep_originals = data.get('keep_originals', True)
    settings = load_settings()
    settings['transcode_keep_originals'] = keep_originals
    save_settings(settings)
    return jsonify({'success': True})

@app.route('/admin/transcode/scan', methods=['POST'])
@admin_required
def admin_transcode_scan():
    """Start a background job that finds non-H.264 videos (events *and* home feed) and legacy HEIC
    images and converts them. Scanning happens inside the job: probing hundreds of files used to
    run in the request and hit the gunicorn timeout. Thumbnails/viewer images are now built
    lazily, so they are no longer part of this job."""
    data = request.get_json(silent=True) or {}
    keep_originals = bool(data.get('keep_originals', True))

    job_id = str(uuid.uuid4())[:8]
    job = {
        'id': job_id, 'file_log': [], 'index': 0, 'transcoded': 0, 'skipped': 0, 'failed': 0,
        'done': False, 'status': 'Scanning media...', 'keep_originals': keep_originals,
    }
    _all_jobs['transcode'][job_id] = job
    _save_jobs()

    def run_transcode():
        with app.app_context():
            try:
                file_log = []
                for folder in (EVENT_VIDEO_FOLDER, UPLOAD_FOLDER):
                    for f in sorted(os.listdir(folder)):
                        fp = os.path.join(folder, f)
                        if os.path.isfile(fp) and _is_video_file(f) and _cached_codec(fp) in _NON_WEB_SAFE_CODECS:
                            file_log.append({'file': f, 'path': fp, 'type': 'video', 'status': 'pending', 'message': ''})
                for folder in (UPLOAD_FOLDER, EVENT_PHOTO_FOLDER):
                    for f in sorted(os.listdir(folder)):
                        fp = os.path.join(folder, f)
                        if os.path.isfile(fp) and _is_heic_image(f):
                            file_log.append({'file': f, 'path': fp, 'type': 'heic', 'status': 'pending', 'message': ''})
                job['file_log'] = file_log
                total = len(file_log)
                job['status'] = 'Starting...' if total else 'All media is compatible'
                _save_jobs()
                for i, entry in enumerate(file_log):
                    fp = entry['path']
                    entry['status'] = 'processing'
                    job['status'] = f"Processing {entry['file']} ({i+1}/{total})..."
                    job['index'] = i + 1
                    _save_jobs()
                    try:
                        if entry['type'] == 'video':
                            if build_web_video(fp):
                                entry['status'], entry['message'] = 'done', 'Transcoded to H.264'
                                job['transcoded'] += 1
                                if not job['keep_originals']:
                                    try:
                                        os.remove(fp)
                                    except OSError:
                                        pass
                            else:
                                entry['status'], entry['message'] = 'failed', 'Transcode failed'
                                job['failed'] += 1
                        elif entry['type'] == 'heic':
                            if convert_heic_to_jpeg(fp):
                                entry['status'], entry['message'] = 'done', 'Converted to JPEG'
                                job['transcoded'] += 1
                                if not job['keep_originals']:
                                    try:
                                        os.remove(fp)
                                    except OSError:
                                        pass
                            else:
                                entry['status'], entry['message'] = 'failed', 'Conversion failed'
                                job['failed'] += 1
                    except Exception as e:
                        entry['status'], entry['message'] = 'failed', 'Error (see server log)'
                        job['failed'] += 1
                        app.logger.error(f"Transcode error for {fp}: {e}")
                    _save_jobs()
                job['status'] = 'Complete' if total else 'All media is compatible'
                _mark_job_done('transcode', job_id)
                _cleanup_completed_jobs()
            except Exception as e:
                app.logger.error(f"Transcode job {job_id} failed: {e}", exc_info=True)
                job['status'] = 'Error: transcoding job failed (see server log)'
                _mark_job_done('transcode', job_id)
                _cleanup_completed_jobs()

    threading.Thread(target=run_transcode, daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/admin/transcode/progress/<job_id>')
@admin_required
def admin_transcode_progress(job_id):
    """Poll transcoding job progress."""
    job = _get_job('transcode', job_id)
    if not job:
        return jsonify({'error': 'Job not found — may have been cleared'}), 404

    total = len(job.get('file_log', []))
    progress = int((job['index'] / total) * 100) if total > 0 else 100

    return jsonify({
        'progress': progress,
        'status': job['status'],
        'done': job['done'],
        'transcoded': job['transcoded'],
        'skipped': job['skipped'],
        'failed': job['failed'],
        'file_log': job.get('file_log', []),
        'total': total
    })


# Recurring events: run once shortly after start-up and then every 6 hours
_start_repeat_scheduler()

# ============================================
# MAIN
# ============================================

if __name__ == '__main__':
    import socket
    import sys

    # One-time / on-demand batch: build H.264 derivatives for all existing
    # videos without modifying originals.  Usage: python app.py rebuild-web-videos
    if len(sys.argv) > 1 and sys.argv[1] == 'rebuild-web-videos':
        video_folders = [
            ('Event videos', EVENT_VIDEO_FOLDER),
            ('Uploads', UPLOAD_FOLDER),
        ]
        built = skipped = failed = 0
        for label, folder in video_folders:
            vids = [f for f in os.listdir(folder)
                    if os.path.isfile(os.path.join(folder, f))]
            print(f'Scanning {len(vids)} file(s) in {label} ({folder})...')
            for f in vids:
                codec = get_video_codec(os.path.join(folder, f))
                if codec in ('h264', 'avc1') or codec is None or codec not in _NON_WEB_SAFE_CODECS:
                    print(f'  skip  {f}  (codec: {codec})')
                    skipped += 1
                    continue
                print(f'  build {f}  (codec: {codec}) ...', flush=True)
                if build_web_video(os.path.join(folder, f)):
                    built += 1
                else:
                    print(f'  FAILED {f}')
                    failed += 1
        print(f'Done. built={built} skipped={skipped} failed={failed}')
        sys.exit(0)


    # Allow PORT env var to override (e.g. Docker containers)
    port = int(os.environ.get('PORT', 5000))
    ports_to_try = [port] if os.environ.get('PORT') else [5000, 5050, 5051, 5052, 8000, 8080]
    
    for test_port in ports_to_try:
        try:
            # Test if port is available
            test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test_socket.bind(('0.0.0.0', test_port))
            test_socket.close()
            port = test_port
            break
        except OSError:
            continue
    
    # Optional HTTPS: if a cert+key pair is present (SSL_CERT_FILE/SSL_KEY_FILE env vars,
    # or certs/cert.pem + certs/key.pem next to the app / in DATA_DIR), serve over TLS
    # on 8443. Generate one with: ./scripts/gen-self-signed-cert.sh
    def _find_ssl_pair():
        cert = os.environ.get('SSL_CERT_FILE')
        key = os.environ.get('SSL_KEY_FILE')
        if cert and key and os.path.isfile(cert) and os.path.isfile(key):
            return cert, key
        for base in (os.path.dirname(os.path.abspath(__file__)), DATA_DIR, '.'):
            c = os.path.join(base, 'certs', 'cert.pem')
            k = os.path.join(base, 'certs', 'key.pem')
            if os.path.isfile(c) and os.path.isfile(k):
                return c, k
        return None, None

    ssl_cert, ssl_key = _find_ssl_pair()
    use_ssl = bool(ssl_cert and ssl_key)
    scheme = 'https' if use_ssl else 'http'
    if use_ssl:
        port = int(os.environ.get('PORT', 8443))
        print(f"🔒 HTTPS enabled (cert: {ssl_cert})")

    print("=" * 60)
    print("🚀 Flask Personal Site Server")
    print("=" * 60)
    print(f"📍 Running at {scheme}://0.0.0.0:{port}")
    print(f"📍 Also accessible at {scheme}://localhost:{port}")
    print("📝 Check users.json for login credentials")
    print("⚠️  Please change the admin password after first login!")
    print("=" * 60)
    
    # The Werkzeug debugger allows code execution: only enable it explicitly (FLASK_DEBUG=1) and
    # never on all interfaces.
    debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    host = '127.0.0.1' if debug else '0.0.0.0'
    try:
        # ssl_context=None (default) => plain HTTP; a (cert, key) tuple => HTTPS
        app.run(host=host, port=port, threaded=True, debug=debug,
                ssl_context=(ssl_cert, ssl_key) if use_ssl else None)
    except OSError as e:
        print(f"\n❌ Error starting server: {e}")
        print(f"Port {port} may be in use. Try killing the process or use a different port:")
        print(f"  python3 app.py (will auto-find a free port)")
        print(f"\nOr manually specify a port by editing app.py")
