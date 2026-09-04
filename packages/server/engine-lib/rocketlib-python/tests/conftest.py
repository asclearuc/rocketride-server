# =============================================================================
# MIT License — Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Put ``lib/`` on sys.path so tests can import the modules under test."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lib'))


@pytest.fixture(autouse=True)
def fresh_venv_env_cache():
    """Give every test a first read of the venv env vars, and leave none behind.

    ``venv_env`` freezes them at first resolution, so without this a case that reads the
    switch poisons every later one in the directory.
    """
    import venv_env

    venv_env._reset_venv_env_cache()
    yield
    venv_env._reset_venv_env_cache()


@pytest.fixture(autouse=True)
def fresh_base_env():
    """Give every test its own base environment.

    ``depends._base_env`` builds the base context once and keeps it in a module-level
    registry, capturing ``engine_cache_dir()`` — and therefore ``_get_executable_dir()`` —
    at that moment. Without this reset, a test that redirects the executable directory into
    its own ``tmp_path`` only gets what it asked for if it happens to be the first one in
    the process to ask; every later test silently reads the first one's cache directory and
    asserts against a tree it never wrote. That failure is order-dependent, so it survives
    a green run and surfaces when an unrelated test is added ahead of it.
    """
    try:
        import depends
    except ImportError:  # engLib is built into engine.exe
        yield
        return

    depends._registry.clear()
    yield
    depends._registry.clear()
