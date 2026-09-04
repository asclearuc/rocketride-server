"""VTest Image — emits one whole image stream per inbound text, inside one callback.

The well-behaved media shape, and the reason the funnel works at all: because the engine
is single-threaded and calls nest, a producer that emits ``BEGIN``…``END`` without
returning cannot be interleaved with another producer's stream. Two of these in parallel
therefore reach a funnel already serialized.

Its twin ``vtest_image_split`` does the opposite on purpose.
"""

from rocketlib import AVI_ACTION, IInstanceBase

MIME = 'image/png'


class IInstance(IInstanceBase):
    """Turn each inbound text into a complete, self-identifying image stream."""

    def writeText(self, text: str):
        # The payload names the node instance so the funnel's output can be attributed by a
        # human reading it -- the wire itself carries no producer identity, which is the
        # whole reason the funnel exists.
        self.instance.writeImage(AVI_ACTION.BEGIN, MIME)
        self.instance.writeImage(AVI_ACTION.WRITE, MIME, f'IMG[{text}]'.encode())
        self.instance.writeImage(AVI_ACTION.END, MIME)
        # The inbound text is not part of this node's output; without this it would travel
        # on alongside the image and confuse the assertion downstream.
        self.preventDefault()
