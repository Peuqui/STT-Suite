"""server.py reads its settings and starts loading at import: point it at a scratch directory and keep it from loading a model."""
import os
import sys
import tempfile
from pathlib import Path

_scratch = Path(tempfile.mkdtemp(prefix="stt-suite-tests-"))
os.environ["STT_CONFIG_FILE"] = str(_scratch / "config.json")
os.environ["STT_VRAM_MEASURED_FILE"] = str(_scratch / "vram_measured.json")
os.environ["STT_EAGER_LOAD"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
