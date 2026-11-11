"""Queue lifecycle tests for intentionally skipped recordings."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hushmeet_queue", ROOT / "scripts/hushmeet-queue.py")
assert SPEC and SPEC.loader
QUEUE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUEUE)


class QueueDismissalTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.queue_path = self.directory / "process-queue.json"
        self.lock_path = self.directory / "process-queue.lock"
        self.audio = self.directory / "silent.wav"
        self.audio.touch()
        self.queue_patch = patch.object(QUEUE, "QUEUE_PATH", self.queue_path)
        self.lock_patch = patch.object(QUEUE, "LOCK_PATH", self.lock_path)
        self.queue_patch.start()
        self.lock_patch.start()

    def tearDown(self) -> None:
        self.lock_patch.stop()
        self.queue_patch.stop()
        self.temporary_directory.cleanup()

    def test_dismissed_audio_is_retained_but_not_requeued(self) -> None:
        self.assertTrue(QUEUE.upsert_job(self.audio, "sky"))
        self.assertTrue(QUEUE.dismiss_job(self.audio))

        self.assertFalse(QUEUE.upsert_job(self.audio, "sky"))
        self.assertEqual(QUEUE.pending_jobs(), [])
        self.assertEqual(QUEUE.load_queue()["jobs"][0]["status"], "ignored")
