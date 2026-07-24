from typing import Any, Dict

from ai.web import WebServer

from .venv_pipe import venv_pipe


def initModule(server: WebServer, config: Dict[str, Any]):
    """Register the venv bridge route on a venv child engine's web server.

    Route WS ``/venv/pipe``: a main-side ``venv`` client connects (per channel) to feed
    bridged lane data into, or drain results out of, the child's local pipeline. The
    resident ``venv_source_stub`` source mounts this module (``use('venv')``) after
    publishing its pipeline target as ``app.state.target``; the route reaches the pipe stack
    through that.
    """
    server.app.router.add_api_websocket_route('/venv/pipe', venv_pipe)
