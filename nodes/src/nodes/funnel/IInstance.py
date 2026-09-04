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

"""Collect several same-lane producers into one, so their output can leave a venv."""

from rocketlib import AVI_ACTION, Entry, IInstanceBase

from .IGlobal import IGlobal


class IInstance(IInstanceBase):
    """Pass every write through, and refuse an interleaved media stream.

    **There is no ``write*`` forwarding code here, and that is the implementation.** The
    engine forwards a lane downstream unless the node calls ``preventDefault()`` — the shape
    `text_revert` and `anonymize` use to *suppress* it. A funnel wants exactly the default,
    so the only methods below are the media guards, which check and then let the default
    forward happen.

    ``writeAudio``/``writeVideo``/``writeImage`` are streams: ``BEGIN``, then ``WRITE``
    frames, then ``END``, and the call carries no stream id. Two producers whose streams
    overlap therefore splice into one unreadable object, and nothing downstream could
    separate them again. Without a funnel the partitioner refuses that shape outright, so
    the guard exists to keep the failure *loud* rather than trading a rejected pipeline for
    a corrupt payload.
    """

    IGlobal: IGlobal

    def open(self, object: Entry):
        # Per object, not per instance: a node is reused across objects.
        self._open_media = set()

    def _guard(self, lane: str, action):
        """Refuse a second stream on ``lane`` while one is still open.

        `int()` on both sides deliberately: ``AVI_ACTION`` members are pybind values that
        do not compare equal to their own int, so a caller holding either form works.
        """
        if int(action) == int(AVI_ACTION.BEGIN):
            if lane in self._open_media:
                raise ValueError(
                    f'funnel: a second "{lane}" stream began before the first ended. '
                    'Two producers are streaming into this funnel at once, and their frames '
                    'carry no stream identity, so they cannot be separated downstream. Give '
                    'them one environment each, or serialize them before the funnel.'
                )
            self._open_media.add(lane)
        elif int(action) == int(AVI_ACTION.END):
            self._open_media.discard(lane)

    def writeAudio(self, action, mimeType: str, buffer: bytes = None):
        self._guard('audio', action)

    def writeVideo(self, action, mimeType: str, buffer: bytes = None):
        self._guard('video', action)

    def writeImage(self, action, mimeType: str, buffer: bytes = None):
        self._guard('image', action)

    def close(self):
        self._open_media = set()
