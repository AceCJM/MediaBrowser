import hashlib
import io
import mimetypes
import os
import subprocess
import json
import threading
import shutil
import uuid
import re
import time

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, FileResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import Library

try:
    from moviepy import VideoFileClip
except ImportError:
    VideoFileClip = None

# ── constants ────────────────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ogv", ".ts", ".mts", ".m2ts"}
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


def _list_directory(abs_path, sort_by='name', sort_order='asc', search_query=''):
    """Return (folders, media_files) sorted and filtered lists for abs_path."""
    folders = []
    media_files = []
    
    if search_query:
        # Recursive search through subdirectories
        search_lower = search_query.lower()
        try:
            for root, dirs, files in os.walk(abs_path):
                # Get relative path from abs_path
                rel_root = os.path.relpath(root, abs_path)
                if rel_root == '.':
                    rel_root = ''
                
                # Filter directories
                for dirname in dirs:
                    if dirname.startswith('.'):
                        continue
                    if search_lower in dirname.lower():
                        # For search results, we need to show the full relative path
                        if rel_root:
                            folder_path = os.path.join(rel_root, dirname)
                        else:
                            folder_path = dirname
                        folders.append(folder_path)
                
                # Filter media files
                for filename in files:
                    if filename.startswith('.'):
                        continue
                    
                    ext = os.path.splitext(filename)[1].lower()
                    if ext in MEDIA_EXTENSIONS:
                        # Check if this file should be excluded (has converted version)
                        full_path = os.path.join(root, filename)
                        rel_path = os.path.relpath(full_path, abs_path)
                        
                        # Skip if there's a converted version
                        name_without_ext = os.path.splitext(filename)[0]
                        ext = os.path.splitext(filename)[1]
                        converted_name = f"{name_without_ext}_converted{ext}"
                        converted_path = os.path.join(root, converted_name)
                        if os.path.exists(converted_path):
                            continue
                            
                        # Check if filename matches search
                        if search_lower in filename.lower():
                            media_files.append(rel_path)
        except PermissionError:
            pass
    else:
        # Original non-recursive logic for normal browsing
        try:
            entries = list(os.scandir(abs_path))  # Convert to list so we can sort it
        except PermissionError:
            return folders, media_files

        # First pass: collect all entries
        all_files = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                folders.append(entry.name)
            elif entry.is_file(follow_symlinks=False):
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in MEDIA_EXTENSIONS:
                    all_files.append(entry.name)

        # Second pass: filter out originals when converted versions exist
        converted_files = set()
        for filename in all_files:
            name_without_ext = os.path.splitext(filename)[0]
            ext = os.path.splitext(filename)[1]
            
            # Check if this is a converted file (ends with _converted)
            if name_without_ext.endswith('_converted'):
                original_name = name_without_ext[:-10] + ext  # Remove '_converted'
                converted_files.add(original_name)
        
        # Only include files that don't have converted versions
        for filename in all_files:
            if filename not in converted_files:
                media_files.append(filename)

    # Apply search filter if provided (only for non-recursive case, recursive is already filtered)
    if search_query and not search_query.strip():
        search_lower = search_query.lower()
        folders = [f for f in folders if search_lower in f.lower()]
        media_files = [f for f in media_files if search_lower in f.lower()]

    # Sort folders
    folders.sort(key=lambda x: x.lower() if sort_by == 'name' else x, reverse=(sort_order == 'desc'))

    # Sort media files
    if sort_by == 'name':
        media_files.sort(key=lambda x: x.lower(), reverse=(sort_order == 'desc'))
    elif sort_by == 'size':
        def get_file_size(filename):
            try:
                full_path = os.path.join(abs_path, filename)
                return os.path.getsize(full_path)
            except OSError:
                return 0
        media_files.sort(key=get_file_size, reverse=(sort_order == 'desc'))
    elif sort_by == 'date':
        def get_file_mtime(filename):
            try:
                full_path = os.path.join(abs_path, filename)
                return os.path.getmtime(full_path)
            except OSError:
                return 0
        media_files.sort(key=get_file_mtime, reverse=(sort_order == 'desc'))

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


