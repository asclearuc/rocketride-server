# =============================================================================
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

"""AST-based per-environment dependency discovery.

Resolves each node ``provider`` to its entry module (via the ``path`` field of
``services*.json``), then statically walks the import graph into the first-party
``nodes`` and ``ai`` packages to collect exactly the ``requirement*.txt`` files that
environment needs, instead of globbing every requirement in the tree.

Three things the walk must get right: it follows nested/in-function imports, not just
module-level ones; it resolves relative imports against the right package (``__init__``
vs regular module); and it collects what the import graph *declares*, which includes the
ancestor packages Python executes on the way in -- ``nodes/__init__.py`` before any node,
``ai/__init__.py`` and ``ai/common/__init__.py`` before any ``ai.common.*`` module. Those
files are nobody's import statement, so following imports alone never opens them.

Attribution is per directory, with one exception. A directory in which some walked file
declares a ``_REQUIREMENTS_FILE`` resolving to a file that exists is *self-describing*:
there, only the declared files are collected. That is what stops a node inheriting its
family's siblings -- ``detect`` no longer gets ``rtmlib`` from ``pose``. Everywhere else
the blanket co-location stands, and both ways the rule can fail to fire (nothing resolves,
or no declaring module was walked) leave the directory globbed, so the result stays a sound
over-approximation. Dynamic ``importlib``/``__import__`` calls that cannot be resolved
statically are flagged so the caller can fall back to the runtime ``depends()`` backstop.

Stdlib only, with the package roots passed in explicitly, so it is testable in
isolation.
"""

from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass, field
from glob import glob
from typing import Iterable, Optional

# Class names a node package re-exports; also the modules worth seeding a walk from.
_ENTRY_BASENAMES = ('__init__.py', 'IGlobal.py', 'IInstance.py', 'IEndpoint.py')

# First-party import roots we recurse INTO; anything else is a third-party leaf we
# record (torch, rfdetr, ...) but never follow. Documentation only -- the live set is the
# `roots` dict each walk is handed, which additionally carries `local_nodes` when the
# engine was started with `--node_path=`.
_FIRST_PARTY = ('nodes', 'ai', 'local_nodes')

# Stdlib / framework tops that are never pip requirements — pruned from third-party.
_NON_REQUIREMENT_TOPS = frozenset(
    {
        'os',
        'sys',
        're',
        'json',
        'typing',
        'asyncio',
        'logging',
        'abc',
        'dataclasses',
        'functools',
        'enum',
        'io',
        'time',
        'math',
        'threading',
        'collections',
        'pathlib',
        'contextlib',
        'warnings',
        'types',
        'base64',
        'hashlib',
        'zlib',
        'subprocess',
        'tempfile',
        'errno',
        'concurrent',
        'itertools',
        'copy',
        'inspect',
        'importlib',
        'traceback',
        'uuid',
        'random',
        'statistics',
        'struct',
        'datetime',
        'shutil',
        'glob',
        'string',
        'rocketlib',
        'depends',
        '__future__',
        'engLib',
    }
)


# ---------------------------------------------------------------------------
# JSONC (services*.json) parsing
# ---------------------------------------------------------------------------


def strip_jsonc(text: str) -> str:
    """Strip ``//`` and ``/* */`` comments and trailing commas from JSONC text.

    String-aware: a ``//`` inside a string literal (e.g. ``"webhook://"``) is
    preserved. ``services*.json`` files are JSONC, so plain ``json.loads`` fails.

    Args:
        text: Raw JSONC document text.

    Returns:
        A string that is valid strict JSON.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '/':
            while i < n and text[i] != '\n':
                i += 1
            continue
        if ch == '/' and i + 1 < n and text[i + 1] == '*':
            i += 2
            while i + 1 < n and not (text[i] == '*' and text[i + 1] == '/'):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return _strip_trailing_commas(''.join(out))


def _strip_trailing_commas(text: str) -> str:
    """Remove commas that immediately precede a ``}`` or ``]`` (string-aware)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == '\\' and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == ',':
            j = i + 1
            while j < n and text[j] in ' \t\r\n':
                j += 1
            if j < n and text[j] in '}]':
                i += 1  # drop the comma
                continue
        out.append(ch)
        i += 1
    return ''.join(out)


