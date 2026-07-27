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

from rocketlib import Entry, IInstanceBase

from .IGlobal import IGlobal


class IInstance(IInstanceBase):
    """A trivial text -> text transform: emit the reversed text.

    Buffers each object's ``text`` and, at ``closing``, writes the character-reversed
    result downstream (so ``hello`` becomes ``olleh``). Mirrors the buffer-then-emit shape
    of ``anonymize``. Used to give the venv round-trip a deterministic, verifiable payload.
    """

    IGlobal: IGlobal

    _buffer: str = ''

    def open(self, object: Entry):
        # Reset the accumulator for the new object (the open lane forwards downstream).
        self._buffer = ''

    def writeText(self, text: str):
        # Hold the text; the reversed result is emitted once at closing.
        self._buffer = self._buffer + text
        self.preventDefault()

    def closing(self):
        # Emit the reversed text downstream, then let the default closing forward.
        self.instance.writeText(self._buffer[::-1])

    def close(self):
        self._buffer = ''
