import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/build-native-padding.py"
SPEC = importlib.util.spec_from_file_location("build_native_padding", SCRIPT)
BUILDER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILDER)


class AtomicMarkerTests(unittest.TestCase):
    def test_failed_write_preserves_existing_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "owner.json"
            marker.write_text('{"owner":"existing"}\n')
            with patch.object(BUILDER.os, "fsync", side_effect=OSError("simulated write failure")):
                with self.assertRaises(OSError):
                    BUILDER.atomic_write_marker(marker, '{"owner":"replacement"}\n')
            self.assertEqual(marker.read_text(), '{"owner":"existing"}\n')


if __name__ == "__main__":
    unittest.main()
