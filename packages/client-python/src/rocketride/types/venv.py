# MIT License
#
# Copyright (c) 2026 Aparavi Software AG
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""
Virtual-environment overlay type definitions for the RocketRide Python SDK.

An **overlay** is the on-disk ``site-packages`` tree one environment of one
pipeline installs into, living under ``<server>/venvs/<projectId>/<envId>/``.
It is a rebuildable cache: the requirements themselves live in the pipeline
document, so reclaiming an overlay costs the next run's install time and
nothing else.

Types:
    VenvOverlay: One environment overlay on disk, as returned by
        ``client.venv.list()``. Travels on the wire under the key
        ``environments``; named for what a row is rather than for the wire key.
"""

from typing import TypedDict


class VenvOverlay(TypedDict, total=False):
    """One environment overlay on disk, as returned by ``client.venv.list()``."""

    # Project directory name under ``venvs/`` — the shortened form of the
    # pipeline's ``project_id``.
    projectId: str
    # Environment directory name — ``main``, or the shortened form of a
    # container node's id.
    envId: str
    # True when a ``requirements.hash`` is present, i.e. the overlay has been
    # installed into.
    installed: bool
    # Size of ``site-packages`` in bytes. Present only when ``sizes`` was
    # requested.
    bytes: int
