"""
media.py – Media helper functions for the personal site Flask app.

Handles video codec detection, H.264 derivative generation, HEIC conversion,
viewer-optimized image derivatives, and video streaming with Range support.
"""

import os
import subprocess
import threading
import logging
from PIL import Image, ImageOps
from flask import request, Response
from werkzeug.utils import secure_filename
from werkzeug.http import parse_range_header

# Register pillow_heif so PIL.Image.open() can handle HEIC files
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Folder constants
# ---------------------------------------------------------------------------
UPLOAD_FOLDER = 'static/uploads'
EVENT_PHOTO_FOLDER = 'static/event_photos'
EVENT_VIDEO_FOLDER = 'static/event_videos'
EVENT_VIDEO_WEB_FOLDER = 'static/event_videos_web'
VIEWER_IMAGE_FOLDER = 'static/viewer_images'
THUMB_FOLDER = 'static/thumbnails'

# Source folders addressable from URLs (/photo/<key>/…, /video/<key>/…, /thumb/<size>/<key>/…)
MEDIA_FOLDERS = {
    'uploads': UPLOAD_FOLDER,
    'event_photos': EVENT_PHOTO_FOLDER,
    'event_videos': EVENT_VIDEO_FOLDER,
}

# Derivative sizes (max side in px, JPEG quality): sm = strips/thumbnails, md = feed cards, xl = fullscreen viewer
THUMB_SIZES = {'sm': (400, 80), 'md': (1280, 82), 'xl': (3200, 90)}

# Refuse to write new media when free disk space would drop below this (env MIN_FREE_DISK_MB)
MIN_FREE_DISK_BYTES = int(os.environ.get('MIN_FREE_DISK_MB', '512')) * 1024 * 1024

# Viewer-optimized image settings
VIEWER_IMAGE_MAX_SIDE = 3200
VIEWER_IMAGE_QUALITY = 90

# Ensure directories exist
for _folder in (UPLOAD_FOLDER, EVENT_PHOTO_FOLDER, EVENT_VIDEO_FOLDER,
                EVENT_VIDEO_WEB_FOLDER, VIEWER_IMAGE_FOLDER, THUMB_FOLDER):
    os.makedirs(_folder, exist_ok=True)


def has_free_space(path, incoming_bytes=0):
    """True if writing `incoming_bytes` under `path` keeps MIN_FREE_DISK_BYTES free."""
    try:
        import shutil
        free = shutil.disk_usage(path if os.path.isdir(path) else os.path.dirname(path) or '.').free
        return free - int(incoming_bytes or 0) > MIN_FREE_DISK_BYTES
    except Exception:
        return True

# ---------------------------------------------------------------------------
# Video codec detection & H.264 derivative generation
# ---------------------------------------------------------------------------

# Video codecs that some browsers cannot decode reliably.
_NON_WEB_SAFE_CODECS = {
    'hevc', 'h265', 'hev1', 'hvc1', 'av01', 'av1',
    'vp8', 'vp9', 'vp10', 'theora', 'mjpeg', 'mpeg4', 'mp4v',
}

_web_video_locks = {}
_web_video_locks_guard = threading.Lock()
_codec_cache = {}
_CODEC_CACHE_MAX = 500


def get_video_codec(video_path):
    """Use ffprobe to determine the video codec. Returns codec name or None."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'stream=codec_name',
             '-of', 'default=noprint_wrappers=1:nokey=1',
             video_path],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip().lower() if result.returncode == 0 else None
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return None


def _cached_codec(path):
    """Get video codec with caching to avoid repeated ffprobe calls."""
    try:
        st = os.stat(path)
        key = (path, st.st_size, int(st.st_mtime))
    except OSError:
        return None
    if key in _codec_cache:
        return _codec_cache[key]
    codec = get_video_codec(path)
    _codec_cache[key] = codec
    # Prune oldest entries if cache grows too large
    if len(_codec_cache) > _CODEC_CACHE_MAX:
        oldest_keys = list(_codec_cache.keys())[:len(_codec_cache) - _CODEC_CACHE_MAX // 2]
        for old_key in oldest_keys:
            del _codec_cache[old_key]
    return codec


def _web_derivative_path(video_path):
    """Path where the H.264 derivative for a video would live.

    Event videos keep the historical layout (event_videos_web/<stem>.mp4). Videos from the home
    feed (static/uploads) get a `uploads__` prefix so an upload and an event video with the same
    name no longer overwrite each other's derivative. Pass the full path for upload videos."""
    name, _ = os.path.splitext(os.path.basename(video_path))
    parent = os.path.basename(os.path.normpath(os.path.dirname(video_path))) if os.path.dirname(video_path) else ''
    prefix = 'uploads__' if parent == os.path.basename(os.path.normpath(UPLOAD_FOLDER)) else ''
    return os.path.join(EVENT_VIDEO_WEB_FOLDER, f'{prefix}{name}.mp4')


