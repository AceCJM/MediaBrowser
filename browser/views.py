import hashlib
import io
import mimetypes
import os

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, FileResponse
from django.shortcuts import get_object_or_404, render

from .models import Library

# ── constants ────────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ogv"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS

THUMBNAIL_SIZE = (320, 180)


# ── helpers ──────────────────────────────────────────────────────────────────

def _safe_join(base, *paths):
    """Join base with paths, raising Http404 if the result escapes base."""
    base = os.path.realpath(base)
    joined = os.path.realpath(os.path.join(base, *paths))
    if not joined.startswith(base + os.sep) and joined != base:
        raise Http404("Path outside library")
    return joined


def _get_accessible_library(user, library_id):
    library = get_object_or_404(Library, pk=library_id)
    if not library.user_has_access(user):
        raise Http404("Library not found")
    return library


def _list_directory(abs_path):
    """Return (folders, media_files) sorted lists for abs_path."""
    folders = []
    media_files = []
    try:
        entries = sorted(os.scandir(abs_path), key=lambda e: e.name.lower())
    except PermissionError:
        return folders, media_files

    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir(follow_symlinks=False):
            folders.append(entry.name)
        elif entry.is_file(follow_symlinks=False):
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in MEDIA_EXTENSIONS:
                media_files.append(entry.name)
    return folders, media_files


def _thumbnail_cache_path(library_id, rel_path):
    """Return the filesystem path for the cached thumbnail."""
    cache_dir = settings.THUMBNAIL_CACHE_DIR
    key = hashlib.sha256(f"{library_id}:{rel_path}".encode()).hexdigest()
    return os.path.join(cache_dir, f"{key}.jpg")


def _generate_image_thumbnail(abs_path):
    """Return JPEG bytes for an image thumbnail, or None on failure."""
    try:
        from PIL import Image
        with Image.open(abs_path) as img:
            img = img.convert("RGB")
            img.thumbnail(THUMBNAIL_SIZE, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            return buf.getvalue()
    except Exception:
        return None


# ── views ─────────────────────────────────────────────────────────────────────

@login_required
def library_list(request):
    """Show all libraries the current user can access."""
    if request.user.is_superuser:
        libraries = Library.objects.all()
    else:
        user_group_ids = request.user.groups.values_list("pk", flat=True)
        libraries = (
            Library.objects.filter(users=request.user)
            | Library.objects.filter(groups__in=user_group_ids)
        ).distinct()

    return render(request, "browser/library_list.html", {"libraries": libraries})


@login_required
def browse(request, library_id, subpath=""):
    """Browse a folder inside a library."""
    library = _get_accessible_library(request.user, library_id)

    subpath = subpath.strip("/").replace("\\", "/")

    if subpath:
        abs_path = _safe_join(library.path, subpath)
    else:
        abs_path = os.path.realpath(library.path)

    if not os.path.isdir(abs_path):
        raise Http404("Directory not found")

    folders, media_files = _list_directory(abs_path)

    # Build breadcrumbs
    parts = [p for p in subpath.split("/") if p]
    breadcrumbs = []
    accumulated = ""
    for part in parts:
        accumulated = f"{accumulated}/{part}" if accumulated else part
        breadcrumbs.append({"name": part, "subpath": accumulated})

    parent_subpath = "/".join(parts[:-1]) if parts else None

    # Build media items with metadata
    media_items = []
    for filename in media_files:
        ext = os.path.splitext(filename)[1].lower()
        rel_path = f"{subpath}/{filename}" if subpath else filename
        media_items.append(
            {
                "filename": filename,
                "rel_path": rel_path,
                "is_video": ext in VIDEO_EXTENSIONS,
                "is_image": ext in IMAGE_EXTENSIONS,
            }
        )

    context = {
        "library": library,
        "subpath": subpath,
        "current_folder": parts[-1] if parts else "",
        "folders": folders,
        "media_items": media_items,
        "breadcrumbs": breadcrumbs,
        "parent_subpath": parent_subpath,
    }
    return render(request, "browser/browse.html", context)


@login_required
def media_player(request, library_id, filepath):
    """Display a media player/viewer page for a single file."""
    library = _get_accessible_library(request.user, library_id)

    filepath = filepath.strip("/")
    abs_path = _safe_join(library.path, filepath)

    if not os.path.isfile(abs_path):
        raise Http404("File not found")

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in MEDIA_EXTENSIONS:
        raise Http404("Unsupported media type")

    parts = filepath.split("/")
    parent_subpath = "/".join(parts[:-1])
    filename = parts[-1]

    mime_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"

    context = {
        "library": library,
        "filepath": filepath,
        "filename": filename,
        "parent_subpath": parent_subpath,
        "is_video": ext in VIDEO_EXTENSIONS,
        "is_image": ext in IMAGE_EXTENSIONS,
        "mime_type": mime_type,
    }
    return render(request, "browser/media_player.html", context)


@login_required
def serve_media(request, library_id, filepath):
    """Stream a media file after verifying permissions."""
    library = _get_accessible_library(request.user, library_id)

    filepath = filepath.strip("/")
    abs_path = _safe_join(library.path, filepath)

    if not os.path.isfile(abs_path):
        raise Http404("File not found")

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in MEDIA_EXTENSIONS:
        raise Http404("Unsupported media type")

    mime_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"

    file_handle = open(abs_path, "rb")
    try:
        response = FileResponse(file_handle, content_type=mime_type)
    except Exception:
        file_handle.close()
        raise
    response["Accept-Ranges"] = "bytes"
    return response


@login_required
def serve_thumbnail(request, library_id, filepath):
    """Return a JPEG thumbnail for an image or an SVG placeholder for a video."""
    library = _get_accessible_library(request.user, library_id)

    filepath = filepath.strip("/")
    abs_path = _safe_join(library.path, filepath)

    if not os.path.isfile(abs_path):
        raise Http404("File not found")

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in MEDIA_EXTENSIONS:
        raise Http404("Unsupported media type")

    # Check thumbnail cache
    cache_path = _thumbnail_cache_path(library_id, filepath)
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            return HttpResponse(f.read(), content_type="image/jpeg")

    os.makedirs(settings.THUMBNAIL_CACHE_DIR, exist_ok=True)

    if ext in IMAGE_EXTENSIONS:
        data = _generate_image_thumbnail(abs_path)
        if data:
            with open(cache_path, "wb") as f:
                f.write(data)
            return HttpResponse(data, content_type="image/jpeg")

    return _video_placeholder_response()


def _video_placeholder_response():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">'
        '<rect width="320" height="180" fill="#1a1a2e"/>'
        '<polygon points="120,60 120,120 200,90" fill="#e94560"/>'
        "</svg>"
    )
    return HttpResponse(svg, content_type="image/svg+xml")
