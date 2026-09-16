from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from herdr_shell.actions import manifest

(root / "herdr-plugin.toml").write_text(manifest())
