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
