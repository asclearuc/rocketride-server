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

import argparse
import sys
from typing import Any, Callable, Dict

from rocketlib import IEndpointBase, monitorStatus, debug
from ai.web import WebServer


class IEndpoint(IEndpointBase):
    """Resident source for a virtual-environment child process.

    A venv child is spawned per run to execute one isolated pipeline group; its bridged
    lane data arrives over a loopback WebSocket, not from a scan. This source keeps the
    child engine alive for the run and hosts that transport: its ``scanObjects`` starts the
    ``/venv/pipe`` WebServer and blocks in ``server.run()`` (like ``webhook/IEndpoint``, but
    mounting the ``venv`` module instead of ``data``). It produces no source objects itself
    -- the ``venv_server`` bridge nodes drive received frames into the local pipeline once
    the ``/venv/pipe`` route hands them the accepted socket. The task completes when the
    server exits (the parent tears the child down at end of run).
    """

    target: IEndpointBase | None = None

    def _run(self):
        # eaas passes the child's data host/port on the command line; the resident source
        # binds its WebServer there, so that port IS the child's /venv/pipe bridge endpoint.
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument('--data_host', type=str, default='localhost')
        parser.add_argument('--data_port', type=int, default=5567)
        parsed_args, _ = parser.parse_known_args(sys.argv)

        self.server = WebServer(
            config={
                'port': parsed_args.data_port,
                'host': parsed_args.data_host,
            }
        )

        # Publish the pipeline target endpoint so the /venv/pipe route can reach the pipe
        # stack (target.getPipe()) and hand accepted sockets to the venv_server nodes.
        self.server.app.state.target = self.target

        # Mount the venv bridge route (/venv/pipe); NOT 'data' (/task/data).
        self.server.use('venv')

        try:
            monitorStatus('Venv child ready - listening for bridged lane data')
        except Exception as e:
            debug(f'venv source status report failed: {e}')

        # Blocks for the life of the run; the parent terminates the child at end of run.
        self.server.run()

    def scanObjects(self, path: str, scanCallback: Callable[[Dict[str, Any]], None]):
        # Save the pipeline target endpoint, then block running the web server.
        self.target = self.endpoint.target
        self._run()
        return
