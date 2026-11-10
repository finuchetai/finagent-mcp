"""Run standard-library diagnostics before installing this standalone package."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from finance_mcp.diagnostics import main

if __name__ == "__main__":
    main()
