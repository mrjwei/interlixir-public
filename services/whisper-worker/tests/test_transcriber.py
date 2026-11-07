"""transcriber.py: the WAV reader always; the real model only when its weights are already in the local Hugging
Face cache (bench.py downloads them) and macOS `say` is there to make speech, so tests never download."""

import io
import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
import wave
from unittest import mock

import transcriber


def write_wav(path, rate=16000, channels=1, width=2, frames=1600):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(width)
        out.setframerate(rate)
        out.writeframes(b"\x00\x40" * frames * channels)


class LoadWavTest(unittest.TestCase):
    def test_reads_the_chunk_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "c.wav"
            write_wav(path)
            samples = transcriber.load_wav(path)
        self.assertEqual((samples.dtype.name, samples.shape), ("float32", (1600,)))
        self.assertAlmostEqual(float(samples[0]), 0x4000 / 32768)

    def test_rejects_anything_else(self):
        for kwargs in ({"rate": 44100}, {"channels": 2}, {"width": 4}):
            with self.subTest(**kwargs), tempfile.TemporaryDirectory() as tmp:
                path = pathlib.Path(tmp) / "c.wav"
                write_wav(path, **kwargs)
                with self.assertRaises(ValueError):
                    transcriber.load_wav(path)


class TaskVcpusTest(unittest.TestCase):
    def test_from_the_task_metadata(self):
        body = io.BytesIO(json.dumps({"Limits": {"CPU": 2, "Memory": 4096}}).encode())
        with mock.patch.dict(os.environ, {"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2/v4/x"}), \
                mock.patch.object(transcriber, "CPU_THREADS", 0), \
                mock.patch("urllib.request.urlopen", return_value=body) as urlopen:
            self.assertEqual(transcriber.task_vcpus(), 2)
        self.assertEqual(urlopen.call_args.args[0], "http://169.254.170.2/v4/x/task")

    def test_half_a_vcpu_is_one_thread(self):
        body = io.BytesIO(json.dumps({"Limits": {"CPU": 0.5}}).encode())
        with mock.patch.dict(os.environ, {"ECS_CONTAINER_METADATA_URI_V4": "http://x"}), \
                mock.patch.object(transcriber, "CPU_THREADS", 0), mock.patch("urllib.request.urlopen", return_value=body):
            self.assertEqual(transcriber.task_vcpus(), 1)

    def test_override_wins(self):
        with mock.patch.object(transcriber, "CPU_THREADS", 3):
            self.assertEqual(transcriber.task_vcpus(), 3)

    def test_outside_ecs(self):
        with mock.patch.dict(os.environ, clear=False) as env, mock.patch.object(transcriber, "CPU_THREADS", 0):
            env.pop("ECS_CONTAINER_METADATA_URI_V4", None)
            self.assertGreaterEqual(transcriber.task_vcpus(), 1)


def _cached_model():
    try:
        from faster_whisper import download_model

        return download_model(transcriber.MODEL_NAME, local_files_only=True)
    except Exception:
        return None


@unittest.skipUnless(shutil.which("say") and shutil.which("ffmpeg"), "needs macOS say and ffmpeg")
class RealModelTest(unittest.TestCase):
    def setUp(self):
        self.model = _cached_model()
        if self.model is None:
            self.skipTest(f"{transcriber.MODEL_NAME} isn't in the Hugging Face cache (run make bench-whisper once)")

    def test_transcribes_english_speech_with_absolute_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            aiff, wav = pathlib.Path(tmp) / "s.aiff", pathlib.Path(tmp) / "s.wav"
            subprocess.run(["say", "-v", "Karen", "-o", str(aiff), "The search bar was hidden behind the menu."], check=True)
            subprocess.run(["ffmpeg", "-y", "-i", str(aiff), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                           check=True, capture_output=True)
            out = transcriber.Transcriber(model=self.model).transcribe(wav, start_seconds=600)
        self.assertIn("search bar", out["text"].lower())
        self.assertGreaterEqual(out["segments"][0]["start"], 600)

    def test_should_stop_abandons_the_chunk(self):
        with tempfile.TemporaryDirectory() as tmp:
            aiff, wav = pathlib.Path(tmp) / "s.aiff", pathlib.Path(tmp) / "s.wav"
            subprocess.run(["say", "-o", str(aiff), "One sentence. Then another sentence after a pause."], check=True)
            subprocess.run(["ffmpeg", "-y", "-i", str(aiff), "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
                           check=True, capture_output=True)
            with self.assertRaises(transcriber.Stopped):
                transcriber.Transcriber(model=self.model).transcribe(wav, should_stop=lambda: True)


if __name__ == "__main__":
    unittest.main()
