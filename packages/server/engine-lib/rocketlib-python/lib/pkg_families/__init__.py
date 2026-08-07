# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# (full text in depends.py)
# =============================================================================

"""Shared-namespace package families: which members install, at what version, in what order.

Some distributions write the **same import directory**. All four ``opencv-*`` wheels
provide ``cv2``; ``onnxruntime`` and ``onnxruntime-gpu`` both provide ``onnxruntime``.
uv sees independent distributions and will never report a conflict between them, so the
last one written silently owns the namespace — and a subset arriving after a superset
takes modules away from an environment that had them.

What the resolver cannot give has to be imposed from outside it, which is what this
package is: **which members may be installed at all**, **one version among those that
co-install**, and **a fixed install order with a known winner**. It is written as data
because the two real cases need different subsets of that — opencv needs agreement and
ordering, onnxruntime needs only exclusion — and because both are handled today by two
*different* hand-written hacks.

Two rules are easy to undo by accident and are therefore stated here as well as in the
design document:

- the install set comes from **the environment's resolution**, never from the packages one
  call happens to touch, with the owner as a fallback for the empty case and never as an
  addition;
- **when** the family step fires is a different question, answered by the install's
  dry-run; conflating the two either drags opencv into an unrelated bootstrap or lets a
  later subset steal the namespace.

Stdlib only, by the rule ``venv_env`` already follows: ``depends`` reads this registry and
``depends`` needs ``engLib``, so importing anything from the engine side here would make
the logic untestable under bare ``pytest`` — and this package is read during bootstrap,
before anything is installed, so it cannot depend on a wheel either.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .facts import Facts
from . import markers
from . import probes

__all__ = [
    'Facts',
    'Family',
    'InstallPass',
    'Member',
    'Probe',
    'probes',
    'align',
    'all_member_dists',
    'applicable',
    'derived_block',
    'excluded',
    'families',
    'family_by_import',
    'family_by_name',
    'install_batches',
    'install_set',
    'installed_versions',
    'members_in',
    'normalize',
    'owner',
    'parse_annotations',
    'parse_resolution',
    'strip_derived_block',
    'version_key',
]

# The marker line that opens the generated block in ``combined.txt``. Matched on rebuild
# so the block cannot accumulate: both install paths regenerate the combined file from the
# authored requirements first, and this is the belt to that braces.
DERIVED_MARKER = '# derived by pkg_families'


# ---------------------------------------------------------------------------
# declarations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Member:
    """One distribution writing the family's shared import directory."""

    dist: str
    # PEP 508 environment marker; None means "applies everywhere".
    marker: Optional[str] = None


@dataclass(frozen=True)
class Probe:
    """Code that must succeed inside the environment just built.

    ``code`` stays a static string: the runner writes a generated preamble ahead of it —
    the overlay ``sys.path`` insert, the install set, the version — so a probe can branch
    on what was installed without the declaration having to be a template.

    ``needs`` names facts that must hold, else the probe is **skipped** and said to be
    skipped. There is deliberately no ``may_narrow``: it would veto a search that does not
    exist yet, and a field nothing reads is the speculative generality this package avoids.
    """

    code: str
    needs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Family:
    """A set of distributions that share one import namespace."""

    name: str
    # The namespace itself — 'cv2' / 'onnxruntime'. Used to notice that this process has
    # already imported what the install is about to overwrite.
    import_name: str
    # Subset first, superset last: whichever members an environment ends up installing,
    # the widest one writes the namespace last. The last *applicable* member is the owner.
    members: tuple[Member, ...]
    probe: Optional[Probe] = None
    # When set, V is declared rather than derived — in every environment. The family then
    # takes no part in alignment and writes no derived block.
    namespace_version: Optional[str] = None
    # Free-form provenance kept with the declaration rather than in a requirements comment
    # that gets deleted with the pin it explains.
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InstallPass:
    """One explicit ``uv pip install <dist>==<version>`` run in the ordered sequence."""

    dist: str
    version: str
    # Force even when uv reports it satisfied: order alone does not make the widest member
    # win — the *write* does, and "already satisfied" skips the write.
    force_reinstall: bool = False


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------


def _registry() -> tuple[Family, ...]:
    # Imported here rather than at module scope so a declaration module can import the
    # dataclasses above without a cycle.
    from .opencv import CV2

    return (CV2,)


