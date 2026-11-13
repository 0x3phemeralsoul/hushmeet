"""Check linked-checkout runtime selection through the real CLI wrappers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]


class LinkedCheckoutRuntimeTests(TestCase):
    def setUp(self) -> None:
        local_tmp = ROOT / "tmp"
        local_tmp.mkdir(exist_ok=True)
        self.temporary_directory = TemporaryDirectory(dir=local_tmp)
        self.primary = Path(self.temporary_directory.name) / "primary"
        self.checkout = self.primary / "tmp" / "linked"
        (self.checkout / "scripts").mkdir(parents=True)
        self.venv = self.primary / "venv"
        (self.venv / "bin").mkdir(parents=True)
        stub = self.venv / "bin/python3"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "out = Path(sys.argv[sys.argv.index('-o') + 1])\n"
            "out.write_text(json.dumps({'args': sys.argv[1:], "
            "'require_gpu': os.environ.get('REQUIRE_GPU')}))\n"
        )
        stub.chmod(0o755)
        shutil.copy2(ROOT / "hushnote", self.checkout / "hushnote")
        shutil.copy2(ROOT / "scripts/hushmeet", self.checkout / "scripts/hushmeet")
        (self.primary / ".hushnoterc").write_text(
            "WHISPER_MODEL=small\nWHISPER_LANGUAGE=en\nREQUIRE_GPU=0\n"
        )
        self.audio = self.primary / "sample audio.wav"
        self.audio.touch()
        self.env = os.environ.copy()
        for key in ("HUSHNOTE_VENV", "HUSHNOTE_CONFIG", "WHISPER_MODEL", "WHISPER_LANGUAGE", "REQUIRE_GPU"):
            self.env.pop(key, None)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_transcription(self, executable: Path) -> dict:
        subprocess.run(
            [str(executable), "transcribe", str(self.audio)],
            cwd=self.checkout,
            env=self.env,
            check=True,
            text=True,
            capture_output=True,
        )
        return json.loads(self.audio.with_suffix(".txt").read_text())

    def test_linked_hushnote_uses_shared_venv_and_config(self) -> None:
        result = self.run_transcription(self.checkout / "hushnote")
        self.assertIn("small", result["args"])
        self.assertIn("en", result["args"])
        self.assertEqual(result["require_gpu"], "0")

    def test_widget_cli_passes_the_shared_runtime_to_hushnote(self) -> None:
        result = self.run_transcription(self.checkout / "scripts/hushmeet")
        self.assertIn("small", result["args"])
        self.assertEqual(result["require_gpu"], "0")

    def test_checkout_config_takes_precedence_and_can_be_overridden(self) -> None:
        (self.checkout / ".hushnoterc").write_text("WHISPER_MODEL=base\n")
        result = self.run_transcription(self.checkout / "hushnote")
        self.assertIn("base", result["args"])
        self.env["HUSHNOTE_CONFIG"] = str(self.primary / ".hushnoterc")
        result = self.run_transcription(self.checkout / "hushnote")
        self.assertIn("small", result["args"])