# ---------------------------------------------------------------------------
# provider -> entry-module resolution (the services*.json 'path' mapping)
# ---------------------------------------------------------------------------


@dataclass
class NodeEntry:
    """Resolution of one provider to the files that seed its dependency walk."""

    provider: str
    node_path: Optional[str]  # dotted module path from services.json, e.g. 'nodes.webhook'
    entry_files: list[str]  # absolute .py files to seed the AST walk (empty if native)
    native: bool = False  # provider has no 'path' -> no Python module (skip)


class ProviderIndex:
    """Maps a pipeline ``provider`` string to its Python entry module.

    Reproduces the engine loader's mapping (``protocol`` -> logical type, ``path`` ->
    module) rather than guessing ``nodes.<provider>``, which is wrong for roughly a
    third of providers: aliases, sub-package paths, name != directory, and native
    nodes with no ``path``.
    """

    def __init__(self, nodes_src: str, local_root: Optional[str] = None):
        """Build the index by scanning every ``services*.json`` under the node roots.

        Args:
            nodes_src: The ``nodes`` package source root (its child ``nodes/`` holds
                the node packages), e.g. ``.../nodes/src``.
            local_root: Optional ``--node_path=<dir>`` value, whose child ``local_nodes/``
                holds workspace-local node packages imported as ``local_nodes.<node>``.
                Named ``local_root`` and not ``node_path`` on purpose: in this module
                ``NodeEntry.node_path`` already means the dotted module path.
        """
        self._nodes_src = os.path.abspath(nodes_src)
        # Top-level package name -> the directory that CONTAINS it. Insertion order is the
        # scan order, and built-in `nodes` goes first so a name collision resolves to the
        # shipped node -- the same precedence the C++ loader has (services.cpp scans the
        # built-in root before local_nodes).
        self._roots: dict[str, str] = {'nodes': self._nodes_src}
        if local_root:
            self._roots['local_nodes'] = os.path.abspath(local_root)
        # logicalType -> node_path ('nodes.webhook') or None for native nodes.
        self._by_provider: dict[str, Optional[str]] = {}
        self._scan()

    def _scan(self) -> None:
        for top, base in self._roots.items():
            pattern = os.path.join(base, top, '**', 'services*.json')
            for path in glob(pattern, recursive=True):
                try:
                    with open(path, 'r', encoding='utf-8') as fh:
                        data = json.loads(strip_jsonc(fh.read()))
                except (OSError, ValueError):
                    continue
                protocol = data.get('protocol')
                if not isinstance(protocol, str):
                    continue
                logical = protocol.split('://', 1)[0].rstrip(':')
                if not logical:
                    continue
                node_path = data.get('path')
                # First definition wins; multiple services*.json may share one dir.
                self._by_provider.setdefault(logical, node_path if isinstance(node_path, str) else None)

    def known(self) -> list[str]:
        """Return all provider (logical-type) strings the index resolved."""
        return sorted(self._by_provider)

    def resolve(self, provider: str) -> Optional[NodeEntry]:
        """Resolve one provider to its entry module files.

        Args:
            provider: The pipeline component ``provider`` (logical type).

        Returns:
            A ``NodeEntry`` (``native`` when the provider has no Python ``path``),
            or ``None`` if the provider is unknown to the index.
        """
        if provider not in self._by_provider:
            return None
        node_path = self._by_provider[provider]
        if not node_path:
            return NodeEntry(provider=provider, node_path=None, entry_files=[], native=True)
        # The dotted path names its own root: `nodes.webhook` resolves under nodes_src,
        # `local_nodes.my_node` under the --node_path dir. Unknown tops fall back to
        # nodes_src so no existing resolution changes.
        base = self._roots.get(node_path.split('.', 1)[0], self._nodes_src)
        pkg_dir = os.path.join(base, *node_path.split('.'))
        entry_files = [
            os.path.join(pkg_dir, base) for base in _ENTRY_BASENAMES if os.path.isfile(os.path.join(pkg_dir, base))
        ]
        return NodeEntry(provider=provider, node_path=node_path, entry_files=entry_files)


