# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""
Tests for both OCR input lanes -- ``IInstance.writeDocuments`` and, since the
component split, ``IInstance.writeImage`` -- against **both** components
(``nodes/src/nodes/ocr/{standard,surya}/IInstance.py``).

Guards two defects that survived since the initial commit because the fulltest
feeds ``image/png``, which ``_determine_lane`` routes to the ``image`` lane:

- ``self.IGlobal.reader(image_data)`` — neither ``Reader`` nor ``ReaderBase``
  defines ``__call__``, so it raised ``TypeError`` on the first document.
- ``self.writeText(text)`` — that is ``IInstanceBase``'s inbound handler, whose
  body is ``pass``. The emitter is ``self.instance.writeText``.

``IInstance.py`` is loaded by file path under a synthetic parent package so its
``from .IGlobal import IGlobal`` resolves without the engine venv.

Only ``numpy``/``PIL`` (unused by the code path under test) and ``IGlobal``
(whose import bootstraps the OCR node's own heavy dependencies) stay stubbed.

Usage:
    ./builder.cmd nodes:test --pytest-pattern=ocr --verbose
"""

import base64
import contextlib
import importlib.util
import sys
import threading
import types
from pathlib import Path
from typing import Iterator

import pytest
from rocketlib import AVI_ACTION

from ai.common.schema import Doc

_PKG = '_ocr_pkg_under_test'

_STUB_NAMES = (
    'numpy',
    'PIL',
    'PIL.Image',
    _PKG,
    f'{_PKG}.IGlobal',
)


def _install_min_stubs() -> None:
    def _mk(name: str, **attrs: object) -> None:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

    _mk('numpy', array=lambda *a, **kw: None)

    pil = types.ModuleType('PIL')
    pil.__path__ = []
    sys.modules['PIL'] = pil
    pil_image = types.ModuleType('PIL.Image')
    pil_image.open = lambda *a, **kw: None
    sys.modules['PIL.Image'] = pil_image
    pil.Image = pil_image

    # Synthetic parent so IInstance.py's `from .IGlobal import IGlobal` resolves
    pkg = types.ModuleType(_PKG)
    pkg.__path__ = []
    sys.modules[_PKG] = pkg
    _mk(f'{_PKG}.IGlobal', IGlobal=object)


@contextlib.contextmanager
def _scoped_stubs() -> Iterator[None]:
    """Install stub modules for the duration of the block, restoring on exit."""
    snapshot = {name: sys.modules.get(name) for name in _STUB_NAMES}
    _install_min_stubs()
    try:
        yield
    finally:
        for name, mod in snapshot.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


_NODE_DIR = Path(__file__).parent.parent.parent / 'src' / 'nodes' / 'ocr'

# ``writeDocuments`` is carried into both components, so both copies must show
# the same observable text/document behaviour — a better property than
# byte-equality, since surya's copy deliberately has no
# ``extract_tables_from_image`` call at all.
#
# The load happens at import time under the scoped stubs, so this is a loop
# building {component: class}, not a fixture over a path constant.
#
# Both execs reuse the single synthetic parent package: ``IInstance.py`` needs
# one for its ``from .IGlobal import IGlobal`` to resolve, the stubbed
# ``{_PKG}.IGlobal`` is the same ``object`` for either copy, and the
# ``{_PKG}.IInstance`` entry is popped after each exec so the second load
# cannot see the first.
_IINSTANCES: dict[str, type] = {}
with _scoped_stubs():
    for _component in ('standard', 'surya'):
        _spec = importlib.util.spec_from_file_location(f'{_PKG}.IInstance', _NODE_DIR / _component / 'IInstance.py')
        assert _spec is not None and _spec.loader is not None
        _iinstance_mod = importlib.util.module_from_spec(_spec)
        _iinstance_mod.__package__ = _PKG
        sys.modules[f'{_PKG}.IInstance'] = _iinstance_mod
        _spec.loader.exec_module(_iinstance_mod)
        sys.modules.pop(f'{_PKG}.IInstance', None)
        _IINSTANCES[_component] = _iinstance_mod.IInstance


class _StubReader:
    """Stand-in for ``ocr.Reader``. Deliberately defines no ``__call__``."""

    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[bytes] = []

    def read(self, image_data):
        self.calls.append(bytes(image_data))
        return self.result


class _StubIGlobal:
    def __init__(self, reader: _StubReader) -> None:
        self.reader = reader
        self.readerLock = threading.Lock()
        # no `table_ocr` attribute, so extract_tables_from_image returns early


class _StubInstance:
    def __init__(self, lanes: tuple[str, ...]) -> None:
        self._lanes = lanes
        self.texts: list = []
        self.documents: list = []

    def hasListener(self, lane: str) -> bool:
        return lane in self._lanes

    def writeText(self, text) -> None:
        self.texts.append(text)

    def writeTable(self, table) -> None:
        pass

    def writeDocuments(self, docs) -> None:
        self.documents.append(docs)


PNG_BYTES = b'\x89PNG\r\n\x1a\n-not-a-real-png-but-opaque-to-the-node'


_ACTIVE: type | None = None


@pytest.fixture(params=sorted(_IINSTANCES), ids=sorted(_IINSTANCES), autouse=True)
def _component(request):
    """
    Run every test in this module once per OCR component.

    Autouse + a module global rather than a parameter on each test: they all
    reach the class through ``_make()``, so threading it explicitly would touch
    every one of them to assert a property that is the same for both. Yields the
    component *name* for the few tests that assert where the two differ.
    """
    global _ACTIVE
    _ACTIVE = _IINSTANCES[request.param]
    yield request.param
    _ACTIVE = None


def _make(lanes: tuple[str, ...] = ('text',), result='hello world'):
    node = _ACTIVE.__new__(_ACTIVE)
    node.inbound_writeText = []
    # IInstanceBase.writeText/preventDefault: overridden, engine dispatch is out of scope here.
    node.writeText = node.inbound_writeText.append
    node.preventDefault = lambda: 'prevented'
    node.IGlobal = _StubIGlobal(_StubReader(result))
    node.instance = _StubInstance(lanes)
    return node


def _doc() -> Doc:
    return Doc(type='Image', page_content=base64.b64encode(PNG_BYTES).decode())


class TestReaderIsInvokedCorrectly:
    def test_calls_read_not_the_instance(self) -> None:
        node = _make()
        node.writeDocuments([_doc()])

        assert node.IGlobal.reader.calls == [PNG_BYTES]

    def test_reader_is_not_callable(self) -> None:
        """The old code did reader(image_data); nothing in the MRO allows that."""
        reader = _StubReader('x')
        assert not callable(reader)
        with pytest.raises(TypeError, match='not callable'):
            reader(PNG_BYTES)


class TestTextIsEmitted:
    def test_text_goes_to_the_emitter(self) -> None:
        node = _make(result='hello world')
        node.writeDocuments([_doc()])

        assert node.instance.texts == ['hello world']

    def test_inbound_handler_is_not_used_as_emitter(self) -> None:
        """self.writeText is IInstanceBase's inbound handler — a `pass` body."""
        node = _make()
        node.writeDocuments([_doc()])

        assert node.inbound_writeText == [], 'text was sent to the inbound handler and lost'

    def test_no_text_lane_means_no_emit(self) -> None:
        node = _make(lanes=())
        node.writeDocuments([_doc()])

        assert node.instance.texts == []

    def test_list_result_is_joined(self) -> None:
        node = _make(result=['hello', 'world'])
        node.writeDocuments([_doc()])

        assert node.instance.texts == ['hello world']


class TestWriteImageLane:
    """
    The raw-image lane, driven BEGIN -> WRITE -> END.

    ``extract_tables_from_image`` has two call sites and only ``writeDocuments``
    is reachable from the tests above, so ``writeImage``'s ``AVI_ACTION.END``
    branch was asserted by nothing. Leaving that line behind in the surya
    component would raise ``AttributeError`` on every image it is handed --
    which is why driving END at all is the surya-side guard, needing no
    assertion of its own.

    ``image/png``, never ``image/gif``: the GIF branch's two dependencies are
    stubbed to return ``None`` (``numpy.array``, ``PIL.Image.open``), so taking
    it would fail inside a fixture and read as a node defect.
    """

    def _drive(self, node, payload: bytes = PNG_BYTES) -> None:
        node.writeImage(AVI_ACTION.BEGIN, 'image/png', b'')
        if payload:
            node.writeImage(AVI_ACTION.WRITE, 'image/png', payload)
        node.writeImage(AVI_ACTION.END, 'image/png', b'')

    def test_end_emits_the_read_text(self) -> None:
        node = _make()
        self._drive(node)

        assert node.IGlobal.reader.calls == [PNG_BYTES]
        assert node.instance.texts == ['hello world']

    def test_list_result_is_joined(self) -> None:
        node = _make(result=['hello', 'world'])
        self._drive(node)

        assert node.instance.texts == ['hello world']

    def test_end_resets_the_accumulator(self) -> None:
        node = _make()
        self._drive(node)

        assert node.image_data == b''

    def test_end_without_data_reads_nothing(self) -> None:
        node = _make()
        self._drive(node, payload=b'')

        assert node.IGlobal.reader.calls == []
        assert node.instance.texts == []

    def test_the_table_call_site_matches_the_component(self, _component: str) -> None:
        """Standard feeds the table lane from END; surya has no such call."""
        node = _make()
        seen: list = []
        if hasattr(node, 'extract_tables_from_image'):
            node.extract_tables_from_image = lambda data, emit: seen.append((bytes(data), emit))
        self._drive(node)

        if _component == 'standard':
            assert seen == [(PNG_BYTES, node.instance.writeTable)]
        else:
            assert not hasattr(node, 'extract_tables_from_image')


class TestDocumentsLane:
    def test_emits_converted_documents(self) -> None:
        node = _make(lanes=('documents',), result='extracted')
        assert node.writeDocuments([_doc()]) == 'prevented'

        assert len(node.instance.documents) == 1
        (txtdoc,) = node.instance.documents[0]
        assert txtdoc.type == 'Document'
        assert txtdoc.page_content == 'extracted'

    def test_rejects_non_image_documents(self) -> None:
        node = _make()
        with pytest.raises(ValueError, match='must be "image"'):
            node.writeDocuments([Doc(type='Document', page_content='')])
