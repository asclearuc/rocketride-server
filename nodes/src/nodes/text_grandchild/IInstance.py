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

import subprocess
import sys

from rocketlib import IInstanceBase

from .IGlobal import IGlobal

# Long enough that the process is unambiguously still alive when the test looks, short enough
# that a forgotten one ages out rather than living until the machine reboots.
_SLEEP_SECONDS = 600


class IInstance(IInstanceBase):
    """Spawn a process of our own and emit its PID as the text.

    Purpose-built for the orphan-safety check (§7 step 8.5B). The interesting property is what
    this process is NOT: it is a plain ``subprocess.Popen``, so unlike an engine it carries no
    ``--autoterm`` stdin monitor and holds no pipe from the server. When the server dies, nothing
    tells it to. That is the exact class of survivor -- ``ffmpeg`` in the AV reader, the audio
    loaders, ``uv``, model servers -- that cooperative teardown cannot reach, and the reason the
    process guard binds the tree at the OS level instead.

    Emitting the PID is what makes the assertion possible from outside the process tree: the test
    reads it off the pipeline result and then checks the OS directly.
    """

    IGlobal: IGlobal

    def writeText(self, text: str):
        """Spawn the sleeper and forward its PID instead of the input text."""
        # sys.executable is the engine binary, which accepts -c like any Python.
        process = subprocess.Popen([sys.executable, '-c', f'import time; time.sleep({_SLEEP_SECONDS})'])
        self.instance.writeText(str(process.pid))
        # Without this the original text is forwarded as well and the result carries two values,
        # which makes the PID awkward to read back.
        self.preventDefault()
