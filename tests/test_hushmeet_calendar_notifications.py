"""Regression coverage for visible HushMeet Calendar authorization failures."""

import importlib
import sys
from datetime import datetime, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def load_module(name: str, relative_path: str):
    spec = spec_from_file_location(name, ROOT / relative_path)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WATCHER = load_module("hushmeet_watcher", "scripts/hushmeet-watcher.py")
NATIVE_HOST = load_module("hushmeet_native_host", "scripts/hushmeet-native-host.py")
AUTH = load_module("hushmeet_calendar_auth", "scripts/hushmeet-calendar-auth.py")
POLICY = importlib.import_module("hushmeet_calendar_policy")


class CalendarNotificationTests(TestCase):
    def test_revoked_grant_has_a_reauthorization_hint(self):
        error = RuntimeError("invalid_grant: Token has been expired or revoked.")

        self.assertEqual(
            WATCHER.calendar_error_notice(error),
            "Google Calendar access needs reauthorization. Run: hushmeet-calendar-auth",
        )
        self.assertEqual(
            NATIVE_HOST.calendar_auth_notice(str(error)),
            "Google Calendar access needs reauthorization. Run: hushmeet-calendar-auth",
        )

    def test_unrelated_meet_error_does_not_trigger_auth_notification(self):
        self.assertIsNone(
            NATIVE_HOST.calendar_auth_notice(
                "No current Calendar event matched this Google Meet URL; no recording started."
            )
        )

    def test_health_check_notifies_once_for_a_repeated_error(self):
        error = RuntimeError("invalid_grant: Token has been expired or revoked.")
        with patch.object(WATCHER, "now_events", side_effect=error), patch.object(
            WATCHER, "send_notification"
        ) as notify:
            first_error, first_notification = WATCHER.check_calendar_health(
                {}, None, 0.0, 100.0, 3600
            )
            second_error, second_notification = WATCHER.check_calendar_health(
                {}, first_error, first_notification, 200.0, 3600
            )

        self.assertEqual(first_error, second_error)
        self.assertEqual(first_notification, 100.0)
        self.assertEqual(second_notification, 100.0)
        notify.assert_called_once()

    def test_health_check_recovery_clears_the_failure_state(self):
        with patch.object(WATCHER, "now_events", return_value=[]), patch.object(
            WATCHER, "send_notification"
        ) as notify:
            error, notification_time = WATCHER.check_calendar_health(
                {}, "previous error", 100.0, 200.0, 3600
            )

        self.assertIsNone(error)
        self.assertEqual(notification_time, 0.0)
        notify.assert_not_called()

    def test_unbound_account_has_a_reauthorization_hint(self):
        error = RuntimeError("Google Calendar authorization is not bound to the allowed account.")

        self.assertEqual(
            WATCHER.calendar_error_notice(error),
            "Google Calendar access needs reauthorization. Run: hushmeet-calendar-auth",
        )
        self.assertEqual(
            NATIVE_HOST.calendar_auth_notice(str(error)),
            "Google Calendar access needs reauthorization. Run: hushmeet-calendar-auth",
        )


