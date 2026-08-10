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

"""
Global state for the Surya OCR component: a reader and the lock guarding it.

No ``depends()`` call and no ``requirements.txt`` beside this file. The
component's third-party dependency (``surya-ocr``) is declared where the
loader lives -- ``ai/common/models/ocr/requirements_surya.txt`` -- and the AST
walker reaches it through the ``ai.common.models.ocr.surya`` import below.
Adding a requirements file here would make this a contract-check component
whose packages are already covered under ``ai/common``.

No ``ModelServerOCR`` either: that adapter subclasses the table library's
``OCRInstance``, and that library is exactly the dependency this component
exists to keep out of its environment. Grepping this whole component for that
library's name should return nothing — which is why it is not spelled here.
"""

import threading

from rocketlib import IGlobalBase


class IGlobal(IGlobalBase):
    def beginGlobal(self):
        # Import what we need
        from .ocr import Reader

        # Get our bag
        bag = self.IEndpoint.endpoint.bag

        # Set up the lock for thread safety
        self.readerLock = threading.Lock()

        # The raw connConfig, NOT `Config.getNodeConfig`: that helper raises
        # `does not have a preconfig section` for a service that declares none,
        # and this one declares none because it has no configuration to profile.
        # Measured, that pairing is the tree's rule rather than this component's
        # exemption -- 24 of 172 services files omit `preconfig`, and not one of
        # their components calls the helper. Nothing reads the value either way:
        # `ReaderBase.__init__` ignores all three arguments and the Surya
        # `Reader` reads no config key.
        self.reader = Reader(self.glb.logicalType, self.glb.connConfig, bag)

        # Deliberately nothing else. IInstance reaches `self.IGlobal.<attr>`
        # in five places; three of them (`table_ocr`, `io`, `Img2TableImage`)
        # live inside `extract_tables_from_image`, which this component does
        # not have. `self.io` in particular looks like general plumbing and is
        # table-only.

    def endGlobal(self):
        self.reader = None