def build_web_video(video_path):
    """Ensure a browser-safe H.264 derivative exists for any video file.

    Accepts a full path to the original video. Returns the derivative path if
    one is needed and available, None if the original is already H.264
    (serve it directly) or on failure. The original is never modified or deleted.
    """
    if not os.path.exists(video_path):
        return None

    filename = os.path.basename(video_path)
    derivative = _web_derivative_path(filename)
    if os.path.exists(derivative) and os.path.getsize(derivative) > 0:
        return derivative

    codec = _cached_codec(video_path)
    # Already H.264 -> universally playable, no derivative needed.
    if codec == 'h264' or codec == 'avc1':
        return None
    # Unknown codec -> don't risk a pointless transcode; serve original.
    if codec is None or codec not in _NON_WEB_SAFE_CODECS:
        return None

    # Serialize per-file so concurrent requests don't transcode the same video.
    with _web_video_locks_guard:
        lock = _web_video_locks.setdefault(filename, threading.Lock())
    with lock:
        if os.path.exists(derivative) and os.path.getsize(derivative) > 0:
            return derivative
        tmp = derivative + '.partial'
        try:
            result = subprocess.run(
                ['ffmpeg', '-y',
                 '-fflags', '+genpts',
                 '-i', video_path,
                 '-c:v', 'libx264', '-crf', '23', '-preset', 'medium',
                 '-pix_fmt', 'yuv420p',
                 '-c:a', 'aac', '-b:a', '128k',
                 '-max_muxing_queue_size', '1024',
                 '-movflags', '+faststart',
                 '-f', 'mp4',
                 tmp],
                capture_output=True, text=True, timeout=1800
            )
            if result.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, derivative)  # atomic; never touches original
                logger.info('Built H.264 web derivative for %s (source codec: %s)',
                            filename, codec)
                return derivative
            logger.error('ffmpeg rc=%s building derivative for %s; '
                         'last stderr lines:\n%s',
                         result.returncode, filename,
                         '\n'.join(result.stderr.strip().splitlines()[-8:]))
        except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
            logger.error('Derivative build error for %s: %s', filename, e)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return None


def build_web_video_async(video_path):
    """Pre-build a derivative in the background so uploads don't block.
    Accepts a full path or just a filename (assumes EVENT_VIDEO_FOLDER)."""
    if os.path.isdir(video_path):
        return
    if not os.path.isabs(video_path) and '/' not in video_path:
        full_path = os.path.join(EVENT_VIDEO_FOLDER, secure_filename(video_path))
    else:
        full_path = video_path
    threading.Thread(target=build_web_video, args=(full_path,), daemon=True).start()


# ---------------------------------------------------------------------------
# Upload validation
# ---------------------------------------------------------------------------

# Canonical extensions we store on disk, and the Content-Type used to serve them.
IMAGE_MIMETYPES = {
    'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif',
    'webp': 'image/webp', 'bmp': 'image/bmp', 'tif': 'image/tiff', 'tiff': 'image/tiff',
    'heic': 'image/heic', 'heif': 'image/heif',
}
_PIL_FORMAT_TO_EXT = {'jpeg': 'jpg', 'png': 'png', 'gif': 'gif', 'webp': 'webp', 'bmp': 'bmp',
                      'tiff': 'tif', 'heif': 'heic'}
