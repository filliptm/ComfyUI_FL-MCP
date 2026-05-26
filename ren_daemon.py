"""Run Ren as a standalone backend daemon.

This mode keeps Ren alive independently from ComfyUI so Ren can supervise,
restart, and inspect Comfy without being killed by the Comfy process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT))


def main() -> None:
    os.environ["FL_REN_MODE"] = "daemon"
    from config import settings

    uvicorn.run(
        "server:app",
        host=settings.ws_host,
        port=settings.ws_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
