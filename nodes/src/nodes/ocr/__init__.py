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
OCR node package for RocketRide Engine.

This package no longer directly exports `IInstance` / `IGlobal` — each OCR
component lives in its own sub-package and the engine loads it via the `path`
field in the matching services file:

- `nodes.ocr.standard` — `ocr` (EasyOCR + DocTR text, img2table tables)
- `nodes.ocr.surya`    — `ocr_surya` (Surya text only, scoped environment)

**This file must import nothing, and that is load-bearing rather than tidy.**
The AST walker harvests an ancestor package's requirement files but
deliberately does not queue its `__init__.py`, while Python still *executes*
it on `import nodes.ocr.surya`. A re-export here would therefore pull the
standard component — and `img2table` with it — into the Surya component's
scoped environment at startup, and the walk would not have warned, because an
ancestor's requirements are never collected.

There is no `requirements.txt` beside this file for the same reason: an
ancestor's requirements *are* harvested, so one here would be inherited by
both components and undo the isolation the split exists to buy.
"""

__all__: list[str] = []