def _generate_video_thumbnail(abs_path):
    """Return JPEG bytes for a video thumbnail, or None on failure."""
    if VideoFileClip is None:
        return None
    try:
        with VideoFileClip(abs_path) as clip:
            # Get a frame at 10% into the video, or at 1 second, whichever is smaller
            time = min(clip.duration * 0.1, 1.0)
            frame = clip.get_frame(time)
            # Convert to PIL Image
            from PIL import Image
            img = Image.fromarray(frame)
            img = img.convert("RGB")
            img.thumbnail(THUMBNAIL_SIZE, Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            return buf.getvalue()
    except Exception:
        return None


def _get_video_metadata(abs_path):
    """Extract metadata from a video file using moviepy."""
    if VideoFileClip is None:
        return None
    
    try:
        with VideoFileClip(abs_path) as clip:
            metadata = {
                'duration': clip.duration,
                'width': clip.w,
                'height': clip.h,
                'fps': clip.fps,
                'size': clip.size,
            }
            
            # Add audio information if available
            if hasattr(clip, 'audio') and clip.audio is not None:
                metadata['has_audio'] = True
                metadata['audio_fps'] = getattr(clip.audio, 'fps', None)
                metadata['audio_nchannels'] = getattr(clip.audio, 'nchannels', None)
            else:
                metadata['has_audio'] = False
            
            return metadata
    except Exception:
        return None


def _get_video_codec_info(abs_path):
    """Extract codec information using ffprobe."""
    try:
        # Run ffprobe to get codec information
        cmd = [
            'ffprobe',
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            abs_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
            
        data = json.loads(result.stdout)
        
        codec_info = {}
        
        # Get video stream info
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'video':
                codec_info['video_codec'] = stream.get('codec_name', 'unknown')
                codec_info['video_codec_long'] = stream.get('codec_long_name', '')
                codec_info['profile'] = stream.get('profile', '')
                codec_info['level'] = stream.get('level', '')
                codec_info['bitrate'] = stream.get('bit_rate', '')
                break
        
        # Get audio stream info
        for stream in data.get('streams', []):
            if stream.get('codec_type') == 'audio':
                codec_info['audio_codec'] = stream.get('codec_name', 'unknown')
                codec_info['audio_codec_long'] = stream.get('codec_long_name', '')
                codec_info['audio_channels'] = stream.get('channels', '')
                codec_info['audio_sample_rate'] = stream.get('sample_rate', '')
                break
        
        # Get format info
        format_info = data.get('format', {})
        codec_info['format_name'] = format_info.get('format_name', '')
        codec_info['format_long_name'] = format_info.get('format_long_name', '')
        
        return codec_info
        
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
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

    # Get sorting parameters from request
    sort_by = request.GET.get('sort', 'date')  # Default to date
    sort_order = request.GET.get('order', 'desc')  # Default to descending
    
    # Get search parameter
    search_query = request.GET.get('search', '').strip()
    
    # Validate parameters
    if sort_by not in ['name', 'date', 'size']:
        sort_by = 'name'
    if sort_order not in ['asc', 'desc']:
        sort_order = 'asc'

    folders, media_files = _list_directory(abs_path, sort_by=sort_by, sort_order=sort_order, search_query=search_query)

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
    for rel_path in media_files:
        # rel_path might be just a filename (normal browsing) or a full relative path (recursive search)
        filename = os.path.basename(rel_path)
        ext = os.path.splitext(filename)[1].lower()
        
        media_items.append(
            {
                "filename": filename,
                "rel_path": rel_path,
                "display_path": rel_path if search_query else filename,  # Show full path for search results
                "is_video": ext in VIDEO_EXTENSIONS,
                "is_image": ext in IMAGE_EXTENSIONS,
            }
        )

    # For search results, folders might contain subdirectory paths
    # We need to process them to show proper navigation
    processed_folders = []
    for folder_path in folders:
        if search_query:
            # For search results, folder_path might be "subfolder" or "subfolder/nested"
            # We want to show the top-level folder that contains matches
            top_level = folder_path.split('/')[0]
            if top_level not in processed_folders:
                processed_folders.append(top_level)
        else:
            processed_folders.append(folder_path)
    
    folders = processed_folders

    context = {
        "library": library,
        "subpath": subpath,
        "current_folder": parts[-1] if parts else "",
        "folders": folders,
        "media_items": media_items,
        "breadcrumbs": breadcrumbs,
        "parent_subpath": parent_subpath,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "search_query": search_query,
    }
    return render(request, "browser/browse.html", context)


@login_required
def media_player(request, library_id, filepath):
    """Display a media player/viewer page for a single file."""
    library = _get_accessible_library(request.user, library_id)

    filepath = filepath.strip("/")
    abs_path = _safe_join(library.path, filepath)

    if not os.path.isfile(abs_path):
        # Check if there's a converted version we should redirect to
        name_without_ext = os.path.splitext(filepath)[0]
        ext = os.path.splitext(filepath)[1]
        converted_filepath = f"{name_without_ext}_converted{ext}"
        converted_abs_path = _safe_join(library.path, converted_filepath)
        
        if os.path.isfile(converted_abs_path):
            # Redirect to the converted version
            from django.shortcuts import redirect
            return redirect('browser:media_player', library_id=library_id, filepath=converted_filepath)
        
        raise Http404("File not found")

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in MEDIA_EXTENSIONS:
        raise Http404("Unsupported media type")

    parts = filepath.split("/")
    parent_subpath = "/".join(parts[:-1])
    filename = parts[-1]

    mime_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
    
    # Get file size
    file_size = os.path.getsize(abs_path)
    
    # Get video metadata if it's a video
    video_metadata = None
    codec_info = None
    is_mpeg_ts = False
    if ext in VIDEO_EXTENSIONS:
        video_metadata = _get_video_metadata(abs_path)
        codec_info = _get_video_codec_info(abs_path)
        is_mpeg_ts = ext in {".ts", ".mts", ".m2ts"}

    context = {
        "library": library,
        "filepath": filepath,
        "filename": filename,
        "parent_subpath": parent_subpath,
        "is_video": ext in VIDEO_EXTENSIONS,
        "is_image": ext in IMAGE_EXTENSIONS,
        "is_mpeg_ts": is_mpeg_ts,
        "mime_type": mime_type,
        "file_size": file_size,
        "video_metadata": video_metadata,
        "codec_info": codec_info,
    }
    return render(request, "browser/media_player.html", context)


@login_required
def serve_media(request, library_id, filepath):
    """Stream a media file after verifying permissions."""
    library = _get_accessible_library(request.user, library_id)

    filepath = filepath.strip("/")
    abs_path = _safe_join(library.path, filepath)

    if not os.path.isfile(abs_path):
        # Check if there's a converted version we should redirect to
        name_without_ext = os.path.splitext(filepath)[0]
        ext = os.path.splitext(filepath)[1]
        converted_filepath = f"{name_without_ext}_converted{ext}"
        converted_abs_path = _safe_join(library.path, converted_filepath)
        
        if os.path.isfile(converted_abs_path):
            # Redirect to the converted version
            from django.shortcuts import redirect
            return redirect('browser:serve_media', library_id=library_id, filepath=converted_filepath)
        
        raise Http404("File not found")

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in MEDIA_EXTENSIONS:
        raise Http404("Unsupported media type")

    # Determine MIME type with special handling for MPEG-TS
    if ext in {".ts", ".mts", ".m2ts"}:
        mime_type = "video/MP2T"
        is_mpeg_ts = True
    else:
        mime_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
        is_mpeg_ts = False

    try:
        file_handle = open(abs_path, "rb")
        response = FileResponse(file_handle, content_type=mime_type)
        response["Accept-Ranges"] = "bytes"
        return response
    except (OSError, IOError) as e:
        # Handle file access errors
        raise Http404("File not accessible")


@login_required
def serve_thumbnail(request, library_id, filepath):
    """Return a JPEG thumbnail for an image or an SVG placeholder for a video."""
    library = _get_accessible_library(request.user, library_id)

    filepath = filepath.strip("/")
    abs_path = _safe_join(library.path, filepath)

    if not os.path.isfile(abs_path):
        # Check if there's a converted version we should redirect to
        name_without_ext = os.path.splitext(filepath)[0]
        ext = os.path.splitext(filepath)[1]
        converted_filepath = f"{name_without_ext}_converted{ext}"
        converted_abs_path = _safe_join(library.path, converted_filepath)
        
        if os.path.isfile(converted_abs_path):
            # Redirect to the converted version's thumbnail
            from django.shortcuts import redirect
            return redirect('browser:serve_thumbnail', library_id=library_id, filepath=converted_filepath)
        
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
    elif ext in VIDEO_EXTENSIONS:
        data = _generate_video_thumbnail(abs_path)
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


# ── Internet Video Download functionality ─────────────────────────────────────

# Global download progress tracking
active_downloads_dict = {}

@login_required
@csrf_exempt
def download_videos(request, library_id):
    """Start downloading videos from internet URLs."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    library = _get_accessible_library(request.user, library_id)

    try:
        data = json.loads(request.body)
        urls = data.get('urls', [])
        subpath = data.get('subpath', '')
        quality = data.get('quality', '1080p')
        format_preference = data.get('format', 'mp4')
        include_audio = data.get('include_audio', True)

        if not urls:
            return JsonResponse({'success': False, 'error': 'No URLs provided'})

        # Generate unique download ID
        download_id = str(uuid.uuid4())

        # Initialize progress tracking with timestamps

        active_downloads_dict[download_id] = {
            'id': download_id,
            'total': len(urls),
            'completed': 0,
            'status': 'Starting download...',
            'current_video': None,
            'completed_files': [],
            'error': None,
            'start_time': time.time(),
            'last_update': time.time(),
            'estimated_completion': None,
            'library_id': library_id,
            'user': request.user.username,
            'current_progress': 0.0,
            'cancelled': False
        }

        # Start download in background thread
        thread = threading.Thread(
            target=_perform_video_downloads,
            args=(download_id, library, urls, subpath, quality, format_preference, include_audio)
        )
        thread.daemon = True
        thread.start()

        return JsonResponse({
            'success': True,
            'download_id': download_id,
            'message': f'Started downloading {len(urls)} videos'
        })

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})


def _perform_video_downloads(download_id, library, urls, subpath, quality, format_preference, include_audio):
    """Perform the actual video downloads using yt-dlp."""
    try:


        # Determine download directory
        if subpath:
            download_dir = _safe_join(library.path, subpath)
        else:
            download_dir = os.path.realpath(library.path)

        # Ensure directory exists
        os.makedirs(download_dir, exist_ok=True)

        # Update progress
        active_downloads_dict[download_id]['status'] = f'Downloading to: {download_dir}'
        active_downloads_dict[download_id]['last_update'] = time.time()

        errors = []
        start_time = time.time()

        for i, url in enumerate(urls):
            video_start_time = time.time()
            try:
                active_downloads_dict[download_id]['current_video'] = url
                active_downloads_dict[download_id]['status'] = f'Downloading video {i+1}/{len(urls)}'
                active_downloads_dict[download_id]['last_update'] = time.time()

                # Download the video using yt-dlp
                success, filename = _download_single_video(url, download_dir, quality, format_preference, include_audio, download_id)

                if success and filename:
                    active_downloads_dict[download_id]['completed'] += 1
                    active_downloads_dict[download_id]['completed_files'].append(filename)
                    active_downloads_dict[download_id]['status'] = f'Completed {active_downloads_dict[download_id]["completed"]}/{len(urls)} videos'

                    # Calculate estimated completion time
                    elapsed = time.time() - start_time
                    completed = active_downloads_dict[download_id]['completed']
                    if completed > 0:
                        avg_time_per_video = elapsed / completed
                        remaining = len(urls) - completed
                        estimated_remaining = avg_time_per_video * remaining
                        active_downloads_dict[download_id]['estimated_completion'] = time.time() + estimated_remaining
                else:
                    errors.append(f"Failed to download: {url}")

            except Exception as e:
                errors.append(f"Error downloading {url}: {str(e)}")

            active_downloads_dict[download_id]['last_update'] = time.time()

        # Final status
        if errors:
            active_downloads_dict[download_id]['error'] = '; '.join(errors[:3])  # Show first 3 errors
            active_downloads_dict[download_id]['status'] = f'Completed with {len(errors)} errors'
        else:
            active_downloads_dict[download_id]['status'] = f'Successfully downloaded {len(urls)} videos'

        active_downloads_dict[download_id]['current_video'] = None
        active_downloads_dict[download_id]['last_update'] = time.time()

    except Exception as e:
        active_downloads_dict[download_id]['error'] = str(e)
        active_downloads_dict[download_id]['status'] = 'Download failed'
        active_downloads_dict[download_id]['current_video'] = None
        active_downloads_dict[download_id]['last_update'] = time.time()


def _download_single_video(url, download_dir, quality, format_preference, include_audio, download_id=None):
    """Download a single video using yt-dlp with real-time progress tracking."""
    try:
        # Build yt-dlp command
        cmd = ['yt-dlp']

        # Output template
        cmd.extend(['-o', os.path.join(download_dir, '%(title)s.%(ext)s')])

        # Quality settings
        if quality == 'best':
            cmd.append('-f', 'bestvideo+bestaudio/best')
        elif quality == 'worst':
            cmd.append('-f', 'worstvideo+worstaudio/worst')
        else:
            # Map quality to yt-dlp format
            quality_map = {
                '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
                '720p': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
                '480p': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
                '360p': 'bestvideo[height<=360]+bestaudio/best[height<=360]'
            }
            format_spec = quality_map.get(quality, 'bestvideo[height<=1080]+bestaudio/best[height<=1080]')
            cmd.extend(['-f', format_spec])

        # Format preference
        if format_preference != 'best':
            cmd.extend(['--merge-output-format', format_preference])

        # Audio settings
        if not include_audio:
            cmd.append('--video-only')

        # Progress and output options
        cmd.extend([
            '--no-playlist',  # Don't download playlists
            '--embed-subs',   # Embed subtitles if available
            '--add-metadata', # Add metadata to file
            '--no-overwrites', # Don't overwrite existing files
            '--restrict-filenames', # Restrict filenames to ASCII
            '--progress',     # Show progress
            '--newline',      # Use newlines for progress
        ])

        # Add the URL
        cmd.append(url)

        # Run yt-dlp with real-time progress monitoring
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            universal_newlines=True
        )

        # Track progress in real-time
        current_progress = 0
        filename = None

        while True:
            if download_id and download_id in active_downloads_dict and active_downloads_dict[download_id].get('cancelled', False):
                # Download was cancelled
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                return False, None

            line = process.stdout.readline()
            if not line and process.poll() is not None:
                break

            line = line.strip()
            if not line:
                continue

            # Parse progress from yt-dlp output
            if '[download]' in line and '%' in line:
                try:
                    # Extract percentage from lines like "[download] 45.2% of 100.0MiB at 1.2MiB/s ETA 01:23"
                    percent_match = re.search(r'(\d+(?:\.\d+)?)%', line)
                    if percent_match:
                        current_progress = float(percent_match.group(1))
                        # Update the current video's progress in the global dict
                        if download_id and download_id in active_downloads_dict:
                            active_downloads_dict[download_id]['current_progress'] = current_progress
                            active_downloads_dict[download_id]['last_update'] = time.time()
                except (ValueError, AttributeError):
                    pass

            # Extract filename when download starts
            if '[download] Destination:' in line and not filename:
                dest_part = line.split('[download] Destination:')[-1].strip()
                filename = os.path.basename(dest_part)
            elif '[Merger]' in line and 'Merging' in line and not filename:
                merge_part = line.split('Merging formats into')[-1].strip().strip('"')
                filename = os.path.basename(merge_part)

        # Wait for process to complete
        return_code = process.wait()

        if return_code == 0:
            # Ensure final progress is 100%
            if download_id and download_id in active_downloads_dict:
                active_downloads_dict[download_id]['current_progress'] = 100.0
                active_downloads_dict[download_id]['last_update'] = time.time()
            return True, filename
        else:
            print(f"yt-dlp error for {url}: return code {return_code}")
            return False, None

    except subprocess.TimeoutExpired:
        return False, None
    except Exception as e:
        print(f"Error downloading {url}: {str(e)}")
        return False, None


@login_required
@csrf_exempt
def download_progress(request, library_id):
    """Get download progress for a specific download ID."""
    library = _get_accessible_library(request.user, library_id)

    download_id = request.GET.get('download_id')
    if not download_id or download_id not in active_downloads_dict:
        return JsonResponse({'error': 'Invalid download ID'})

    progress = active_downloads_dict[download_id]
    total = progress['total']
    completed = progress['completed']
    current_progress = progress.get('current_progress', 0.0)

    # Calculate overall progress: completed videos + current video progress
    if total > 0:
        overall_progress = (completed / total) * 100
        if completed < total:  # Still downloading
            overall_progress += (current_progress / total)
        percent = min(100, overall_progress)
    else:
        percent = 0

    response_data = {
        'percent': int(percent),
        'status': progress['status'],
        'completed': completed >= total,
        'downloaded_count': completed,
        'total_count': total,
        'current_video': progress.get('current_video'),
        'error': progress.get('error')
    }

    # Clean up completed downloads after some time
    if response_data['completed'] or response_data['error']:
        # Keep progress for 5 minutes after completion
        def cleanup_progress():
    
            time.sleep(300)  # 5 minutes
            active_downloads_dict.pop(download_id, None)

        cleanup_thread = threading.Thread(target=cleanup_progress)
        cleanup_thread.daemon = True
        cleanup_thread.start()

    return JsonResponse(response_data)


@login_required
def active_downloads(request):
    """Get all active downloads for the current user."""


    user_downloads = []
    current_time = time.time()

    for download_id, progress in active_downloads_dict.items():
        # Only show downloads for this user
        if progress.get('user') != request.user.username:
            continue

        total = progress['total']
        completed = progress['completed']
        percent = int((completed / total) * 100) if total > 0 else 0

        # Calculate time remaining
        time_remaining = None
        if progress.get('estimated_completion'):
            remaining_seconds = max(0, progress['estimated_completion'] - current_time)
            if remaining_seconds > 0:
                hours = int(remaining_seconds // 3600)
                minutes = int((remaining_seconds % 3600) // 60)
                seconds = int(remaining_seconds % 60)
                if hours > 0:
                    time_remaining = f"{hours}h {minutes}m"
                elif minutes > 0:
                    time_remaining = f"{minutes}m {seconds}s"
                else:
                    time_remaining = f"{seconds}s"

        # Calculate elapsed time
        elapsed_seconds = current_time - progress.get('start_time', current_time)
        elapsed_minutes = int(elapsed_seconds // 60)
        elapsed_seconds = int(elapsed_seconds % 60)
        elapsed_time = f"{elapsed_minutes}m {elapsed_seconds}s"

        user_downloads.append({
            'id': download_id,
            'library_id': progress.get('library_id'),
            'percent': percent,
            'status': progress['status'],
            'completed': completed >= total,
            'downloaded_count': completed,
            'total_count': total,
            'current_video': progress.get('current_video'),
            'error': progress.get('error'),
            'time_remaining': time_remaining,
            'elapsed_time': elapsed_time,
            'start_time': progress.get('start_time'),
            'last_update': progress.get('last_update')
        })

    # Sort by start time (newest first)
    user_downloads.sort(key=lambda x: x['start_time'], reverse=True)

    return JsonResponse({'downloads': user_downloads})


@login_required
@csrf_exempt
def cancel_download(request):
    """Cancel an active download."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body)
        download_id = data.get('download_id')

        if not download_id or download_id not in active_downloads_dict:
            return JsonResponse({'success': False, 'error': 'Invalid download ID'})

        # Check if the download belongs to the current user
        progress = active_downloads_dict[download_id]
        if progress.get('user') != request.user.username:
            return JsonResponse({'success': False, 'error': 'Access denied'})

        # Mark download as cancelled
        active_downloads_dict[download_id]['cancelled'] = True
        active_downloads_dict[download_id]['status'] = 'Cancelling download...'
        active_downloads_dict[download_id]['last_update'] = time.time()

        return JsonResponse({'success': True, 'message': 'Download cancellation requested'})

    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
