"""Queue lifecycle tests for intentionally skipped recordings."""

from __future__ import annotations

import importlib.util
import os
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

    def test_processing_one_selected_job_leaves_other_jobs_queued(self) -> None:
        other_audio = self.directory / "other.wav"
        other_audio.touch()
        self.assertTrue(QUEUE.upsert_job(self.audio, "sky"))
        self.assertTrue(QUEUE.upsert_job(other_audio, "dsolver"))

        with patch.object(QUEUE, "process_job") as process_job:
            self.assertEqual(QUEUE.process_one(str(self.audio)), 0)

        process_job.assert_called_once()
        jobs = QUEUE.load_queue()["jobs"]
        self.assertEqual(jobs[0]["status"], "succeeded")
        self.assertEqual(jobs[1]["status"], "pending")


class QueueCredentialTests(TestCase):
    def setUp(self) -> None:
        local_tmp = ROOT / "tmp"
        local_tmp.mkdir(exist_ok=True)
        self.temporary_directory = TemporaryDirectory(dir=local_tmp)
        self.env_path = Path(self.temporary_directory.name) / "openai.env"
        self.env_patch = patch.object(QUEUE, "OPENAI_ENV_PATH", self.env_path)
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.temporary_directory.cleanup()

    def test_desktop_queue_uses_the_nightly_service_credential_file(self) -> None:
        self.env_path.write_text('OPENAI_API_KEY="test-key-from-file"\n')
        with patch.dict(os.environ, {}, clear=True):
            env = QUEUE.summary_environment("sky")
        self.assertEqual(env["OPENAI_API_KEY"], "test-key-from-file")
        self.assertEqual(env["HUSHNOTE_CATEGORY"], "sky")
        self.assertEqual(env["HUSHNOTE_TRANSCRIBE_FORMAT"], "md")

    def test_inherited_credential_takes_precedence(self) -> None:
        self.env_path.write_text("OPENAI_API_KEY=test-key-from-file\n")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "inherited-key"}, clear=True):
            env = QUEUE.summary_environment("dsolver")
        self.assertEqual(env["OPENAI_API_KEY"], "inherited-key")

    def test_credential_file_is_parsed_without_executing_content(self) -> None:
        marker = self.env_path.parent / "must-not-exist"
        self.env_path.write_text(f'OPENAI_API_KEY="$(touch {marker})"\n')
        with patch.dict(os.environ, {}, clear=True):
            env = QUEUE.summary_environment("sky")
        self.assertEqual(env["OPENAI_API_KEY"], f"$(touch {marker})")
        self.assertFalse(marker.exists())
