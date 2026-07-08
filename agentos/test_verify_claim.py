#!/usr/bin/env python3
"""
Unit tests for verify_claim (Hermes HQ Phase 1, P1-A, GATE 1a).

Run: python -m unittest test_verify_claim -v
"""

import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from verify_claim import verify_claim


class TestFileRef(unittest.TestCase):
    def test_true_ref_existing_file(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"hello")
            path = f.name
        try:
            result = verify_claim(f"file:{path}")
            self.assertTrue(result["ok"])
            self.assertIn("sha256=", result["evidence"])
        finally:
            Path(path).unlink()

    def test_false_ref_missing_file(self):
        result = verify_claim(r"file:C:\definitely\does\not\exist\nope.txt")
        self.assertFalse(result["ok"])
        self.assertIn("not found", result["evidence"])


class TestCommandRef(unittest.TestCase):
    def test_true_ref_succeeding_command(self):
        cmd = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
        result = verify_claim(f"cmd:{cmd}")
        self.assertTrue(result["ok"])
        self.assertIn("exit=0", result["evidence"])

    def test_false_ref_failing_command(self):
        cmd = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
        result = verify_claim(f"cmd:{cmd}")
        self.assertFalse(result["ok"])
        self.assertIn("exit=1", result["evidence"])


class TestUrlRef(unittest.TestCase):
    def test_true_ref_live_url(self):
        fake_resp = mock.MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = mock.Mock(return_value=fake_resp)
        fake_resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            result = verify_claim("url:https://example.com/healthy")
        self.assertTrue(result["ok"])
        self.assertIn("status=200", result["evidence"])

    def test_false_ref_dead_url(self):
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("name resolution failed"),
        ):
            result = verify_claim("url:https://this.domain.does.not.exist.invalid/")
        self.assertFalse(result["ok"])
        self.assertIn("unreachable", result["evidence"])


class TestMisc(unittest.TestCase):
    def test_empty_ref(self):
        result = verify_claim("")
        self.assertFalse(result["ok"])

    def test_schemeless_url_autodetect(self):
        fake_resp = mock.MagicMock()
        fake_resp.status = 200
        fake_resp.__enter__ = mock.Mock(return_value=fake_resp)
        fake_resp.__exit__ = mock.Mock(return_value=False)
        with mock.patch("urllib.request.urlopen", return_value=fake_resp):
            result = verify_claim("https://example.com/no-scheme-prefix")
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
