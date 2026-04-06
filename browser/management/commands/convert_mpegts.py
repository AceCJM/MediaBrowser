import os
import subprocess
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from browser.models import Library


class Command(BaseCommand):
    help = (
        'Convert MPEG-TS video files to MP4 format for better browser compatibility.\n\n'
        'This command detects MPEG-TS files by their actual container format, not just file extensions.\n'
        'Files with any extension (.mp4, .mkv, etc.) that contain MPEG-TS data will be processed.\n\n'
        'Processing logic:\n'
        '- Files already encoded with H.264 are remuxed (fast, no quality loss)\n'
        '- Files with other codecs are re-encoded to H.264 (slower but necessary)\n\n'
        'Use --dry-run to see what would be done without making changes.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--library-id',
            type=int,
            help='Library ID to process (optional, processes all libraries if not specified)',
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be converted without actually doing it',
        )
        parser.add_argument(
            '--overwrite',
            action='store_true',
            help='Overwrite existing MP4 files',
        )

    def handle(self, *args, **options):
        library_id = options.get('library_id')
        dry_run = options.get('dry_run', False)
        overwrite = options.get('overwrite', False)

        # Check if ffmpeg is available
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            raise CommandError('ffmpeg is not installed or not available in PATH')

        # Get libraries to process
        if library_id:
            try:
                libraries = [Library.objects.get(pk=library_id)]
            except Library.DoesNotExist:
                raise CommandError(f'Library with ID {library_id} does not exist')
        else:
            libraries = Library.objects.all()

        total_converted = 0
        total_skipped = 0
        total_remuxed = 0
        total_reencoded = 0

        for library in libraries:
            self.stdout.write(f'Processing library: {library.name}')
            converted, skipped, remuxed, reencoded = self._process_library(library, dry_run, overwrite)
            total_converted += converted
            total_skipped += skipped
            total_remuxed += remuxed
            total_reencoded += reencoded

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Dry run complete. Would convert {total_converted} files '
                    f'({total_remuxed} remux, {total_reencoded} re-encode), skip {total_skipped}'
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f'Conversion complete. Converted {total_converted} files '
                    f'({total_remuxed} remuxed, {total_reencoded} re-encoded), skipped {total_skipped}'
                )
            )

    def _process_library(self, library, dry_run, overwrite):
        """Process all MPEG-TS files in a library."""
        converted = 0
        skipped = 0
        remuxed = 0
        reencoded = 0

        # Walk through the library directory
        for root, dirs, files in os.walk(library.path):
            for file in files:
                # Check files that might be video files
                if file.lower().endswith(('.ts', '.mts', '.m2ts', '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v', '.ogv')):
                    file_path = Path(root) / file
                    
                    # Check if this is actually an MPEG-TS file
                    if not self._is_mpegts_file(str(file_path)):
                        continue
                        
                    # For files that are already .mp4 but contain MPEG-TS, create a new name
                    if file_path.suffix.lower() == '.mp4':
                        mp4_path = file_path.with_stem(f"{file_path.stem}_converted")
                    else:
                        mp4_path = file_path.with_suffix('.mp4')

                    # Check if MP4 already exists
                    if mp4_path.exists() and not overwrite:
                        try:
                            filename_display = str(file_path.relative_to(library.path))
                            self.stdout.write(f'  Skipping (MP4 exists): {filename_display}')
                        except UnicodeEncodeError:
                            self.stdout.write(f'  Skipping (MP4 exists): [filename with special characters]')
                        skipped += 1
                        continue

                    if dry_run:
                        # Check codec for dry run info
                        codec_info = self._get_video_codec(str(file_path))
                        codec_name = codec_info.get('video_codec', 'unknown') if codec_info else 'unknown'
                        action = 'remux' if codec_name == 'h264' else 're-encode'
                        # Handle Unicode characters in filenames
                        try:
                            filename_display = str(file_path.relative_to(library.path))
                            self.stdout.write(f'  Would {action} ({codec_name}): {filename_display}')
                        except UnicodeEncodeError:
                            # Fallback for filenames with problematic Unicode characters
                            self.stdout.write(f'  Would {action} ({codec_name}): [filename with special characters]')
                        converted += 1
                        if codec_name == 'h264':
                            remuxed += 1
                        else:
                            reencoded += 1
                    else:
                        try:
                            was_remuxed = self._convert_file(file_path, mp4_path)
                            action = 'Remuxed' if was_remuxed else 'Re-encoded'
                            try:
                                filename_display = str(file_path.relative_to(library.path))
                                self.stdout.write(
                                    self.style.SUCCESS(f'  {action}: {filename_display}')
                                )
                            except UnicodeEncodeError:
                                self.stdout.write(
                                    self.style.SUCCESS(f'  {action}: [filename with special characters]')
                                )
                            converted += 1
                            if was_remuxed:
                                remuxed += 1
                            else:
                                reencoded += 1
                        except Exception as e:
                            self.stdout.write(
                                self.style.ERROR(f'  Failed to convert {file_path.relative_to(library.path)}: {e}')
                            )

        return converted, skipped, remuxed, reencoded

    def _convert_file(self, input_path, output_path):
        """Convert a single MPEG-TS file to MP4, remuxing if already H.264."""
        
        # First, check the video codec
        codec_info = self._get_video_codec(str(input_path))
        
        if codec_info and codec_info.get('video_codec') == 'h264':
            # Already H.264, just remux to MP4 (much faster)
            self._remux_to_mp4(input_path, output_path)
            return True  # Was remuxed
        else:
            # Need to re-encode
            self._reencode_to_mp4(input_path, output_path)
            return False  # Was re-encoded

    def _is_mpegts_file(self, file_path):
        """Check if a file is in MPEG-TS format by examining its container."""
        try:
            result = subprocess.run([
                'ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', file_path
            ], capture_output=True, text=False, timeout=5)
            
            if result.returncode == 0 and result.stdout:
                try:
                    stdout_str = result.stdout.decode('utf-8', errors='ignore')
                    data = json.loads(stdout_str)
                    format_name = data.get('format', {}).get('format_name', '')
                    return 'mpegts' in format_name.lower()
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return False
            
            return False
        except (subprocess.TimeoutExpired, FileNotFoundError, TypeError):
            return False

    def _get_video_codec(self, file_path):
        """Get video codec information using ffprobe."""
        try:
            cmd = [
                'ffprobe',
                '-v', 'quiet',
                '-print_format', 'json',
                '-show_streams',
                file_path
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=False, timeout=10)
            if result.returncode != 0:
                return None
            
            try:
                stdout_str = result.stdout.decode('utf-8', errors='ignore')
                data = json.loads(stdout_str)
                
                # Find video stream
                for stream in data.get('streams', []):
                    if stream.get('codec_type') == 'video':
                        return {
                            'video_codec': stream.get('codec_name', 'unknown'),
                            'profile': stream.get('profile', ''),
                            'width': stream.get('width', 0),
                            'height': stream.get('height', 0),
                        }
                
                return None
            except (UnicodeDecodeError, json.JSONDecodeError):
                return None
            
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def _remux_to_mp4(self, input_path, output_path):
        """Remux MPEG-TS to MP4 without re-encoding (fast)."""
        cmd = [
            'ffmpeg',
            '-i', str(input_path),
            '-c', 'copy',  # Copy streams without re-encoding
            '-bsf:a', 'aac_adtstoasc',  # Convert ADTS to ASC for AAC audio
            '-movflags', '+faststart',  # Enable fast start for web playback
            '-y',  # Overwrite output files
            str(output_path)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f'ffmpeg remux failed: {result.stderr}')

        if not output_path.exists():
            raise Exception('Output file was not created')

    def _reencode_to_mp4(self, input_path, output_path):
        """Re-encode video to H.264 MP4 (slower but necessary for non-H.264 codecs)."""
        cmd = [
            'ffmpeg',
            '-i', str(input_path),
            '-c:v', 'libx264',  # Re-encode video to H.264
            '-preset', 'medium',  # Good balance of speed/quality
            '-crf', '23',  # Constant Rate Factor for quality
            '-c:a', 'aac',  # Convert audio to AAC
            '-b:a', '128k',  # Audio bitrate
            '-movflags', '+faststart',  # Enable fast start for web playback
            '-y',  # Overwrite output files
            str(output_path)
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f'ffmpeg re-encode failed: {result.stderr}')

        if not output_path.exists():
            raise Exception('Output file was not created')