def families() -> tuple[Family, ...]:
    """Every registered family.

    ``onnxruntime`` is declared in this package but deliberately **not** registered yet: a
    family changes what its environments install the moment it appears here, and that
    change belongs to the commit that also removes the five copied pins and declares the
    namespace version.
    """
    return _registry()


def family_by_name(name: str) -> Optional[Family]:
    for fam in families():
        if fam.name == name:
            return fam
    return None


def family_by_import(import_name: str) -> Optional[Family]:
    for fam in families():
        if fam.import_name == import_name:
            return fam
    return None


def all_member_dists() -> frozenset[str]:
    """Every registered family's members, normalised — the set the early return subtracts."""
    return frozenset(normalize(m.dist) for fam in families() for m in fam.members)


# ---------------------------------------------------------------------------
# names and versions
# ---------------------------------------------------------------------------

_NAME_SEPARATORS = re.compile(r'[-_.]+')
_LEADING_DIGITS = re.compile(r'^(\d+)(.*)$')


def normalize(dist: str) -> str:
    """PEP 503 normalisation, so ``opencv_python_headless`` and ``opencv-python-headless`` meet."""
    return _NAME_SEPARATORS.sub('-', dist.strip()).lower()


def version_key(version: str) -> tuple:
    """A sort key for comparing two releases of the same family.

    **Not** a PEP 440 implementation, and it does not need to be: the values come from a
    compiled constraints file or from a ``*.dist-info`` directory name, where a family's
    members are lockstep-released and plainly numeric. A trailing non-numeric segment
    (``4.13.0rc1``) sorts *below* the plain release, which is the one PEP 440 rule that
    would otherwise bite; anything more exotic still compares deterministically, just not
    necessarily the way PyPI would.
    """
    parts = []
    for component in version.strip().split('.'):
        found = _LEADING_DIGITS.match(component)
        if found:
            number, tail = int(found.group(1)), found.group(2)
        else:
            number, tail = -1, component
        # An empty tail is the plain release and must outrank any suffix.
        parts.append((number, 0 if tail == '' else -1, tail))
    return tuple(parts)


# ---------------------------------------------------------------------------
# reading the artifacts
# ---------------------------------------------------------------------------


def parse_resolution(text: str) -> dict[str, str]:
    """``constraints.txt`` -> ``{normalised dist: version}``.

    Only ``name==version`` lines count. Index-url lines start with ``-`` and are skipped;
    so is anything without an exact pin, which a compiled constraints file does not contain.
    """
    resolved: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split('#', 1)[0].strip()
        if not line or line.startswith('-'):
            continue
        line = line.split(';', 1)[0].strip()
        name, separator, version = line.partition('==')
        if not separator:
            continue
        name = name.split('[', 1)[0].strip()
        version = version.strip()
        if name and version:
            resolved[normalize(name)] = version
    return resolved


def parse_annotations(text: str) -> dict[str, tuple[str, ...]]:
    """``constraints.txt`` -> ``{normalised dist: who asked for it}``.

    uv writes ``# via <requester>`` under each pin, and that is where "who moved whom"
    comes from — so ``--no-annotate`` must never be added to either compile as a tidiness
    measure. A ``-r cache/combined.txt`` entry maps onward to a ``# Source:`` line in the
    combined file for the authored requirements file.
    """
    annotations: dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith('#'):
            if current is None:
                continue
            body = stripped.lstrip('#').strip()
            if body.startswith('via'):
                body = body[3:].strip()
            if body:
                annotations.setdefault(current, []).append(body)
            continue
        if stripped.startswith('-'):
            current = None
            continue
        name = stripped.split(';', 1)[0].split('==', 1)[0].split('[', 1)[0].strip()
        current = normalize(name) if name else None
    return {name: tuple(sources) for name, sources in annotations.items()}


def installed_versions(site_dir: str) -> dict[str, str]:
    """What the target site actually holds, read from ``*.dist-info`` directory names.

    A directory listing rather than a ``uv`` run: this answers "do the ordered passes have
    anything to do" on every ``depends()`` call, and a suite that makes a hundred of those
    would turn one subprocess per call into real time.
    """
    found: dict[str, str] = {}
    try:
        entries = os.listdir(site_dir)
    except OSError:
        return found
    for entry in entries:
        if not entry.endswith('.dist-info'):
            continue
        name, separator, version = entry[: -len('.dist-info')].rpartition('-')
        if separator and name:
            found[normalize(name)] = version
    return found


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------


