import os
import sys
from pathlib import Path

# dbq reads DBQ_CONFIG at import time. Point it somewhere empty before any test
# module imports dbq, so a developer's real ~/.config/dbq never leaks in.
os.environ["DBQ_CONFIG"] = "/nonexistent/dbq-tests/config.toml"

sys.path.insert(0, str(Path(__file__).resolve().parent))