# ---------------------------------------------------------------------------
# the transitive AST walk
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryResult:
    """Outcome of a dependency walk over one environment's node set."""

    requirement_files: list[str] = field(default_factory=list)  # absolute paths
    reached_modules: list[str] = field(default_factory=list)  # first-party dotted names
    third_party: list[str] = field(default_factory=list)  # top-level pkg names (leaves)
    dynamic_imports: list[str] = field(default_factory=list)  # unresolved importlib/__import__
    unresolved_providers: list[str] = field(default_factory=list)


def _module_to_file(dotted: str, roots: dict[str, str]) -> Optional[str]:
    """Resolve a dotted first-party module to a ``.py`` file (module or package)."""
    top = dotted.split('.', 1)[0]
    base = roots.get(top)
    if base is None:
        return None
    rel = os.path.join(*dotted.split('.'))
    for cand in (os.path.join(base, rel + '.py'), os.path.join(base, rel, '__init__.py')):
        if os.path.isfile(cand):
            return cand
    return None


def _root_of(path: str, roots: dict[str, str]) -> Optional[str]:
    """The root directory ``path`` sits under, **longest match first**.

    One answer for one question, shared by everything that has to decide which root a file belongs
    to. Longest-first is not cosmetic: with ``--node_path=`` pointing inside the exe dir, `nodes`
    and `ai` both map to the exe dir, and plain iteration order would claim a `local_nodes` file
    before its own root ever got a look.
    """
    best: Optional[str] = None
    for base in roots.values():
        if path.startswith(base) and (best is None or len(base) > len(best)):
            best = base
    return best


def _pkg_of(path: str, roots: dict[str, str]) -> str:
    """Return the dotted *package* a file belongs to (``__init__`` vs module aware)."""
    base = _root_of(path, roots)
    if base is None:
        return ''
    rel = os.path.relpath(path, base).replace(os.sep, '.')
    if rel.endswith('.__init__.py'):
        return rel[: -len('.__init__.py')]  # pkg/__init__.py -> pkg
    if rel.endswith('.py'):
        return '.'.join(rel[:-3].split('.')[:-1])  # a.b.mod -> a.b
    return ''


def _package_dirs_to(path: str, roots: dict[str, str]) -> list[str]:
    """Every package directory Python executes on the way to ``path``, root-inclusive.

    Importing ``a.b.c`` runs ``a/__init__.py`` and then ``a/b/__init__.py`` before the module
    itself, and each of those installs its own co-located ``requirement*.txt``. Those files are
    nobody's import statement, so a walk that only follows imports never opens them -- which is
    how the tree baseline stayed invisible to every environment.

    Root-**inclusive**, because the top-level package is not special to the import machinery, but
    never the root *itself*: in the deployed engine the root IS the exe dir, whose own
    ``requirement*.txt`` belongs to the base compile, not to any environment.

    A directory without ``__init__.py`` is skipped, not a stop -- PEP 420 makes it a namespace
    package that executes nothing while its own ancestors still run.
    """
    base = _root_of(path, roots)
    if base is None:
        return []
    parts = os.path.relpath(path, base).replace(os.sep, '/').split('/')[:-1]
    dirs = []
    for depth in range(1, len(parts) + 1):
        directory = os.path.join(base, *parts[:depth])
        if os.path.isfile(os.path.join(directory, '__init__.py')):
            dirs.append(directory)
    return dirs


def _import_targets(node: ast.AST, cur_file: str, roots: dict[str, str]) -> list[str]:
    """Dotted module names an Import/ImportFrom refers to (relative resolved)."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        if node.level:
            anchor = _pkg_of(cur_file, roots).split('.')
            for _ in range(node.level - 1):
                anchor = anchor[:-1]
            base = '.'.join(a for a in anchor if a)
            if node.module:
                return [f'{base}.{node.module}' if base else node.module]
            return [base] if base else []
        return [node.module] if node.module else []
    return []


def _string_consts(value: ast.AST) -> list[str]:
    """Flatten str constants from a Constant / List / Tuple expression."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return [value.value]
    if isinstance(value, (ast.List, ast.Tuple)):
        out: list[str] = []
        for elt in value.elts:
            out.extend(_string_consts(elt))
        return out
    return []


