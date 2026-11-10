"""Local microphone activity detection for the HushMeet status indicator."""

from __future__ import annotations

import math
import subprocess
import threading
import time
from array import array
from pathlib import Path
from typing import Any


SAMPLE_RATE = 8_000
CHUNK_BYTES = 1_600
MIN_SIGNAL_RMS = 250
ACTIVITY_HOLD_SECONDS = 0.5


def microphone_source_for_recording(state: dict[str, Any]) -> str:
    """Select the microphone source even when recording also captures output audio."""
    source = state.get("audio_source")
    if isinstance(source, str) and source and not source.endswith(".monitor"):
        return source
    result = subprocess.run(
        ["pactl", "get-default-source"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return "@DEFAULT_SOURCE@"


def rms_level(raw_audio: bytes) -> float:
    """Return the root-mean-square amplitude of little-endian s16 PCM audio."""
    if len(raw_audio) < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(raw_audio[: len(raw_audio) - len(raw_audio) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


class MicrophoneActivityMonitor:
    """Read a low-rate local PulseAudio stream without saving any audio data."""

    def __init__(
        self,
        source: str,
        *,
        threshold: float = MIN_SIGNAL_RMS,
        hold_seconds: float = ACTIVITY_HOLD_SECONDS,
    ) -> None:
        self.source = source
        self.threshold = threshold
        self.hold_seconds = hold_seconds
        self._process: subprocess.Popen[bytes] | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._last_signal_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        try:
            self._process = subprocess.Popen(
                [
                    "parec",
                    "--raw",
                    "--format=s16le",
                    f"--rate={SAMPLE_RATE}",
                    "--channels=1",
                    "--latency-msec=100",
                    "--device",
                    self.source,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            self._process = None
            return
        self._thread = threading.Thread(target=self._read, name="hushmeet-mic-activity", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self._process and self._process.stdout
        while not self._stop_event.is_set():
            chunk = self._process.stdout.read(CHUNK_BYTES)
            if not chunk:
                return
            if rms_level(chunk) >= self.threshold:
                with self._lock:
                    self._last_signal_at = time.monotonic()

    def is_active(self) -> bool:
        with self._lock:
            return time.monotonic() - self._last_signal_at <= self.hold_seconds

    def stop(self) -> None:
        self._stop_event.set()
        if self._process and self._process.poll() is None:
            self._process.terminate()
        if self._thread:
            self._thread.join(timeout=1)
        self._process = None
        self._thread = None
