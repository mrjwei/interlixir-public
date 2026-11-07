"""faster-whisper wrapper: one set of decoding settings, shared by the worker and bench.py, so the
benchmark measures exactly what runs in the task.

English only: the model is a distil-whisper English checkpoint, and the language is fixed to "en", so
there is no detection pass. CPU with int8 weights (Fargate has no GPU).
"""

import os
import time

MODEL_NAME = os.environ.get("MODEL_NAME", "distil-small.en")
# Baked into the image at build time (Dockerfile). Locally, a model name works too: it's downloaded once into
# the Hugging Face cache.
MODEL_DIR = os.environ.get("MODEL_DIR", MODEL_NAME)
BEAM_SIZE = int(os.environ.get("BEAM_SIZE", "5"))
# Inference threads; unset means one per vCPU of the task (task_vcpus()).
CPU_THREADS = int(os.environ.get("CPU_THREADS", "0"))


def task_vcpus():
    """One inference thread per vCPU the task is given. A container sees every core of the host, and a CPU
    limit is a quota rather than fewer cores, so on ECS the size comes from the task metadata endpoint
    (Limits.CPU, in vCPUs). Left at 0, CTranslate2 would use 4 threads whatever the task size."""
    if CPU_THREADS:
        return CPU_THREADS
    metadata = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if metadata:
        import json
        import urllib.request

        try:
            with urllib.request.urlopen(f"{metadata}/task", timeout=2) as response:
                return max(1, round(float(json.load(response)["Limits"]["CPU"])))
        except Exception:
            pass
    return len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()


class Stopped(Exception):
    """should_stop() turned true between two segments (SIGTERM): the chunk was abandoned, not finished."""


def load_wav(path):
    """A chunk as Whisper's input: float32 samples at 16 kHz. The job creator writes every chunk as 16 kHz mono
    16-bit PCM WAV, so it's read directly. faster-whisper's own decoder (PyAV) isn't used: 1.2.1 passes
    PyAV an argument that PyAV 19 no longer accepts."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) != (16000, 1, 2):
            raise ValueError(f"{path} is not 16 kHz mono 16-bit WAV")
        frames = wav.readframes(wav.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


class Transcriber:
    def __init__(self, model=MODEL_DIR, name=MODEL_NAME, cpu_threads=None, beam_size=BEAM_SIZE):
        from faster_whisper import WhisperModel

        cpu_threads = cpu_threads or task_vcpus()

        started = time.monotonic()
        self.model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=cpu_threads)
        self.load_seconds = time.monotonic() - started
        self.name, self.beam_size, self.cpu_threads = name, beam_size, cpu_threads

    def transcribe(self, path, start_seconds=0, should_stop=lambda: False):
        """Transcribe one chunk WAV (load_wav). Segment times are absolute: offset by where the chunk starts in
        the recording (the message's startSeconds), so merged chunks keep the recording's timeline."""
        segments, info = self.model.transcribe(
            load_wav(path),
            language="en",
            beam_size=self.beam_size,
            # Recommended for distil-whisper: conditioning on the previous window makes it repeat itself.
            condition_on_previous_text=False,
            # Silero VAD skips silence: faster, and Whisper hallucinates text ("Thank you.") on silence.
            vad_filter=True,
        )
        out = []
        for segment in segments:  # a generator: decoding happens while iterating, so stopping here is quick
            if should_stop():
                raise Stopped()
            text = segment.text.strip()
            if text:
                out.append({
                    "start": round(start_seconds + segment.start, 2),
                    "end": round(start_seconds + segment.end, 2),
                    "text": text,
                })
        return {
            "audioSeconds": round(info.duration, 2),
            "text": " ".join(s["text"] for s in out),
            "segments": out,
        }
