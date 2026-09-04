# =============================================================================
# MIT License
# Copyright (c) 2026 Aparavi Software AG
# =============================================================================

"""Argument-parser tests for ``ai.eaas``.

The server's command line was untested until 2C-GC added ``--venv-gc-disabled``, a brake whose
failure nothing would report: the loop would keep collecting and the operator would find out from
a missing overlay.

Scope is the parser alone. ``run()`` copies ``args.venv_gc_disabled`` into the config dict and
then builds a real ``WebServer``, so covering that assignment would mean standing up the server.
Both ends are pinned instead -- the ``dest`` here, and ``_start_venv_gc``'s read of the config key
in ``test_task_server.py`` -- leaving one unguarded line between them.
"""

from __future__ import annotations

import pytest

eaas = pytest.importorskip('ai.eaas', reason='eaas imports ai.web, which needs the server deps')


def test_venv_gc_is_enabled_by_default():
    args = eaas.create_parser().parse_args([])
    assert args.venv_gc_disabled is False


def test_venv_gc_disabled_flag_sets_the_config_key():
    args = eaas.create_parser().parse_args(['--venv-gc-disabled'])
    assert args.venv_gc_disabled is True


def test_the_flag_takes_no_value():
    # store_true, so `=false` must be refused loudly rather than read as "disabled" -- the parse
    # that would do the opposite of what the operator typed.
    with pytest.raises(SystemExit):
        eaas.create_parser().parse_args(['--venv-gc-disabled=false'])


def test_the_flag_is_named_for_its_collector():
    # Renaming it breaks every launch configuration and service definition that passes it.
    help_text = eaas.create_parser().format_help()
    assert '--venv-gc-disabled' in help_text
    assert '--gc-disabled ' not in help_text
