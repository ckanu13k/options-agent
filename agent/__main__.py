"""CLI entrypoint: `python -m agent` runs a single trading pass."""
import json
import sys

from .orchestrator import run_once

if __name__ == "__main__":
    result = run_once()
    # Emit a concise JSON summary so GitHub Actions logs are grep-friendly
    print(json.dumps(result, indent=2, default=str))
    # Non-zero exit only on hard errors (AppConfig.from_env will raise);
    # a "no setups" day is normal and should not fail the workflow.
    sys.exit(0)
