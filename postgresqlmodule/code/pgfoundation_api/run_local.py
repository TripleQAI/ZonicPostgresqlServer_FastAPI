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


def _explain(exc: Exception) -> None:
    """Turn a startup failure into something actionable.

    The config loader is right to fail hard on an unresolved reference — a DSN
    that silently defaults to nothing is how you end up connected to the wrong
    database. But "unresolved config reference: ${env:PGF_MAIN_DSN}" at the
    bottom of a lifespan traceback does not tell a developer *where* to set it,
    and that is the launcher's job to say.
    """
    message = str(exc)
    print(f"\n  Startup failed: {message}\n", file=sys.stderr)

    if "unresolved config reference" in message and "env:" in message:
        var = message.split("env:")[-1].rstrip("} ")
        root = Path(__file__).resolve().parents[3]
        print(
            # ASCII only: this prints to a Windows console that is usually cp1252,
            # where a section sign or em dash arrives as a replacement character
            # and makes a helpful message look like a second bug.
            f"  The config file references ${{env:{var}}}, which is not set in this\n"
            f"  environment. Credentials deliberately live outside the repository\n"
            f"  (documentations/12-deployment-targets.md, section 12.2), so set it in:\n\n"
            f"      {root / '.env'}\n\n"
            f"  Copy the template if you have not already:\n\n"
            f"      copy .env.example .env\n\n"
            f"  The VS Code launch configurations load that file automatically.\n"
            f"  No database to hand? Run the demo instead - it needs none:\n\n"
            f"      python run_demo.py\n",
            file=sys.stderr,
        )


def main() -> None:
    import uvicorn

    from pgfoundation import ConfigError, DataFoundation
    from pgfoundation_api.app import create_app
    from pgfoundation_api.settings import load_service_settings

    # Preflight, before uvicorn binds the port. A config error announced *after*
    # "Uvicorn running on..." reads like a crash; announced here it reads like
    # the instruction it actually is.
    try:
        settings = load_service_settings()
        if settings.config_path:
            names = DataFoundation.check_config(settings.config_path)
            print(f"  config OK — data sources: {', '.join(names)}")
    except ConfigError as exc:
        _explain(exc)
        raise SystemExit(2) from None
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
