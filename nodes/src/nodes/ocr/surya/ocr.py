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
Surya OCR Reader Module.

Uses the ai.common.models Surya wrapper for model server compatibility.

Single-engine by construction: there is no engine picker here, and no
``SCRIPT_FAMILIES`` table. Surya 0.17+ recognition is multilingual and
auto-detecting, so the language list the EasyOCR path needs has no meaning
for it. Keeping a dispatch table would also re-import the engines this
component exists to leave behind.
"""

import io
from typing import Any, Dict

import numpy as np
from PIL import Image

from ai.common.reader import ReaderBase
from ai.common.models.ocr.surya import Surya
from rocketlib import debug


class Reader(ReaderBase):
    """
    Surya OCR Reader using the model server wrapper.

    The wrapper auto-detects whether to use a remote model server or fall
    back to local inference.
    """

    def __init__(self, provider: str, connConfig: Dict[str, Any], bag: Dict[str, Any]):
        """
        Initialize the Surya OCR reader.

        There is deliberately no ``config.get('engine', ...)`` read here. The
        standard component's ``Reader`` defaults that key to ``'easyocr'``, so
        a copy that kept the lookup would resolve to EasyOCR in an environment
        that does not have it -- silent in review, ImportError at first use.

        Args:
            provider: Node provider name
            connConfig: Connection configuration
            bag: Shared bag dictionary
        """
        super().__init__(provider, connConfig, bag)

        self._ocr = Surya()

        debug('Surya OCR Reader initialized')

    def read(self, image_data) -> str:
        """
        Read text from an image.

        Args:
            image_data: Image as bytes, numpy array, or PIL Image

        Returns:
            Extracted text as string
        """
        # Convert to bytes for model server
        image_bytes = self._to_bytes(image_data)

        # Call OCR engine
        result = self._ocr.read(image_bytes)

        # Extract text from result
        return self._extract_text(result)

    def _to_bytes(self, image_data) -> bytes:
        """
        Convert various image formats to PNG bytes.

        Args:
            image_data: Image as a bytes-like object, numpy array, or PIL Image

        Returns:
            Image as PNG bytes

        Raises:
            TypeError: if the input is not a supported image representation
        """
        # bytearray is not a bytes subclass; writeImage accumulates into one
        if isinstance(image_data, (bytes, bytearray, memoryview)):
            return bytes(image_data)

        if isinstance(image_data, np.ndarray):
            # Handle grayscale images
            if len(image_data.shape) == 2:
                pil_image = Image.fromarray(image_data, mode='L')
            else:
                pil_image = Image.fromarray(image_data)
            buffer = io.BytesIO()
            pil_image.save(buffer, format='PNG')
            return buffer.getvalue()

        if isinstance(image_data, Image.Image):
            buffer = io.BytesIO()
            image_data.save(buffer, format='PNG')
            return buffer.getvalue()

        raise TypeError(
            f'OCR reader cannot convert {type(image_data).__name__} to image bytes; '
            'expected bytes-like, numpy.ndarray, or PIL.Image.Image'
        )

    def _extract_text(self, result) -> str:
        """
        Extract text from OCR result.

        Branches on the *result type*, not on the engine, so this is carried
        over from the standard component unchanged.

        Args:
            result: OCR result from engine

        Returns:
            Extracted text as string
        """
        if isinstance(result, dict):
            return result.get('text', '')

        if isinstance(result, str):
            return result

        if isinstance(result, list):
            # List of results - join text from each
            texts = []
            for item in result:
                if isinstance(item, dict):
                    texts.append(item.get('text', ''))
                elif isinstance(item, str):
                    texts.append(item)
            return '\n'.join(texts)

        return str(result)
