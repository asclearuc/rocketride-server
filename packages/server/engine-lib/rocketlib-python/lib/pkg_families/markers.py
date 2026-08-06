# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# (full text in depends.py)
# =============================================================================

"""A deliberately small PEP 508 environment-marker evaluator.

``packaging`` would do this properly and is not stdlib, and this package is read by
``depends`` during bootstrap — before anything is installed — so it cannot depend on a
wheel. What is implemented is the grammar this repository actually writes: comparisons of
a known variable against a string literal, joined by ``and`` / ``or``.

Anything else raises :class:`UnsupportedMarker` rather than guessing. A marker we cannot
read is a marker we must not silently treat as false: for a family member that would
quietly drop a distribution from the install set.
"""

from __future__ import annotations

import os
import platform
import re
import sys
from typing import Optional

# The variables a marker may name. Anything outside this set is unsupported rather than
# empty — an unknown name would otherwise compare equal to nothing and read as False.
_KNOWN = (
    'platform_system',
    'platform_machine',
    'sys_platform',
    'os_name',
    'python_version',
    'python_full_version',
)

_COMPARISON = re.compile(
    r"""^\s*
        (?:
            (?P<lvar>[a-z_]+)\s*(?P<lop>==|!=)\s*(?P<lstr>'[^']*'|"[^"]*")
          | (?P<rstr>'[^']*'|"[^"]*")\s*(?P<rop>==|!=)\s*(?P<rvar>[a-z_]+)
        )
        \s*$""",
    re.VERBOSE,
)


class UnsupportedMarker(ValueError):
    """The marker uses grammar this evaluator does not implement."""


def marker_environment(env: Optional[dict] = None) -> dict[str, str]:
    """The marker variables for *this* interpreter, or the caller's override."""
    if env is not None:
        return dict(env)
    return {
        'platform_system': platform.system(),
        'platform_machine': platform.machine(),
        'sys_platform': sys.platform,
        'os_name': os.name,
        'python_version': f'{sys.version_info.major}.{sys.version_info.minor}',
        'python_full_version': platform.python_version(),
    }


def evaluate(marker: str, environment: dict[str, str]) -> bool:
    """Evaluate ``marker`` against ``environment``.

    Raises:
        UnsupportedMarker: the expression is outside the supported grammar, or names a
            variable this evaluator does not know.
    """
    text = marker.strip()
    if not text:
        return True
    if '(' in text or ')' in text:
        raise UnsupportedMarker(f'parentheses are not supported: {marker!r}')
    # `and` binds tighter than `or`, so split on `or` first and every operand is then a
    # pure conjunction.
    return any(_all_of(part, environment) for part in re.split(r'\bor\b', text))


def _all_of(conjunction: str, environment: dict[str, str]) -> bool:
    return all(_compare(part, environment) for part in re.split(r'\band\b', conjunction))


def _compare(clause: str, environment: dict[str, str]) -> bool:
    match = _COMPARISON.match(clause)
    if not match:
        raise UnsupportedMarker(f'unsupported marker clause: {clause.strip()!r}')
    var = match.group('lvar') or match.group('rvar')
    op = match.group('lop') or match.group('rop')
    literal = match.group('lstr') or match.group('rstr')
    if var not in _KNOWN:
        raise UnsupportedMarker(f'unknown marker variable: {var!r}')
    if var not in environment:
        raise UnsupportedMarker(f'marker variable not provided: {var!r}')
    value = environment[var]
    expected = literal[1:-1]
    return value == expected if op == '==' else value != expected
