# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
# =============================================================================

import threading
from typing import Any, Callable, Dict

from rocketlib import IEndpointBase, monitorStatus, debug


class IEndpoint(IEndpointBase):
    """Resident source for a virtual-environment child process.

    A venv child is spawned per run to execute one isolated pipeline group; its bridged
    lane data arrives over a loopback WebSocket, not from a scan. This source keeps the
    child engine alive for the run and mounts that transport on the **shared** subprocess
    WebServer that ``ai/node.py`` bootstraps on ``--data_port`` -- the same server that
    already serves ``/task/data``, so ``/venv/pipe`` is an additional route on it rather
    than a second listener on the same port. It produces no source objects itself -- the
    ``venv_server`` bridge nodes drive received frames into the local pipeline once the
    ``/venv/pipe`` route hands them the accepted socket. The task completes when the child
    is torn down at end of run.
    """

    target: IEndpointBase | None = None

    def _run(self):
        # The import MUST be inside this function: `node.py:run()` assigns the module-level
        # `shared_web_server` at runtime, so a top-of-file `from ai.node import ...` would
        # capture the pre-assignment value (None) forever. Same reason as webhook/telegram.
        from ai import node

        # Raises a self-explaining error if this subprocess has no shared server (it always
        # does: eaas spawns venv children with --data_port).
        server = node.require_shared_web_server(self.endpoint.logicalType or 'venv_source_stub')

        # Publish the pipeline target endpoint so the /venv/pipe route can reach the pipe
        # stack (target.getPipe()) and hand accepted sockets to the venv_server nodes. The
        # `data` module reads the same attribute lazily, per connection.
        server.app.state.target = self.target

        # Mount the venv bridge route (/venv/pipe) alongside the shared server's /task/data.
        # The server is already serving by now; `use()` is a router append, which Starlette
        # resolves per request, so a route added at runtime is picked up by later connects.
        server.use('venv')

        try:
            monitorStatus('Venv child ready - listening for bridged lane data')
        except Exception as e:
            debug(f'venv source status report failed: {e}')

        # Block so scanObjects() does not return and end the task; the parent terminates the
        # child at end of run. The shared server runs on node.py's background event loop.
        self._shutdown_event = threading.Event()
        self._shutdown_event.wait()

    def scanObjects(self, path: str, scanCallback: Callable[[Dict[str, Any]], None]):
        # Save the pipeline target endpoint, then block for the life of the run.
        self.target = self.endpoint.target
        self._run()
        return
