"""server.py reads its settings and starts loading at import: point it at a scratch directory and keep it from loading a model."""
import os
import sys
import tempfile
from pathlib import Path

_scratch = Path(tempfile.mkdtemp(prefix="stt-suite-tests-"))
os.environ["STT_CONFIG_FILE"] = str(_scratch / "config.json")
os.environ["STT_VRAM_MEASURED_FILE"] = str(_scratch / "vram_measured.json")
os.environ["STT_EAGER_LOAD"] = "0"
# This machine only, with a token (the commands that change something need it): the tests that need other
# rules set them themselves (test_auth.py).
os.environ["STT_HOST"] = "127.0.0.1"
os.environ["STT_PORT"] = "5080"
(_scratch / "token").write_text("test-token\n", encoding="utf-8")
os.environ["STT_TOKEN_FILE"] = str(_scratch / "token")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
