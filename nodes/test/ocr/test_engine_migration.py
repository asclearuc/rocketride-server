# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""
The text-side migration surface of the OCR component split.

Three things that all look like configuration handling and are not:

- ``engine: surya`` on the standard node **raises and names the component**.
  Without the guard it falls through ``OCR_ENGINES.get(engine) -> None`` and
  returns EasyOCR results under Surya's name.
- ``engine: trocr`` **still returns EasyOCR**, deliberately. Increment 2.5
  deleted the engine and left the silent fallback so saved pipelines keep
  loading; trocr has no component to be pointed at. Raising on it "for
  symmetry" with the surya case is the regression this pins.
- the surya component's ``Reader``, built from an **empty** config, holds a
  ``Surya``. ``Reader.__init__`` on the standard side defaults ``engine`` to
  the literal ``'easyocr'`` and the surya services file declares no ``engine``
  field, so a ``Reader`` trimmed the obvious way silently builds EasyOCR in an
  environment that does not have it -- review-clean, ``ImportError`` at first
  use.

The three engine stubs are **distinct classes** on purpose: a single shared
stub would make every ``isinstance`` assertion here vacuously true.

The table-side half of the loud/silent asymmetry lives in
``test_model_server_ocr.py`` (``TestTableEngineValidation``).

Usage:
    ./builder.cmd nodes:test --pytest-pattern=ocr --verbose
"""

import contextlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Iterator

import pytest

_STUB_NAMES = (
    'rocketlib',
    'ai',
    'ai.common',
    'ai.common.config',
    'ai.common.models',
    'ai.common.models.ocr',
    'ai.common.models.ocr.easyocr',
    'ai.common.models.ocr.doctr',
    'ai.common.models.ocr.surya',
    'ai.common.reader',
    'numpy',
    'PIL',
    'PIL.Image',
)


class _ReaderBase:
    """Stand-in for ``ai.common.reader.ReaderBase``."""

    def __init__(self, *_a: object, **_kw: object) -> None:
        pass


class _Config:
    """Stand-in for ``ai.common.config.Config``: hands the node its connConfig."""

    @staticmethod
    def getNodeConfig(_provider: str, connConfig: dict) -> dict:
        return dict(connConfig or {})


def _noop(*_a: object, **_kw: object) -> None:
    """No-op stub for ``rocketlib.debug``."""


class _EasyOCR:
    def __init__(self, *_a: object, **kw: object) -> None:
        self.kwargs = kw


class _DocTR:
    def __init__(self, *_a: object, **kw: object) -> None:
        self.kwargs = kw


class _Surya:
    def __init__(self, *_a: object, **kw: object) -> None:
        self.kwargs = kw


# (module, exported name, stub) — the names the two components import
_OCR_ENGINES = (
    ('easyocr', 'EasyOCR', _EasyOCR),
    ('doctr', 'DocTR', _DocTR),
    ('surya', 'Surya', _Surya),
)


class _NDArray:
    """Stand-in for ``numpy.ndarray`` — only needs to be a type for isinstance."""


class _PILImage:
    """Stand-in for ``PIL.Image.Image`` — only needs to be a type for isinstance."""


def _install_min_stubs() -> None:
    def _mk(name: str, **attrs: object) -> None:
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m

    _mk('rocketlib', debug=_noop)

    ai = types.ModuleType('ai')
    ai.__path__ = []
    sys.modules['ai'] = ai
    ai_common = types.ModuleType('ai.common')
    ai_common.__path__ = []
    sys.modules['ai.common'] = ai_common

    _mk('ai.common.config', Config=_Config)
    _mk('ai.common.reader', ReaderBase=_ReaderBase)

    models = types.ModuleType('ai.common.models')
    models.__path__ = []
    sys.modules['ai.common.models'] = models
    ocr = types.ModuleType('ai.common.models.ocr')
    ocr.__path__ = []
    sys.modules['ai.common.models.ocr'] = ocr
    models.ocr = ocr
    for mod, name, cls in _OCR_ENGINES:
        _mk(f'ai.common.models.ocr.{mod}', **{name: cls})
        setattr(ocr, mod, sys.modules[f'ai.common.models.ocr.{mod}'])

    _mk('numpy', ndarray=_NDArray)

    pil = types.ModuleType('PIL')
    pil.__path__ = []
    sys.modules['PIL'] = pil
    pil_image = types.ModuleType('PIL.Image')
    pil_image.Image = _PILImage
    sys.modules['PIL.Image'] = pil_image
    pil.Image = pil_image


@contextlib.contextmanager
def _scoped_stubs() -> Iterator[None]:
    """Install stub modules for the duration of the block, restoring on exit."""
    snapshot = {name: sys.modules.get(name) for name in _STUB_NAMES}

    # A sibling test's MagicMock numpy would break the real numpy/PIL imports
    numpy_snapshot: dict[str, types.ModuleType] = {}
    for name in list(sys.modules):
        if name == 'numpy' or name.startswith('numpy.'):
            mod = sys.modules[name]
            if not hasattr(mod, '__path__') and not hasattr(mod, '__file__'):
                numpy_snapshot[name] = mod
                del sys.modules[name]

    _install_min_stubs()
    try:
        yield
    finally:
        for name, mod in snapshot.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        for name, mod in numpy_snapshot.items():
            sys.modules[name] = mod


_NODE_DIR = Path(__file__).parent.parent.parent / 'src' / 'nodes' / 'ocr'

_MODULES: dict[str, object] = {}
with _scoped_stubs():
    for _component in ('standard', 'surya'):
        _spec = importlib.util.spec_from_file_location(
            f'_ocr_migration_{_component}', _NODE_DIR / _component / 'ocr.py'
        )
        assert _spec is not None and _spec.loader is not None
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _MODULES[_component] = _mod

_STANDARD = _MODULES['standard']
_SURYA = _MODULES['surya']


def _make(module: object, **config: object) -> object:
    """Build a component's ``Reader``; ``config`` reaches it through ``_Config``."""
    return module.Reader('ocr', config, {})


