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
from rocketlib import APERR, Ec, Entry, IInstanceBase

from .IGlobal import IGlobal


class IInstance(IInstanceBase):
    """A text filter that always fails, for exercising failure paths across a venv boundary.

    Consumes text like ``text_revert`` and raises at ``closing`` instead of emitting. It exists so
    the venv merge-back (§4.12) has a deterministic in-venv failure to carry home.

    It raises an ``APERR`` with a **distinctive** code rather than a bare ``Exception`` on purpose:
    the engine keeps an ``APERR``'s own ``ec`` on the error it records and collapses everything else
    to ``Ec.Exception``, so a named code is what makes "the child's real code reached the client"
    an observable assertion rather than an implementation detail.
    """

    IGlobal: IGlobal

    def open(self, object: Entry):
        pass

    def writeText(self, text: str):
        # Swallow the input: this node never produces anything.
        self.preventDefault()

    def closing(self):
        raise APERR(Ec.InvalidDocument, 'text_fail: deliberate failure inside the virtual environment')
