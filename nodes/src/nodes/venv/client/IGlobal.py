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

from typing import Any, Dict

from rocketlib import IGlobalBase


class IGlobal(IGlobalBase):
    """Shared state for the ``venv`` (egress) bridge node.

    Step 6 scope: the lane bridge itself is complete, but the loopback endpoint of the
    spawned venv child does not exist yet. Spawning the child and injecting its
    WebSocket URL + bearer token is step 7 (local spawn + hub routing), so these stay
    unset here and ``beginInstance`` refuses to connect until then.
    """

    def beginGlobal(self):
        # TODO(step 7): the partitioner/orchestrator spawns the venv child on loopback
        # and supplies `urlProcess` (ws://127.0.0.1:<port>/...) + a Bearer `token`.
        self.urlProcess = None
        self.headers = {}

    def endGlobal(self):
        pass

    urlProcess: str = None
    headers: Dict[str, Any] = None