class TestMovedEngineIsLoud:
    def test_surya_raises_on_the_standard_component(self) -> None:
        with pytest.raises(ValueError, match='surya'):
            _make(_STANDARD, engine='surya')

    def test_the_message_names_the_component_to_move_to(self) -> None:
        with pytest.raises(ValueError, match=r'ocr_surya://'):
            _make(_STANDARD, engine='surya')

    def test_case_is_not_an_escape_hatch(self) -> None:
        """`__init__` lowercases before dispatch, so `Surya` must not slip past."""
        with pytest.raises(ValueError, match=r'ocr_surya://'):
            _make(_STANDARD, engine='Surya')

    def test_surya_is_gone_from_the_engine_table(self) -> None:
        assert 'surya' not in _STANDARD.OCR_ENGINES
        assert set(_STANDARD.OCR_ENGINES) == {'easyocr', 'doctr'}


class TestUnknownEngineStaysSilent:
    """The other half of the asymmetry, and the one a symmetry-minded fix breaks."""

    def test_trocr_still_returns_easyocr(self) -> None:
        reader = _make(_STANDARD, engine='trocr')

        assert isinstance(reader._ocr, _EasyOCR)

    def test_the_fallback_is_asserted_by_instance_not_by_log_silence(self) -> None:
        """Silent is shorthand: the fallback does emit one debug line
        (`ocr.py`'s "falling back to EasyOCR"), so a test written against log
        silence would fail on correct code.
        """
        reader = _make(_STANDARD, engine='no-such-engine')

        assert isinstance(reader._ocr, _EasyOCR)


class TestStandardEnginesStillResolve:
    @pytest.mark.parametrize(
        ('engine', 'expected'), [('easyocr', _EasyOCR), ('doctr', _DocTR)], ids=['easyocr', 'doctr']
    )
    def test_engine_maps_to_its_class(self, engine: str, expected: type) -> None:
        assert isinstance(_make(_STANDARD, engine=engine)._ocr, expected)


class TestSuryaComponentNeedsNoConfig:
    def test_empty_config_builds_surya_not_easyocr(self) -> None:
        """The trap: the standard `Reader` defaults `engine` to 'easyocr' and the
        surya services file has no `engine` field to override it.
        """
        reader = _make(_SURYA)

        assert isinstance(reader._ocr, _Surya)
        assert not isinstance(reader._ocr, _EasyOCR)

    def test_an_engine_key_cannot_redirect_it(self) -> None:
        """No config read at all, so a stale `engine: easyocr` in a saved pipeline
        is inert rather than a way back into the shared environment.
        """
        reader = _make(_SURYA, engine='easyocr', script_family='cyrillic')

        assert isinstance(reader._ocr, _Surya)

    def test_the_dispatch_machinery_is_gone(self) -> None:
        """Not merely unused — absent, so it cannot re-import what it dispatched to."""
        for name in ('OCR_ENGINES', 'SCRIPT_FAMILIES'):
            assert not hasattr(_SURYA, name), f'{name} survived into the surya component'
        assert not hasattr(_SURYA.Reader, '_init_ocr_engine')