class CalendarAccountBindingTests(TestCase):
    def setUp(self):
        self.temporary_directory = TemporaryDirectory()
        self.state_path = Path(self.temporary_directory.name) / "authorized-account.json"
        self.state_path_patch = patch.object(POLICY, "AUTHORIZED_ACCOUNT_STATE_PATH", self.state_path)
        self.state_path_patch.start()

    def tearDown(self):
        self.state_path_patch.stop()
        self.temporary_directory.cleanup()

    def test_authorized_token_is_bound_to_the_exact_configured_account(self):
        config = {"allowed_google_account": "0x3phemeralsoul@dewiz.xyz"}
        POLICY.write_authorized_account_state("0X3PHEMERALSOUL@dewiz.xyz", "refresh-token")

        self.assertEqual(
            POLICY.verify_authorized_account_state(config, "refresh-token"),
            "0x3phemeralsoul@dewiz.xyz",
        )
        self.assertEqual(self.state_path.stat().st_mode & 0o777, 0o600)

    def test_different_token_or_account_fails_closed(self):
        config = {"allowed_google_account": "0x3phemeralsoul@dewiz.xyz"}
        POLICY.write_authorized_account_state("0x3phemeralsoul@dewiz.xyz", "refresh-token")

        with self.assertRaisesRegex(RuntimeError, "not bound to the allowed account"):
            POLICY.verify_authorized_account_state(config, "other-refresh-token")
        with self.assertRaisesRegex(RuntimeError, "not bound to the allowed account"):
            POLICY.verify_authorized_account_state(
                {"allowed_google_account": "another-account@dewiz.xyz"}, "refresh-token"
            )

    def test_authorization_requires_a_configured_account(self):
        with self.assertRaisesRegex(RuntimeError, "Missing allowed_google_account"):
            POLICY.allowed_google_account({})

    def test_testing_token_renewal_is_due_before_the_seven_day_expiry(self):
        authorized_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
        POLICY.write_authorized_account_state(
            "0x3phemeralsoul@dewiz.xyz", "refresh-token", authorized_at
        )
        config = {"calendar_reauthorize_after_days": 6}

        self.assertFalse(
            POLICY.calendar_reauthorization_due(
                config, datetime(2026, 8, 6, 23, 59, tzinfo=timezone.utc)
            )
        )
        self.assertTrue(
            POLICY.calendar_reauthorization_due(
                config, datetime(2026, 8, 7, tzinfo=timezone.utc)
            )
        )

    def test_oauth_identity_must_match_the_allowed_account(self):
        credentials = SimpleNamespace(id_token="id-token", client_id="client-id")
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value={
            "email": "0x3phemeralsoul@dewiz.xyz",
            "email_verified": True,
        }):
            account = AUTH.verified_google_account(credentials, "0x3phemeralsoul@dewiz.xyz")

        self.assertEqual(account, "0x3phemeralsoul@dewiz.xyz")

    def test_oauth_identity_for_a_different_account_is_rejected(self):
        credentials = SimpleNamespace(id_token="id-token", client_id="client-id")
        with patch("google.oauth2.id_token.verify_oauth2_token", return_value={
            "email": "another-account@dewiz.xyz",
            "email_verified": True,
        }):
            with self.assertRaisesRegex(SystemExit, "not authorized"):
                AUTH.verified_google_account(credentials, "0x3phemeralsoul@dewiz.xyz")


class CalendarTestingReminderTests(TestCase):
    def test_due_testing_token_shows_a_rate_limited_notification(self):
        notice = "HushMeet Calendar test authorization is due for renewal. Run: hushmeet-calendar-auth"
        with patch.object(WATCHER, "now_events", return_value=[]), patch.object(
            WATCHER, "calendar_reauthorization_notice", return_value=notice
        ), patch.object(WATCHER, "send_notification") as notify:
            first_error, first_notification = WATCHER.check_calendar_health(
                {}, None, 0.0, 100.0, 3600, 86400
            )
            second_error, second_notification = WATCHER.check_calendar_health(
                {}, first_error, first_notification, 200.0, 3600, 86400
            )

        self.assertEqual(first_error, notice)
        self.assertEqual(second_error, notice)
        self.assertEqual(first_notification, 100.0)
        self.assertEqual(second_notification, 100.0)
        notify.assert_called_once_with("HushMeet Calendar needs attention", notice)


class DiscordVoiceDetectionTests(TestCase):
    DISCORD_COMMUNICATION_OUTPUT = '''Sink Input #42
\tProperties:
\t\tapplication.name = "Discord"
\t\tapplication.process.binary = "Discord"
\t\tmedia.role = "communication"
'''
    DISCORD_COMMUNICATION_INPUT = '''Source Output #24
\tProperties:
\t\tapplication.name = "Discord"
\t\tapplication.process.binary = "Discord"
\t\tmedia.role = "communication"
'''

    def test_discord_notification_playback_is_not_a_voice_call(self):
        notification_output = '''Sink Input #42
\tProperties:
\t\tapplication.name = "Discord"
\t\tapplication.process.binary = "Discord"
\t\tmedia.role = "event"
'''
        with patch.object(
            WATCHER.subprocess,
            "run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout=notification_output),
                SimpleNamespace(returncode=0, stdout=""),
            ],
        ):
            self.assertFalse(WATCHER.discord_in_voice())

    def test_discord_playback_without_microphone_capture_is_not_a_voice_call(self):
        with patch.object(
            WATCHER.subprocess,
            "run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout=self.DISCORD_COMMUNICATION_OUTPUT),
                SimpleNamespace(returncode=0, stdout=""),
            ],
        ):
            self.assertFalse(WATCHER.discord_in_voice())

    def test_matching_discord_communication_playback_and_capture_is_a_voice_call(self):
        with patch.object(
            WATCHER.subprocess,
            "run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout=self.DISCORD_COMMUNICATION_OUTPUT),
                SimpleNamespace(returncode=0, stdout=self.DISCORD_COMMUNICATION_INPUT),
            ],
        ):
            self.assertTrue(WATCHER.discord_in_voice())
