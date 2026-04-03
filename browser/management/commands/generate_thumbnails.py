import hashlib
import io
import os
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from browser.models import Library

try:
    from moviepy import VideoFileClip
except ImportError:
    VideoFileClip = None

# Constants from views.py
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".tif"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ogv"}
MEDIA_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
THUMBNAIL_SIZE = (320, 180)


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


class Command(BaseCommand):
    help = 'Generate thumbnails for all media files in libraries'

    def add_arguments(self, parser):
        parser.add_argument(
            '--library-id',
            type=int,
            help='Generate thumbnails for a specific library ID only',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be done without actually generating thumbnails',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Regenerate thumbnails even if they already exist',
        )

    def handle(self, *args, **options):
        library_id = options['library_id']
        dry_run = options['dry_run']
        force = options['force']

        if library_id:
            try:
                libraries = [Library.objects.get(pk=library_id)]
            except Library.DoesNotExist:
                raise CommandError(f'Library with ID {library_id} does not exist')
        else:
            libraries = Library.objects.all()

        if not libraries:
            self.stdout.write('No libraries found.')
            return

        # Ensure cache directory exists
        os.makedirs(settings.THUMBNAIL_CACHE_DIR, exist_ok=True)

        total_processed = 0
        total_generated = 0
        total_skipped = 0
        total_errors = 0

        for library in libraries:
            self.stdout.write(f'Processing library: {library.name} (ID: {library.id})')
            processed, generated, skipped, errors = self._process_library(
                library, dry_run, force
            )
            total_processed += processed
            total_generated += generated
            total_skipped += skipped
            total_errors += errors

        self.stdout.write(
            self.style.SUCCESS(
                f'Completed: {total_processed} files processed, '
                f'{total_generated} thumbnails generated, '
                f'{total_skipped} skipped, '
                f'{total_errors} errors'
            )
        )

    def _process_library(self, library, dry_run, force):
        """Process all media files in a library."""
        processed = 0
        generated = 0
        skipped = 0
        errors = 0

        base_path = Path(library.path)

        for root, dirs, files in os.walk(base_path):
            # Skip hidden directories
            dirs[:] = [d for d in dirs if not d.startswith('.')]

            for filename in files:
                if filename.startswith('.'):
                    continue

                ext = os.path.splitext(filename)[1].lower()
                if ext not in MEDIA_EXTENSIONS:
                    continue

                # Get relative path from library root
                file_path = Path(root) / filename
                try:
                    rel_path = file_path.relative_to(base_path).as_posix()
                except ValueError:
                    # File outside library path (shouldn't happen with os.walk)
                    continue

                processed += 1

                # Check if thumbnail already exists
                cache_path = _thumbnail_cache_path(library.id, rel_path)
                if os.path.exists(cache_path) and not force:
                    if not dry_run:
                        self.stdout.write(
                            f'  Skipping {rel_path} (thumbnail exists)'
                        )
                    skipped += 1
                    continue

                if dry_run:
                    self.stdout.write(f'  Would generate thumbnail for {rel_path}')
                    generated += 1
                    continue

                # Generate thumbnail
                abs_path = str(file_path)
                try:
                    if ext in IMAGE_EXTENSIONS:
                        data = _generate_image_thumbnail(abs_path)
                    elif ext in VIDEO_EXTENSIONS:
                        data = _generate_video_thumbnail(abs_path)
                    else:
                        data = None

                    if data:
                        with open(cache_path, 'wb') as f:
                            f.write(data)
                        self.stdout.write(
                            self.style.SUCCESS(f'  Generated thumbnail for {rel_path}')
                        )
                        generated += 1
                    else:
                        self.stdout.write(
                            self.style.WARNING(f'  Failed to generate thumbnail for {rel_path}')
                        )
                        errors += 1

                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(f'  Error generating thumbnail for {rel_path}: {e}')
                    )
                    errors += 1

        return processed, generated, skipped, errors