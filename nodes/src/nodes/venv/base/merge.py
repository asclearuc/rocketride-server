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
The response merge rule for venv merge-back (design §4.12).

A ``response``/``end`` node that lands *inside* a venv writes into the **child's** entry, which
no client ever reads -- ``data_conn.close_sync`` only ever reads main's root entry. Merge-back
ships the child's entry home and folds its ``response`` into main's; this module is that fold,
and nothing else.

It is kept separate from the transport for the same reason ``lanes.py`` is: the rule is a
contract visible to every SDK caller, it is the part most likely to be got subtly wrong, and
keeping it free of ``rocketlib``/``fastapi``/``websockets`` imports means it can be loaded and
exercised without an engine.

The rule:

- **dicts merge deep** -- so ``result_types`` (a ``key -> type`` map written by the response
  node) unions without special-casing;
- **lists concatenate, main's items first** -- both sides may hold results for the same lane and
  neither should win. Note the response node's own ``deep_merge_dicts`` *replaces* lists, so it
  cannot be reused here;
- **scalars are child-wins** -- for ``name``/``path`` the two sides carry the same value anyway,
  since the child entry is reconstructed from main's.

*Ordering caveat, worth stating because it is visible in results:* "main first" orders only what
main holds **at merge time**. A main-side node that writes after the venv boundary closes still
appends after the child's items.

Double-counting is impossible by construction rather than by timing: ``Entry::__toJson`` emits
``response`` but ``Entry::__fromJson`` never reads it back, so the ``open`` frame cannot seed the
child with main's response and the two lists are always disjoint.
"""

from typing import Any, Dict


def merge_response(main: Dict[str, Any], child: Dict[str, Any]) -> Dict[str, Any]:
    """Fold a child entry's ``response`` into main's and return the merged mapping.

    Neither input is mutated; the result is a fresh structure the caller writes back key by key
    (assigning a whole ``IJson`` property is blocked -- per-key assignment is what the response
    node itself does).

    Args:
        main: main's current ``entry.response`` as a plain dict.
        child: the child's ``entry.response`` as it crossed the bridge.

    Returns:
        The merged mapping. Keys only main has are preserved untouched.
    """
    merged: Dict[str, Any] = dict(main)

    for key, childValue in child.items():
        if key not in merged:
            merged[key] = childValue
            continue

        mainValue = merged[key]

        if isinstance(mainValue, dict) and isinstance(childValue, dict):
            merged[key] = merge_response(mainValue, childValue)
        elif isinstance(mainValue, list) and isinstance(childValue, list):
            merged[key] = list(mainValue) + list(childValue)
        else:
            # Mixed or scalar: the child produced this object's result, so it wins.
            merged[key] = childValue

    return merged
