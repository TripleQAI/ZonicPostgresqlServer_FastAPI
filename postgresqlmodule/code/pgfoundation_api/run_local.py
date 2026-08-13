"""Local dev launcher.

    python -m pgfoundation_api.run_local

Honours PGF_CONFIG, PGF_PROFILE, PGF_SERVER__HOST, PGF_SERVER__PORT.

**Windows note.** Async psycopg cannot run on the Proactor event loop, and
uvicorn's own ``run()`` installs one. So the server is built and driven inside a
SelectorEventLoop we create, bypassing uvicorn's loop setup. This is the same
guard the Django runner carried, and it is not optional here — Windows is the
primary development environment.
"""

from __future__ import annotations

import asyncio
import selectors
import sys
from pathlib import Path

# No installable package: put the code root on sys.path so this works from any
# cwd and as either `-m pgfoundation_api.run_local` or a direct file run.
_CODE_ROOT = str(Path(__file__).resolve().parents[1])
if _CODE_ROOT not in sys.path:
    sys.path.insert(0, _CODE_ROOT)


def main() -> None:
    import uvicorn

    from pgfoundation_api.app import create_app
    from pgfoundation_api.settings import load_service_settings

    settings = load_service_settings()
    config = uvicorn.Config(
        create_app(settings),
        host=settings.server.host,
        port=settings.server.port,
        log_level=settings.server.log_level,
        lifespan="on",
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    def _selector_loop() -> asyncio.AbstractEventLoop:
        return asyncio.SelectorEventLoop(selectors.SelectSelector())

    loop_factory = _selector_loop if sys.platform == "win32" else None
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(server.serve())


if __name__ == "__main__":
    main()
