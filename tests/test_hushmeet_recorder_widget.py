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


class RecordingStateTests(TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "active.json"
        self.state_patch = patch.object(RECORDING, "STATE_PATH", self.state_path)
        self.state_patch.start()

    def tearDown(self) -> None:
        self.state_patch.stop()
        self.temporary_directory.cleanup()

    def test_title_category_suggestion_uses_known_category_and_sky_fallback(self) -> None:
        self.assertEqual(RECORDING.category_for_title("dSolver rollout"), "dsolver")
        self.assertEqual(RECORDING.category_for_title("Board planning"), "board")
        self.assertEqual(RECORDING.category_for_title("NFAT Spell Planning"), "sky")

    def test_recording_command_carries_explicit_audio_signal(self) -> None:
        command = RECORDING.recording_command(
            "sky", "NFAT Spell Planning", "microphone", "alsa_input.usb-jabra"
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
        with patch.object(RECORDING.subprocess, "Popen", return_value=process) as popen:
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

    def test_stop_signals_group_and_clears_state(self) -> None:
        RECORDING.write_active_recording({"pid": 12345, "title": "Meeting"})
        with patch.object(RECORDING, "_process_group_is_alive", return_value=True), patch.object(
            RECORDING.os, "killpg"
        ) as kill_group:
            self.assertTrue(RECORDING.stop_recording())
        kill_group.assert_called_once()
        self.assertFalse(self.state_path.exists())


class AudioInputTests(TestCase):
    def test_available_inputs_keep_standard_modes_and_add_every_signal(self) -> None:
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
        self.assertEqual([item.key for item in inputs[:3]], ["microphone", "monitor", "both"])
        self.assertEqual(inputs[3].source, "alsa_input.usb-jabra")
        self.assertEqual(inputs[4].source, "alsa_output.speakers.monitor")

    def test_audio_lookup_failure_keeps_microphone_available(self) -> None:
        with patch.object(
            RECORDING.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="")
        ):
            inputs = RECORDING.available_audio_inputs()
        self.assertEqual(inputs[0].label, "Default microphone")


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