_HEIC_BRANDS = {b'heic', b'heix', b'hevc', b'hevx', b'heim', b'heis', b'mif1', b'msf1'}
_VIDEO_EXTENSIONS = {'mp4', 'mov', 'm4v', 'webm', 'mkv', 'avi'}


def safe_stem(filename, max_len=60):
    """Sanitised original file stem for human-readable stored names ('file' if nothing is left)."""
    stem = os.path.splitext(os.path.basename(filename or ''))[0]
    stem = secure_filename(stem).strip('._')[:max_len]
    return stem or 'file'


def sniff_upload(file_storage):
    """Validate an uploaded file by extension AND content.

    Returns ('image', ext) or ('video', ext) with the canonical extension to store the file
    under, or None if the file is not an accepted photo/video. Never trust the client's
    Content-Type: an .html renamed to .jpg or sent as image/png must be rejected."""
    name = file_storage.filename or ''
    ext = os.path.splitext(name)[1].lower().lstrip('.')
    stream = file_storage.stream
    try:
        stream.seek(0)
        head = stream.read(64)
        stream.seek(0)
    except Exception:
        return None
    if not head:
        return None

    if _is_heic_image('x.' + ext):
        ok = len(head) >= 12 and head[4:8] == b'ftyp' and head[8:12] in _HEIC_BRANDS
        return ('image', 'heic') if ok else None

    if _is_image_file('x.' + ext):
        try:
            with Image.open(stream) as im:
                fmt = (im.format or '').lower()
        except Exception:
            fmt = ''
        finally:
            try:
                stream.seek(0)
            except Exception:
                pass
        canonical = _PIL_FORMAT_TO_EXT.get(fmt)
        return ('image', canonical) if canonical else None

    if ext in _VIDEO_EXTENSIONS:
        is_isobmff = len(head) >= 12 and head[4:8] == b'ftyp'
        is_ebml = head[:4] == b'\x1a\x45\xdf\xa3'
        is_avi = head[:4] == b'RIFF' and head[8:12] == b'AVI '
        if ext in ('mp4', 'mov', 'm4v') and is_isobmff:
            return ('video', ext)
        if ext in ('webm', 'mkv') and is_ebml:
            return ('video', ext)
        if ext == 'avi' and is_avi:
            return ('video', ext)
        return None

    return None


# ---------------------------------------------------------------------------
# File type detection
# ---------------------------------------------------------------------------

def _is_video_file(filename):
    """Check if a filename has a video extension."""
    video_ext = {'.mp4', '.mov', '.webm', '.m4v', '.avi', '.mkv', '.wmv',
                 '.flv', '.ogg', '.ogv'}
    ext = os.path.splitext(filename)[1].lower()
    return ext in video_ext


def _is_image_file(filename):
    """Check if a filename has an image extension (excluding HEIC)."""
    image_ext = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff', '.tif'}
    ext = os.path.splitext(filename)[1].lower()
    return ext in image_ext


def _is_heic_image(filename):
    """Check if a filename is a HEIC/HEIF image."""
    heic_ext = {'.heic', '.heics', '.heif', '.heifs', '.hif'}
    ext = os.path.splitext(filename)[1].lower()
    return ext in heic_ext


# ---------------------------------------------------------------------------
# HEIC conversion
# ---------------------------------------------------------------------------

def convert_heic_to_jpeg(filepath):
    """Convert a HEIC/HEIF image to JPEG. Returns the new JPEG path, or None on failure."""
    try:
        import pillow_heif
    except ImportError:
        logger.error('HEIC conversion failed: pillow_heif not installed')
        return None
    try:
        img = pillow_heif.read_heif(filepath)
        name, _ = os.path.splitext(filepath)
        jpeg_path = name + '.jpg'
        # Convert to RGB if necessary (HEIC may have alpha)
        if img.mode in ('RGBA', 'LA', 'P'):
            img = img.convert('RGB')
        img.save(jpeg_path, 'JPEG', quality=92)
        return jpeg_path
    except Exception as e:
        logger.error('HEIC conversion failed for %s: %s', filepath, e)
        return None


# ---------------------------------------------------------------------------
# Viewer-optimized image derivatives
# ---------------------------------------------------------------------------