def members_in(resolved: dict[str, str]) -> tuple[Family, ...]:
    """The registered families this resolution contains at least one member of.

    Detection reads the **resolution**, not the declarations: both real cases arrive
    transitively (Surya declares ``surya-ocr``; ``anonymize`` declares ``gliner``), so a
    rule keyed on what a requirements file names would miss them.
    """
    return tuple(fam for fam in families() if any(normalize(m.dist) in resolved for m in fam.members))


def applicable(family: Family, facts: Facts) -> tuple[Member, ...]:
    """The members whose marker holds here, in declared order."""
    selected = []
    for member in family.members:
        if member.marker is None or markers.evaluate(member.marker, facts.environment):
            selected.append(member)
    return tuple(selected)


def owner(family: Family, facts: Facts) -> Optional[Member]:
    """The last applicable member: the platform stand-in when the resolution names none.

    Needs no Darwin special case — on macOS ``-gpu`` does not apply, so plain
    ``onnxruntime`` is both the only applicable member and the owner.
    """
    members = applicable(family, facts)
    return members[-1] if members else None


def align(family: Family, resolved: dict[str, str]) -> Optional[str]:
    """The one version the namespace is held at, or ``None`` when nothing was resolved.

    A family that declares ``namespace_version`` skips derivation entirely: that value
    *is* V, in every environment. Otherwise V is the minimum over **every** member that
    appeared, matching this platform or not — each is the resolver's answer about the
    shared namespace. Considering only matching members breaks the moment a family's
    applicable member is one nothing names: on Linux ``gliner`` resolves plain
    ``onnxruntime`` while ``-gpu`` is what gets installed, and "matching only" would leave
    V undefined on exactly the platform the family exists for.
    """
    if family.namespace_version:
        return family.namespace_version
    present = [resolved[normalize(m.dist)] for m in family.members if normalize(m.dist) in resolved]
    if not present:
        return None
    return min(present, key=version_key)


def install_set(family: Family, resolved: dict[str, str], facts: Facts) -> tuple[Member, ...]:
    """The applicable members named in **this environment's** resolution, in declared order.

    Falls back to the owner only when that comes out empty — never as an addition. Adding
    the owner unconditionally would put a non-headless contrib build into an environment
    that asked for headless, which then fails ``import cv2`` on a host with no ``libGL``;
    narrowing the set to one call's packages would let a subset arriving later be the only
    member written, taking the namespace from a superset that was already there.
    """
    members = applicable(family, facts)
    named = tuple(m for m in members if normalize(m.dist) in resolved)
    if named:
        return named
    fallback = members[-1] if members else None
    return (fallback,) if fallback else ()


def excluded(family: Family) -> tuple[str, ...]:
    """Every member, for the main install's ``--excludes``.

    "Excluded" does not mean "not installed": for a member in the install set it means
    "not installed *by that run*", because the main install would let uv pick the order
    and the order is the whole point. A member outside the set is excluded outright.
    """
    return tuple(m.dist for m in family.members)


def derived_block(family: Family, version: str, members: tuple[Member, ...]) -> str:
    """The ``<member>==V`` lines appended to ``combined.txt`` before the second compile.

    **Requirements, not constraints.** A constraint on a distribution nothing requests is
    a no-op, so it would never check that V exists for the members no consumer names —
    and checking exactly that is what the second compile is for. (A learned *ceiling* is
    the exact inverse and must go in as ``-c``; the two rules look contradictory side by
    side and are the same rule applied to opposite intents.)

    Empty for a family whose version is declared: there is no derivation to check, and a
    block naming a member nothing resolves would make the second compile unconditional
    and permanent.
    """
    if family.namespace_version or not members:
        return ''
    lines = [f'{DERIVED_MARKER}: {family.name}']
    lines.extend(f'{member.dist}=={version}' for member in members)
    return '\n'.join(lines) + '\n'


