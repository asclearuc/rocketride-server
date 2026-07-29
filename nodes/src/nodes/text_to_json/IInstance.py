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

from rocketlib import IJson, IInstanceBase

from .IGlobal import IGlobal


class IInstance(IInstanceBase):
    """A text -> json filter that emits on receipt, during the data phase.

    It exists for the step-8.3 diamond live check, where a venv is fed from two environments
    at once and the two branches must be told apart. ``text_revert`` buffers and emits at
    ``closing``, so a diamond built only from it would have every branch arriving flush-time;
    this node emits as soon as it is written to, which keeps exactly one flush-time branch and
    makes a dropped branch attributable.

    It also makes the second branch carry real data. ``webhook`` *declares* the ``json`` lane
    but does not emit it for a ``text/plain`` send, so a diamond wired straight off the source
    runs vacuously on that side -- the join is exercised only if something actually produces.
    """

    IGlobal: IGlobal

    def writeText(self, text: str):
        # Emit immediately (data phase), and swallow the text so only json leaves this node.
        self.instance.writeJson(IJson({'len': len(text), 'text': text}))
        self.preventDefault()