def _is_large_image(filepath, max_side=VIEWER_IMAGE_MAX_SIDE):
    """Check if an image exceeds the viewer max dimension threshold."""
    try:
        with Image.open(filepath) as im:
            return max(im.size) > max_side
    except Exception:
        return False


def generate_viewer_image(src_path, max_side=VIEWER_IMAGE_MAX_SIDE,
                          quality=VIEWER_IMAGE_QUALITY):
    """Generate a viewer-optimized derivative: EXIF-normalized, resized to max_side,
    progressive JPEG. Returns the output path or None on failure.

    The output filename keeps the original stem but forces .jpg extension,
    stored in VIEWER_IMAGE_FOLDER."""
    try:
        stem = os.path.splitext(os.path.basename(src_path))[0]
        out_path = os.path.join(VIEWER_IMAGE_FOLDER, stem + '.jpg')

        # Skip if already generated and newer than source
        if os.path.exists(out_path) and os.path.getmtime(out_path) >= os.path.getmtime(src_path):
            return out_path

        with Image.open(src_path) as im:
            # Apply EXIF orientation (auto-rotate)
            im = ImageOps.exif_transpose(im)
            # Convert to RGB for JPEG compatibility
            if im.mode not in ('RGB', 'L'):
                im = im.convert('RGB')
            # Resize if too large
            if max(im.size) > max_side:
                im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            # Save as progressive JPEG
            im.save(
                out_path,
                format='JPEG',
                quality=quality,
                optimize=True,
                progressive=True,
            )
        return out_path
    except Exception as e:
        logger.error('Viewer image generation failed for %s: %s', src_path, e)
        return None


# ---------------------------------------------------------------------------
# Thumbnails / posters (generated lazily, namespaced by source folder)
# ---------------------------------------------------------------------------

_thumb_locks = {}
_thumb_locks_guard = threading.Lock()


def thumbnail_path(folder_key, filename, size):
    stem = os.path.splitext(os.path.basename(filename))[0]
    return os.path.join(THUMB_FOLDER, size, folder_key, stem + '.jpg')


def _video_poster(src_path, out_path, max_side):
    """Grab a poster frame with ffmpeg. Returns True on success."""
    tmp = out_path + '.partial.jpg'
    try:
        result = subprocess.run(
            ['ffmpeg', '-y', '-v', 'error', '-ss', '1', '-i', src_path, '-frames:v', '1',
             '-vf', f"scale='min({max_side},iw)':-2", '-q:v', '4', tmp],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            # Some clips are shorter than 1s: retry from the first frame
            result = subprocess.run(
                ['ffmpeg', '-y', '-v', 'error', '-i', src_path, '-frames:v', '1',
                 '-vf', f"scale='min({max_side},iw)':-2", '-q:v', '4', tmp],
                capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, out_path)
            return True
        logger.error('poster generation failed for %s: %s', src_path, result.stderr.strip()[-300:])
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception) as e:
        logger.error('poster generation error for %s: %s', src_path, e)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return False


def get_thumbnail(folder_key, filename, size):
    """Return the path of a JPEG derivative (thumbnail for images, poster frame for videos),
    generating it if missing or older than the source. Returns None if it cannot be built."""
    base = MEDIA_FOLDERS.get(folder_key)
    if base is None or size not in THUMB_SIZES:
        return None
    src = os.path.join(base, os.path.basename(filename))
    if not os.path.isfile(src):
        return None
    out = thumbnail_path(folder_key, filename, size)
    try:
        if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(src) and os.path.getsize(out) > 0:
            return out
    except OSError:
        pass
    max_side, quality = THUMB_SIZES[size]
    with _thumb_locks_guard:
        lock = _thumb_locks.setdefault(out, threading.Lock())
    with lock:
        if os.path.exists(out) and os.path.getsize(out) > 0 and os.path.getmtime(out) >= os.path.getmtime(src):
            return out
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if _is_video_file(src):
            return out if _video_poster(src, out, max_side) else None
        tmp = out + '.partial.jpg'
        try:
            with Image.open(src) as im:
                im.draft('RGB', (max_side, max_side))  # fast JPEG downscale when possible
                im = ImageOps.exif_transpose(im)
                if im.mode not in ('RGB', 'L'):
                    im = im.convert('RGB')
                if max(im.size) > max_side:
                    im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
                im.save(tmp, format='JPEG', quality=quality, optimize=True, progressive=True)
            os.replace(tmp, out)
            return out
        except Exception as e:
            logger.error('thumbnail generation failed for %s: %s', src, e)
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return None


