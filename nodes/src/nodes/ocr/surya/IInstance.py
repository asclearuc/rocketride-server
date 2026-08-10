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
Per-instance handling for the Surya OCR component: text only.

This is the standard component's ``IInstance`` minus ``extract_tables_from_image``
**and minus its two call sites** -- one in ``writeImage``'s ``AVI_ACTION.END``
branch, one per document in ``writeDocuments``. Dropping the method while
leaving either caller would raise ``AttributeError`` on the first image this
component is handed, and the `table` lane those calls feed is not declared in
``services.surya.json`` anyway.

``debug``/``warning`` are gone with the method: they were used only by its
diagnostics and its swallow-and-warn handler.
"""

import base64
import io
import numpy as np
from PIL import Image
from typing import List
from rocketlib import IInstanceBase, AVI_ACTION, Entry
from ai.common.schema import Doc
from ai.common.avi.descriptor import rename_ext
from .IGlobal import IGlobal


class IInstance(IInstanceBase):
    IGlobal: IGlobal

    def open(self, object: Entry):
        self.image_data = b''  # Reset image data when a new object is opened

    def writeImage(self, action: int, mimeType: str, buffer: bytes):
        # Handle AVI_BEGIN action
        if action == AVI_ACTION.BEGIN:
            # BEGIN carries the stream descriptor (not image bytes); start empty so
            # descriptor bytes never leak into the decoded image.
            self.image_data = bytearray()

        # Handle AVI_WRITE action (appending chunks of the image)
        elif action == AVI_ACTION.WRITE:
            self.image_data += buffer  # Append the chunk to the existing image data

        # Handle AVI_END action (finalizing the image processing)
        elif action == AVI_ACTION.END:
            if not self.image_data:
                return

            # If the image is a GIF, iterate through the frames and
            # convert each frame to a format readable by OpenCV (e.g., PNG)
            # Text grabbed from each frame will be concatenated into a single string
            # separated by newlines and sent to the text lane
            if mimeType == 'image/gif':
                gif = Image.open(io.BytesIO(self.image_data))
                text_list = []
                try:
                    while True:
                        frame = gif.convert('RGB')
                        frame_np = np.array(frame)
                        with self.IGlobal.readerLock:
                            frame_text = self.IGlobal.reader.read(frame_np)
                        if isinstance(frame_text, list):
                            frame_text = ' '.join(frame_text)
                        text_list.append(frame_text)
                        gif.seek(gif.tell() + 1)
                except EOFError:
                    pass  # End of frames

                text = '\n'.join(text_list)
            else:
                # Acquire the lock before starting the OCR process
                with self.IGlobal.readerLock:
                    text = self.IGlobal.reader.read(self.image_data)

            if isinstance(text, list):
                text = ' '.join(text)

            self.image_data = b''  # Reset image data after the image is processed

            # Write text to text lane
            self.instance.writeText(text)

    def writeDocuments(self, documents: List[Doc]):
        txtdocs: List[Doc] = []

        # Iterate through the documents
        for doc in documents:
            # Ensure the document is an image type
            if doc.type != 'Image':
                raise ValueError('Document type must be "image"')

            # Decode the base64 image
            image_data = base64.b64decode(doc.page_content)

            # Read the text by OCR model
            with self.IGlobal.readerLock:
                text = self.IGlobal.reader.read(image_data)

            if isinstance(text, list):
                text = ' '.join(text)

            # If we have a listener on our text lane, write the text to it
            if self.instance.hasListener('text'):
                self.instance.writeText(text)

            # If we have a listener on our documents lane, create a new
            # text document and add it to the list
            if self.instance.hasListener('documents'):
                # Create a copy of the document to avoid modifying the original
                txtdoc = doc.model_copy()

                # document is now regular document type instead of image
                txtdoc.type = 'Document'

                # Add the text to the document
                txtdoc.page_content = text

                # Content changed image -> text: swap the name extension to .txt
                # (rename_ext copies the shared metadata so the input doc is untouched).
                txtdoc.metadata = rename_ext(txtdoc.metadata, 'txt')

                # Append it
                txtdocs.append(txtdoc)

        # Emit the documents with the read text
        if self.instance.hasListener('documents'):
            self.instance.writeDocuments(txtdocs)

        # Prevent default behavior of writing the image document which
        # is to call the next driver with the document images. If the
        # pipe really wanted the original image documents, it should be
        # connected to the source driver
        return self.preventDefault()
