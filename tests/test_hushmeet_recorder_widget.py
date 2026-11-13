"""Unit coverage for the HushMeet recorder widget's non-GUI behavior."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

RECORDING = importlib.import_module("hushmeet_recording")
CALENDAR = importlib.import_module("hushmeet_calendar")
ACTIVITY = importlib.import_module("hushmeet_mic_activity")
PROCESSING_STATUS = importlib.import_module("hushmeet_processing_status")


class RecordingStateTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "active.json"
        self.log_path = Path(self.temporary_directory.name) / "recorder.log"
        self.state_patch = patch.object(RECORDING, "STATE_PATH", self.state_path)
        self.log_patch = patch.object(RECORDING, "RECORDER_LOG_PATH", self.log_path)
        self.state_patch.start()
        self.log_patch.start()

    def tearDown(self) -> None:
        self.log_patch.stop()
        self.state_patch.stop()
        self.temporary_directory.cleanup()

    def test_title_category_suggestion_uses_known_category_and_sky_fallback(self) -> None:
        self.assertEqual(RECORDING.category_for_title("dSolver rollout"), "dsolver")
        self.assertEqual(RECORDING.category_for_title("Board planning"), "board")
        self.assertEqual(RECORDING.category_for_title("NFAT Spell Planning"), "sky")

    def test_recording_command_carries_explicit_audio_signal(self) -> None:
        command = RECORDING.recording_command(
            "sky", "NFAT Spell Planning", "both", "alsa_input.usb-jabra"
        )
        self.assertEqual(command[-2:], ["--audio-source", "alsa_input.usb-jabra"])
        self.assertIn("--audio-source-type", command)

    def test_stale_state_is_removed_before_widget_reads_it(self) -> None:
        self.state_path.write_text(json.dumps({"pid": 42, "title": "Old meeting"}))
        with patch.object(RECORDING, "_process_group_is_alive", return_value=False):
            self.assertIsNone(RECORDING.active_recording())
        self.assertFalse(self.state_path.exists())

    def test_start_persists_complete_widget_state(self) -> None:
        process = SimpleNamespace(pid=12345)
        with patch.object(RECORDING.subprocess, "Popen", return_value=process) as popen, patch.object(
            RECORDING.time, "sleep"
        ):
            state = RECORDING.start_recording(
                "sky",
                "NFAT Spell Planning",
                "Recorder widget",
                audio_source_type="both",
                audio_label="Microphone + system audio",
            )
        self.assertEqual(state["pid"], 12345)
        self.assertEqual(state["category_label"], "SKY")
        self.assertEqual(state["audio_source_type"], "both")
        self.assertIn("started_at", state)
        self.assertEqual(json.loads(self.state_path.read_text())["title"], "NFAT Spell Planning")
        popen.assert_called_once()

    def test_immediate_recorder_exit_does_not_publish_active_state(self) -> None:
        self.log_path.write_text("ffmpeg: No such process\n")
        process = SimpleNamespace(pid=12345, poll=lambda: 1)
        with patch.object(RECORDING.subprocess, "Popen", return_value=process), patch.object(
            RECORDING.time, "sleep"
        ):
            with self.assertRaisesRegex(RuntimeError, "ffmpeg: No such process"):
                RECORDING.start_recording("sky", "Meeting", "Recorder widget")
        self.assertFalse(self.state_path.exists())

    def test_stop_signals_group_and_clears_state(self) -> None:
        RECORDING.write_active_recording({"pid": 12345, "title": "Meeting"})
        with patch.object(RECORDING, "_process_group_is_alive", return_value=True), patch.object(
            RECORDING.os, "killpg"
        ) as kill_group, patch.object(RECORDING.subprocess, "Popen") as queue_seed:
            self.assertTrue(RECORDING.stop_recording())
        kill_group.assert_called_once()
        self.assertIn("seed-after-delay", queue_seed.call_args.args[0])
        self.assertFalse(self.state_path.exists())


class AudioInputTests(TestCase):
    def test_available_inputs_mix_system_audio_with_each_microphone(self) -> None:
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "name": "alsa_input.usb-jabra",
                        "description": "Jabra Speak microphone",
                    },
                    {
                        "name": "alsa_output.speakers.monitor",
                        "properties": {"device.description": "Laptop speakers monitor"},
                    },
                ]
            ),
        )
        with patch.object(RECORDING.subprocess, "run", return_value=response):
            inputs = RECORDING.available_audio_inputs()
        self.assertEqual([item.key for item in inputs], ["both", "source:alsa_input.usb-jabra"])
        self.assertEqual(inputs[1].source, "alsa_input.usb-jabra")
        self.assertEqual(inputs[1].source_type, "both")
        self.assertIn("system audio", inputs[1].label)
        self.assertTrue(all(item.source_type == "both" for item in inputs))

    def test_no_physical_source_blocks_the_default_recording_choice(self) -> None:
        response = SimpleNamespace(
            returncode=0,
            stdout=json.dumps([{"name": "auto_null.monitor", "description": "Dummy Output Monitor"}]),
        )
        with patch.object(RECORDING.subprocess, "run", return_value=response):
            inputs = RECORDING.available_audio_inputs()
        self.assertEqual(inputs[0].key, "unavailable")
        self.assertIn("No physical microphone", inputs[0].label)

    def test_audio_lookup_failure_blocks_silent_default_recording(self) -> None:
        with patch.object(
            RECORDING.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")
        ):
            inputs = RECORDING.available_audio_inputs()
        self.assertEqual(inputs[0].key, "unavailable")
        self.assertIn("No physical microphone", inputs[0].label)


class MicrophoneActivityTests(TestCase):
    def test_rms_level_distinguishes_silence_from_speech_level_samples(self) -> None:
        self.assertEqual(ACTIVITY.rms_level(b"\x00\x00" * 8), 0.0)
        self.assertGreater(ACTIVITY.rms_level((1_000).to_bytes(2, "little", signed=True) * 8), 250)

    def test_explicit_microphone_source_is_reused_for_activity_detection(self) -> None:
        state = {"audio_source": "alsa_input.usb-jabra"}
        self.assertEqual(ACTIVITY.microphone_source_for_recording(state), "alsa_input.usb-jabra")

    def test_monitor_output_selection_falls_back_to_default_microphone(self) -> None:
        state = {"audio_source": "alsa_output.speakers.monitor"}
        result = SimpleNamespace(returncode=0, stdout="alsa_input.default\n")
        with patch.object(ACTIVITY.subprocess, "run", return_value=result):
            self.assertEqual(ACTIVITY.microphone_source_for_recording(state), "alsa_input.default")

    def test_activity_holds_briefly_after_a_signal(self) -> None:
        monitor = ACTIVITY.MicrophoneActivityMonitor("alsa_input.default", hold_seconds=0.5)
        monitor._last_signal_at = 10.0
        with patch.object(ACTIVITY.time, "monotonic", return_value=10.4):
            self.assertTrue(monitor.is_active())
        with patch.object(ACTIVITY.time, "monotonic", return_value=10.6):
            self.assertFalse(monitor.is_active())


class ProcessingStatusTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.queue_path = Path(self.temporary_directory.name) / "process-queue.json"
        self.lock_path = Path(self.temporary_directory.name) / "process-queue.lock"
        self.queue_patch = patch.object(PROCESSING_STATUS, "QUEUE_PATH", self.queue_path)
        self.lock_patch = patch.object(PROCESSING_STATUS, "LOCK_PATH", self.lock_path)
        self.queue_patch.start()
        self.lock_patch.start()

    def tearDown(self) -> None:
        self.lock_patch.stop()
        self.queue_patch.stop()
        self.temporary_directory.cleanup()

    def test_all_unfinished_jobs_and_ten_latest_completed_jobs_are_returned(self) -> None:
        pending = [
            {"audio_file": f"/meetings/pending-{index}/audio.wav", "status": "pending", "created_at": str(index)}
            for index in range(3)
        ]
        completed = [
            {
                "audio_file": f"/meetings/finished-{index}/audio.wav",
                "status": "succeeded",
                "finished_at": f"2026-11-10T{index:02}:00:00+00:00",
            }
            for index in range(12)
        ]
        self.queue_path.write_text(json.dumps({"jobs": [*pending, *completed]}))

        unfinished, recent = PROCESSING_STATUS.processing_status()

        self.assertEqual(len(unfinished), 3)
        self.assertEqual(len(recent), 10)
        self.assertEqual(PROCESSING_STATUS.recording_name(recent[0]), "finished-11")
        self.assertEqual(PROCESSING_STATUS.recording_name(recent[-1]), "finished-2")

    def test_failed_jobs_remain_visible_with_their_error(self) -> None:
        job = {
            "audio_file": "/meetings/failed/audio.wav",
            "status": "failed",
            "category": "sky",
            "last_error": "transcription model unavailable",
        }
        self.queue_path.write_text(json.dumps({"jobs": [job]}))

        unfinished, recent = PROCESSING_STATUS.processing_status()

        self.assertEqual(unfinished, [job])
        self.assertEqual(recent, [])
        self.assertIn("transcription model unavailable", PROCESSING_STATUS.job_detail(job))

    def test_terminal_escape_sequences_are_not_rendered_in_failure_details(self) -> None:
        job = {
            "audio_file": "/meetings/failed/audio.wav",
            "status": "failed",
            "last_error": "\x1b[0;34m[INFO]\x1b[0m Transcribing\nError: model unavailable\x07",
        }

        details = PROCESSING_STATUS.job_detail(job)

        self.assertEqual(details, "SKY · failed\n[INFO] Transcribing\nError: model unavailable")
        self.assertNotIn("\x1b", details)

    def test_legacy_recording_names_are_displayed_with_a_leading_timestamp(self) -> None:
        job = {
            "audio_file": (
                "/meetings/SKY/20261109/"
                "NFAT Spell Planning — 20261109_160725/NFAT Spell Planning — 20261109_160725.wav"
            )
        }

        self.assertEqual(
            PROCESSING_STATUS.recording_name(job),
            "2026-11-09:16 — NFAT Spell Planning",
        )

    def test_wav_duration_is_rendered_as_hours_and_minutes(self) -> None:
        self.assertEqual(PROCESSING_STATUS.duration_label(Path("missing.wav")), None)
        self.assertEqual(PROCESSING_STATUS.format_duration(3_719), "01:02")


class NearbyCalendarTests(TestCase):
    def test_in_progress_event_precedes_an_equally_near_future_event(self) -> None:
        current = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)
        in_progress = {
            "summary": "Current Sky meeting",
            "start": {"dateTime": "2026-11-09T15:56:00Z"},
            "end": {"dateTime": "2026-11-09T16:04:00Z"},
        }
        future = {
            "summary": "Upcoming dSolver meeting",
            "start": {"dateTime": "2026-11-09T16:02:00Z"},
            "end": {"dateTime": "2026-11-09T17:00:00Z"},
        }
        with patch.object(CALENDAR, "calendar_events", return_value=[future, in_progress]) as events:
            ordered = CALENDAR.nearby_events({}, current)
        self.assertEqual(ordered, [in_progress, future])
        self.assertEqual(events.call_args.kwargs["before_minutes"], 5)
        self.assertEqual(events.call_args.kwargs["after_minutes"], 5)