def strip_derived_block(text: str) -> str:
    """Remove any previously appended blocks — belt to the regeneration braces.

    Both install paths rebuild ``combined.txt`` from the authored requirements before
    compiling, so this should never find anything. It exists because the failure it
    prevents is silent: a block appended to whatever was on disk would grow one per
    rebuild and pin the environment to the first V it ever computed.
    """
    kept, skipping = [], False
    for line in text.splitlines(keepends=True):
        if line.startswith(DERIVED_MARKER):
            skipping = True
            continue
        if skipping:
            if not line.strip() or '==' in line:
                continue
            skipping = False
        kept.append(line)
    return ''.join(kept)


def redundant(version: str, members: tuple[Member, ...], resolved: dict[str, str]) -> bool:
    """Whether the derived block would change nothing, so the second compile can be skipped.

    Not a micro-optimisation: the base compile resolves the whole tree and runs at engine
    startup, so an unconditional second pass would double it on every requirements edit.
    """
    return all(resolved.get(normalize(m.dist)) == version for m in members)


def declaration_digest(resolved: dict[str, str]) -> str:
    """A digest over the declarations of the families this resolution contains.

    The values that shape resolution and install — the member list and any declared
    namespace version — live in ``lib/pkg_families/*.py``, where the drift hash's file walk
    never looks. Without this an operator can edit the declared version and watch nothing
    happen: no recompile, no reinstall, the environment keeps what it had. A remedy that
    appears to do nothing is worse than no remedy.

    Per family and by content, never one blob over the whole registry: an environment folds
    in only the families it actually contains, so bumping onnxruntime leaves an opencv-only
    environment alone. Hashing the module's mtime instead would rebuild every environment on
    every engine upgrade.

    Returns ``''`` when no family is present, which the caller uses to leave such an
    environment's hash **byte-identical** — a family-free environment must not rebuild
    merely because this mechanism exists.
    """
    parts = []
    for family in members_in(resolved):
        members = ','.join(f'{m.dist}:{m.marker or ""}' for m in family.members)
        parts.append(f'{family.name}|{members}|{family.namespace_version or ""}')
    if not parts:
        return ''
    return hashlib.sha1('\n'.join(sorted(parts)).encode('utf-8')).hexdigest()[:16]


def hash_contribution(constraints_path: str) -> str:
    """:func:`declaration_digest` for the environment whose resolution is at that path.

    Reads the **previous** resolution on purpose. The drift hash is computed before the
    compile, so which families the environment contains is not yet known for this build —
    but it is known for the last one, and that is the right question: if a family's
    declaration changed under an environment that has it, that environment must rebuild. An
    environment newly acquiring a family has a changed requirement set anyway, so it
    rebuilds through the ordinary path and picks the contribution up on the way.
    """
    try:
        with open(constraints_path, 'r', encoding='utf-8') as fh:
            return declaration_digest(parse_resolution(fh.read()))
    except OSError:
        return ''


def combine_hash(requirements_hash: str, digest: str) -> str:
    """Fold a declaration digest into a requirements hash, or leave it untouched.

    Untouched is the load-bearing half: an environment holding no family member keeps the
    exact bytes it stored before this mechanism existed, so it does not rebuild once for
    nothing.
    """
    return f'{requirements_hash}:{digest}' if digest else requirements_hash


def install_batches(
    family: Family,
    version: str,
    members: tuple[Member, ...],
    installed: dict[str, str],
) -> tuple[InstallPass, ...]:
    """The ordered explicit installs, widest last, with the forced re-lay where needed.

    One pass per member rather than one run naming several: a single run would let uv
    choose the order, which is the thing being imposed.

    The force is the part that is easy to leave out and impossible to notice afterwards.
    An environment already holding ``opencv-contrib-python`` at V, joined by a new
    consumer wanting ``opencv-python-headless``: the ordered pass installs headless,
    reaches contrib, and uv reports contrib satisfied and skips the write — so headless's
    ``cv2/`` is the last one written and ``ximgproc`` disappears from a directory that had
    it. When anything ahead of the last member is going to be laid down, the last member is
    reinstalled by force. Decided from the ``*.dist-info`` listing before any pass starts,
    not by reading uv's output afterwards.
    """
    if not members:
        return ()
    needed = [m for m in members if installed.get(normalize(m.dist)) != version]
    if not needed:
        return ()
    passes = [InstallPass(dist=m.dist, version=version) for m in needed]
    last = members[-1]
    if last not in needed and any(m != last for m in needed):
        passes.append(InstallPass(dist=last.dist, version=version, force_reinstall=True))
    return tuple(passes)
