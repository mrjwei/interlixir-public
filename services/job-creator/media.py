"""ffprobe / ffmpeg for the job creator. The static binaries are in the image (Dockerfile).

probe() checks a file is readable and has an audio track; split() does extract + normalise + chunk in one
ffmpeg pass: first audio track only, mono, 16 kHz, 16-bit PCM WAV (what Whisper wants), cut every
chunk_seconds into <out_dir>/<n>.wav with n from 0. A last chunk under MIN_TAIL_SECONDS is dropped: encoder
padding makes an exact 30:00 file decode a few samples long, which would otherwise be a 7th, empty chunk.
"""

import json
import pathlib
import subprocess
import wave

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
MIN_TAIL_SECONDS = 1.0


class InputError(Exception):
    """The file itself is the problem (not audio, no audio track, too long). Retrying won't help, so the job
    is marked Failed with this message, which the user sees."""


def probe(path, timeout):
    """Duration of the file in seconds (None if the container doesn't say). Raises InputError if ffprobe
    can't read it or it has no audio track."""
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-select_streams", "a", "-show_entries", "stream=index:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise InputError("The file couldn't be read as audio or video. Check it plays, then upload it again.")
    info = json.loads(result.stdout or "{}")
    if not info.get("streams"):
        raise InputError("The file has no audio track.")
    duration = info.get("format", {}).get("duration")
    try:
        return float(duration)
    except (TypeError, ValueError):
        return None


def split(path, out_dir, chunk_seconds, timeout):
    """Chunk files in order, as paths. Raises InputError if ffmpeg can't decode the audio, and RuntimeError
    for anything that isn't the file's fault (out of /tmp space), so that one is retried."""
    out_dir = pathlib.Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [FFMPEG, "-nostdin", "-v", "error", "-i", str(path),
         "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
         "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
         str(out_dir / "%d.wav")],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        if "No space left on device" in result.stderr:
            raise RuntimeError(f"ffmpeg ran out of /tmp space: {result.stderr[-500:]}")
        raise InputError("The audio couldn't be decoded. Check the file plays, then upload it again.")
    chunks = sorted(out_dir.glob("*.wav"), key=lambda p: int(p.stem))
    if [int(p.stem) for p in chunks] != list(range(len(chunks))):
        raise RuntimeError(f"ffmpeg wrote non-contiguous chunks: {[p.name for p in chunks]}")
    if len(chunks) > 1 and _seconds(chunks[-1]) < MIN_TAIL_SECONDS:  # never the only chunk
        chunks.pop().unlink()
    if not chunks:
        raise InputError("The file has no audio to transcribe.")
    return chunks


def _seconds(wav_path):
    with wave.open(str(wav_path)) as w:
        return w.getnframes() / w.getframerate()