def prebuild_thumbnails(folder_key, filename, sizes=('sm', 'md')):
    """Build the common derivative sizes in a background thread (called right after upload)."""
    def _run():
        for size in sizes:
            get_thumbnail(folder_key, filename, size)
    threading.Thread(target=_run, daemon=True).start()


def delete_derivatives(folder_key, filename):
    """Remove every derivative of a media file: thumbnails, H.264 web copy, legacy viewer image."""
    base = MEDIA_FOLDERS.get(folder_key, '')
    candidates = [thumbnail_path(folder_key, filename, size) for size in THUMB_SIZES]
    candidates.append(_web_derivative_path(os.path.join(base, filename)))
    stem = os.path.splitext(os.path.basename(filename))[0]
    candidates.append(os.path.join(VIEWER_IMAGE_FOLDER, stem + '.jpg'))
    for path in candidates:
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as e:
            logger.error('could not remove derivative %s: %s', path, e)


# ---------------------------------------------------------------------------
# Video serving (Range request support)
# ---------------------------------------------------------------------------

def get_video_mimetype(filename):
    """Get the correct MIME type for video files."""
    ext = os.path.splitext(filename)[1].lower()
    mimetypes = {
        '.mp4': 'video/mp4',
        '.mov': 'video/quicktime',
        '.webm': 'video/webm',
        '.m4v': 'video/x-m4v',
        '.avi': 'video/x-msvideo',
        '.mkv': 'video/x-matroska',
    }
    return mimetypes.get(ext, 'video/mp4')


def stream_video(video_path):
    """Stream video with proper Range request support.
    Checks for and serves H.264 derivatives when available."""
    if not os.path.exists(video_path):
        return "Video not found", 404

    file_size = os.path.getsize(video_path)
    range_header = request.headers.get('Range')
    mimetype = get_video_mimetype(video_path)

    if range_header:
        # Proper parsing (suffix ranges like "bytes=-500", malformed headers → 416/ignored)
        rng = parse_range_header(range_header)
        if rng is None or rng.units != 'bytes' or len(rng.ranges) != 1:
            resp = Response("Requested range not satisfiable", status=416)
            resp.headers['Content-Range'] = f'bytes */{file_size}'
            return resp
        bounds = rng.range_for_length(file_size)
        if bounds is None:
            resp = Response("Requested range not satisfiable", status=416)
            resp.headers['Content-Range'] = f'bytes */{file_size}'
            return resp
        byte_start, byte_end = bounds[0], bounds[1] - 1
        content_length = byte_end - byte_start + 1

        def generate():
            with open(video_path, 'rb') as f:
                f.seek(byte_start)
                remaining = content_length
                chunk_size = 8192
                while remaining > 0:
                    read_size = min(chunk_size, remaining)
                    data = f.read(read_size)
                    if not data:
                        break
                    remaining -= len(data)
                    yield data

        response = Response(
            generate(),
            status=206,
            mimetype=mimetype,
            direct_passthrough=True
        )
        response.headers['Content-Range'] = f'bytes {byte_start}-{byte_end}/{file_size}'
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Content-Length'] = content_length
        response.headers['Cache-Control'] = 'private, max-age=86400'
        return response
    else:
        def generate():
            with open(video_path, 'rb') as f:
                chunk_size = 8192
                while True:
                    data = f.read(chunk_size)
                    if not data:
                        break
                    yield data

        response = Response(
            generate(),
            status=200,
            mimetype=mimetype,
            direct_passthrough=True
        )
        response.headers['Accept-Ranges'] = 'bytes'
        response.headers['Content-Length'] = file_size
        response.headers['Cache-Control'] = 'private, max-age=86400'
        return response
