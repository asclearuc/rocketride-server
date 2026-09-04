"""VTest Image Split — the badly-behaved media producer, on purpose.

Opens its image stream in ``writeText`` and closes it in ``closing``, so the stream spans
callbacks and the engine can run another producer in between. Two of these in parallel
interleave, and their frames carry no stream identity — the corruption the funnel's guard
exists to turn into a loud failure.

Nothing ships this shape; it exists so the guard can be proven to fire against a real run
rather than only against a unit test.
"""

from rocketlib import AVI_ACTION, IInstanceBase

MIME = 'image/png'


class IInstance(IInstanceBase):
    """Begin the stream on text, end it at closing — deliberately not atomic."""

    def writeText(self, text: str):
        self.instance.writeImage(AVI_ACTION.BEGIN, MIME)
        self.instance.writeImage(AVI_ACTION.WRITE, MIME, f'SPLIT[{text}]'.encode())
        self.preventDefault()

    def closing(self):
        self.instance.writeImage(AVI_ACTION.END, MIME)
