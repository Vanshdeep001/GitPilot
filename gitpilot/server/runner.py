"""Daemon runner — starts the webhook server and the orchestrator together.

This is the entry point launched as a detached background process by
``gitpilot start``. It runs the FastAPI server (uvicorn) on the main thread and
the orchestrator event loop on a daemon thread.
"""

from __future__ import annotations

import logging
import threading

DEFAULT_PORT = 8787


def _start_orchestrator() -> None:
    """Run the orchestrator loop; never let it kill the process."""
    try:
        from gitpilot.agent.orchestrator import Orchestrator

        Orchestrator().run()
    except Exception:  # noqa: BLE001
        logging.getLogger("gitpilot.runner").exception("Orchestrator crashed")


def run(host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logger = logging.getLogger("gitpilot.runner")

    orchestrator_thread = threading.Thread(
        target=_start_orchestrator, name="orchestrator", daemon=True
    )
    orchestrator_thread.start()
    logger.info("Orchestrator thread started")

    import uvicorn

    logger.info("Starting webhook server on %s:%s", host, port)
    uvicorn.run("gitpilot.server.webhook:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
