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

import os

from typing import Any, Dict

from rocketlib import IGlobalBase

# The per-run bridge token, inherited from the spawning orchestrator's environment (§4.5:
# never argv/disk). The child's /venv/pipe route verifies it before accepting.
VENV_TOKEN_ENV = 'ROCKETRIDE_VENV_TOKEN'


class IGlobal(IGlobalBase):
    """Shared state for the ``venv`` (client) bridge node.

    At spawn (step 7) the orchestrator injects the live loopback URL into this node's config
    (``ws://127.0.0.1:<child_port>/venv/pipe?channel=<channelId>``). We read it from the
    engine-supplied ``connConfig`` here; the Bearer token comes from the inherited env, not
    the config. ``beginInstance`` refuses to connect if the URL is still absent.
    """

    def beginGlobal(self):
        config = self.glb.connConfig or {}
        self.urlProcess = config.get('urlProcess')
        # Direction (Gap C): this client runs in main, so it is the RETURN INGRESS
        # (receives child->main data) when its channel targets main, else the FORWARD
        # EGRESS (sends main->child data, engine-driven).
        self.targetEnv = config.get('targetEnv')
        self.sourceEnv = config.get('sourceEnv')
        token = os.environ.get(VENV_TOKEN_ENV)
        self.headers = {'Authorization': f'Bearer {token}'} if token else {}

    def endGlobal(self):
        pass

    urlProcess: str = None
    headers: Dict[str, Any] = None
    targetEnv: str = None
    sourceEnv: str = None
