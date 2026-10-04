from __future__ import annotations

import logging
import signal
import threading

from .camera import build_source
from .config import RuntimeConfig
from .processing import CorrectionSettings, ImageProcessor, SettingsStore
from .service import CaptureService
from .web import WebContext, create_server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = RuntimeConfig.from_env()
    except ValueError as exc:
        logging.getLogger(__name__).error("configuration error:\n%s", exc)
        raise SystemExit(2) from exc
    settings = SettingsStore(CorrectionSettings.from_env())
    source = build_source(config.camera)
    service = CaptureService(
        source=source,
        settings=settings,
        processor=ImageProcessor(),
        restart_delay_seconds=config.camera.restart_delay_seconds,
    )
    server = create_server(WebContext(service, settings, config.server))

    shutdown_started = threading.Event()

    def request_shutdown(_signum: int, _frame: object) -> None:
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    if hasattr(signal, "SIGHUP"):
        # The service runs detached. An SSH terminal disappearing must not be
        # interpreted as an application shutdown request.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    service.start()
    logging.getLogger(__name__).info(
        "web server listening on http://%s:%d (camera=%s)",
        config.server.host,
        config.server.port,
        source.name,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        service.stop()


if __name__ == "__main__":
    main()
