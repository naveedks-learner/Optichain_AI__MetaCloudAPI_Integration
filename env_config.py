from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


ENV_FILE = Path(__file__).resolve().with_name(".env")

# Existing process environment variables take precedence over the local file.
load_dotenv(dotenv_path=ENV_FILE, override=False)