def _requirement_basenames(value: ast.AST) -> list[str]:
    """Basenames of the ``.txt`` files a ``_REQUIREMENTS_FILE`` assignment names.

    Never a plain string in this tree: every declaration is
    ``os.path.join(os.path.dirname(__file__), 'x.txt')``, a list of those, or
    ``dirname + '/x.txt'``, so Call arguments and BinOp operands have to be looked
    through to reach the constant. The directory half is always the declaring module's
    own, hence the basename.
    """
    raw: list[str] = []

    def _collect(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            raw.append(node.value)
        elif isinstance(node, (ast.List, ast.Tuple)):
            for elt in node.elts:
                _collect(elt)
        elif isinstance(node, ast.Call):
            for arg in node.args:
                _collect(arg)
        elif isinstance(node, ast.BinOp):
            _collect(node.left)
            _collect(node.right)

    _collect(value)
    return [os.path.basename(s) for s in raw if s.endswith('.txt')]


def discover(entry_files: Iterable[str], roots: dict[str, str]) -> DiscoveryResult:
    """Walk the import graph from ``entry_files`` and collect requirement files.

    Args:
        entry_files: Absolute ``.py`` paths to seed the walk (a node's entry
            modules).
        roots: ``{'nodes': <nodes src>, 'ai': <ai src>}`` first-party package roots.

    Returns:
        A ``DiscoveryResult``. ``requirement_files`` is the sound (over-approximating)
        set of ``requirement*.txt`` paths reachable from the seeds.
    """
    seen: set[str] = set()
    queue: list[str] = [os.path.abspath(f) for f in entry_files]
    reqs: set[str] = set()  # named by an explicit depends()/load_depends() literal
    declared: set[str] = set()  # named by a walked _REQUIREMENTS_FILE
    co_located: set[str] = set()  # candidate directories; globbed after the walk
    self_describing: set[str] = set()
    reached: set[str] = set()
    third: set[str] = set()
    dynamic: list[str] = []

    while queue:
        cur = queue.pop()
        if not cur or cur in seen or not os.path.isfile(cur):
            continue
        seen.add(cur)
        cur_dir = os.path.dirname(cur)
        co_located.add(cur_dir)  # a node's / ai module's co-located requirement*.txt
        # The packages Python runs on the way in. Harvested, never queued: queuing them would walk
        # ai.common.models' eager barrel and drag every model family back into every environment.
        for pkg_dir in _package_dirs_to(cur, roots):
            co_located.add(pkg_dir)
        try:
            tree = ast.parse(open(cur, encoding='utf-8').read())
        except (OSError, SyntaxError, ValueError):
            continue
        for node in ast.walk(tree):
            # calls: depends()/load_depends() string args + dynamic-import detection
            if isinstance(node, ast.Call):
                fn = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
                if fn in ('depends', 'load_depends'):
                    for arg in node.args:
                        for s in _string_consts(arg):
                            if s.endswith('.txt'):
                                cand = os.path.join(cur_dir, s)
                                if os.path.isfile(cand):
                                    reqs.add(os.path.abspath(cand))
                if fn == 'import_module' and not (
                    node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
                ):
                    dynamic.append(f'importlib.import_module(dynamic) @ {cur}')
                if getattr(node.func, 'id', None) == '__import__' and not (
                    node.args and isinstance(node.args[0], ast.Constant)
                ):
                    dynamic.append(f'__import__(dynamic) @ {cur}')
            # *_REQUIREMENTS_FILE = 'requirements_x.txt' (or a list of them)
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and 'REQUIREMENT' in tgt.id.upper():
                        for s in _string_consts(node.value):
                            if s.endswith('.txt'):
                                cand = os.path.join(cur_dir, s)
                                if os.path.isfile(cand):
                                    reqs.add(os.path.abspath(cand))
            # The model-loader convention, matched exactly: it is what makes a directory
            # self-describing, and the loose branch above would drag in every `requirements`
            # local in ai/ if it were widened to reach these Call/BinOp values.
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == '_REQUIREMENTS_FILE':
                        for name in _requirement_basenames(node.value):
                            cand = os.path.join(cur_dir, name)
                            # Keyed on the resolved path: a declaration naming a file that is
                            # not there must leave the directory globbed, not empty it.
                            if os.path.isfile(cand):
                                declared.add(os.path.abspath(cand))
                                self_describing.add(cur_dir)
            # imports: recurse first-party, record third-party leaves
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for mod in _import_targets(node, cur, roots):
                    top = mod.split('.', 1)[0]
                    if top in roots:
                        if top == 'ai' and '.models' in ('.' + mod):
                            reached.add(mod)
                        nxt = _module_to_file(mod, roots)
                        if nxt:
                            queue.append(nxt)
                    elif top and top not in _NON_REQUIREMENT_TOPS:
                        third.add(top)

    for directory in co_located:
        if directory in self_describing:
            continue
        for rq in glob(os.path.join(directory, 'requirement*.txt')):
            reqs.add(os.path.abspath(rq))
    reqs |= declared

    return DiscoveryResult(
        requirement_files=sorted(reqs),
        reached_modules=sorted(reached),
        third_party=sorted(third),
        dynamic_imports=sorted(set(dynamic)),
    )


def local_nodes_root(argv: Iterable[str]) -> Optional[str]:
    """The ``--node_path=`` directory from an engine argv, when it holds ``local_nodes/``.

    Mirrors the C++ condition rather than approximating it: ``services.cpp`` and
    ``python/init.cpp`` both act only when ``<dir>/local_nodes`` exists and is a directory,
    so a bare ``--node_path`` pointing somewhere else contributes no providers here either.
    Takes the **first** match, like the main-engine spawn's inheritance loop.

    Pure and argv-in so the module stays stdlib-only and testable without an engine.
    """
    for arg in argv or ():
        if isinstance(arg, str) and arg.startswith('--node_path='):
            value = arg[len('--node_path=') :].strip()
            if value and os.path.isdir(os.path.join(value, 'local_nodes')):
                return value
            return None
    return None


def discover_for_providers(
    providers: Iterable[str], nodes_src: str, ai_src: str, local_root: Optional[str] = None
) -> DiscoveryResult:
    """High-level: resolve providers and walk their combined dependency graph.

    Args:
        providers: The set of node ``provider`` strings used by one environment.
        nodes_src: ``nodes`` package source root (e.g. ``.../nodes/src``).
        ai_src: ``ai`` package source root (e.g. ``.../packages/ai/src``).
        local_root: Optional ``--node_path=<dir>`` holding ``local_nodes/``.

    Returns:
        A merged ``DiscoveryResult`` for the whole environment. Providers unknown
        to the index (or native, no-``path`` nodes with no module) contribute no
        files and, when unknown, are listed in ``unresolved_providers``.

    Duplicate providers (a pipeline may have many nodes of the same type, e.g. two
    OpenAI or several Frame Grabber nodes) are collapsed up front, so each provider
    is resolved and walked exactly once.
    """
    index = ProviderIndex(nodes_src, local_root)
    roots = {'nodes': os.path.abspath(nodes_src), 'ai': os.path.abspath(ai_src)}
    if local_root:
        # First-party too, so a local node importing another local node is followed like
        # any other in-tree import rather than recorded as a third-party leaf.
        roots['local_nodes'] = os.path.abspath(local_root)
    seeds: list[str] = []
    unresolved: list[str] = []
    for provider in dict.fromkeys(providers):  # unique, first-seen order
        entry = index.resolve(provider)
        if entry is None:
            unresolved.append(provider)
            continue
        seeds.extend(entry.entry_files)
    result = discover(seeds, roots)
    result.unresolved_providers = sorted(set(unresolved))
    return result
