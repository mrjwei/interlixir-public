"""media.py against real ffmpeg/ffprobe on synthetic inputs (skipped if they aren't on PATH)."""

import pathlib
import shutil
import subprocess
import tempfile
import unittest
import wave

import media

HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def make(path, *args):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args, str(path)], check=True)
    return path


def tone(path, seconds, rate=44100, channels=2):
    """A stereo 44.1 kHz AAC file (.m4a): a typical phone recording, unlike what Whisper wants."""
    return make(path, "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate={rate}:duration={seconds}",
                "-ac", str(channels), "-c:a", "aac")


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe not installed")
class MediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_probe_duration(self):
        self.assertAlmostEqual(media.probe(tone(self.tmp / "a.m4a", 25), 30), 25, delta=0.2)

    def test_split_normalises_and_chunks(self):
        chunks = media.split(tone(self.tmp / "a.m4a", 25), self.tmp / "out", 10, 60)
        self.assertEqual([p.name for p in chunks], ["0.wav", "1.wav", "2.wav"])
        lengths = []
        for path in chunks:
            with wave.open(str(path)) as w:
                self.assertEqual((w.getnchannels(), w.getframerate(), w.getsampwidth()), (1, 16000, 2))
                lengths.append(w.getnframes() / w.getframerate())
        self.assertAlmostEqual(lengths[0], 10, delta=0.1)
        self.assertAlmostEqual(lengths[1], 10, delta=0.1)
        self.assertAlmostEqual(sum(lengths), 25, delta=0.2)

    def test_padding_tail_is_dropped(self):
        # AAC padding makes 30 s decode a few samples long: 3 chunks, not 3 plus an empty 4th.
        chunks = media.split(tone(self.tmp / "a.m4a", 30), self.tmp / "out", 10, 60)
        self.assertEqual([p.name for p in chunks], ["0.wav", "1.wav", "2.wav"])
        self.assertFalse((self.tmp / "out" / "3.wav").exists())

    def test_short_recording_is_one_chunk(self):
        chunks = media.split(tone(self.tmp / "a.m4a", 0.5), self.tmp / "out", 10, 60)
        self.assertEqual([p.name for p in chunks], ["0.wav"])  # under MIN_TAIL_SECONDS, but the only chunk

    def test_video_keeps_only_the_audio(self):
        video = make(self.tmp / "v.mp4", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5:duration=12",
                     "-f", "lavfi", "-i", "sine=duration=12", "-shortest", "-c:v", "libx264", "-c:a", "aac")
        chunks = media.split(video, self.tmp / "out", 5, 60)
        self.assertEqual(len(chunks), 3)
        with wave.open(str(chunks[0])) as w:
            self.assertEqual(w.getnchannels(), 1)

    def test_video_without_audio(self):
        video = make(self.tmp / "v.mp4", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=5:duration=2", "-c:v", "libx264")
        with self.assertRaisesRegex(media.InputError, "no audio track"):
            media.probe(video, 30)

    def test_not_media(self):
        junk = self.tmp / "junk.mp3"
        junk.write_bytes(b"this is not audio" * 100)
        with self.assertRaisesRegex(media.InputError, "couldn't be read"):
            media.probe(junk, 30)

    def test_split_of_undecodable_input(self):
        junk = self.tmp / "junk.wav"
        junk.write_bytes(b"RIFF" + b"\0" * 100)
        with self.assertRaises(media.InputError):
            media.split(junk, self.tmp / "out", 10, 30)


if __name__ == "__main__":
    unittest.main()
