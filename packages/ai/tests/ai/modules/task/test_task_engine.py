"""
Unit tests for ai.modules.task.task_engine.Task — pure-logic methods.

``Task.__init__`` is heavy (sockets, TASK_STATUS construction, DAP base,
asyncio locks, ...), so tests bypass it via ``__new__`` and seed only the
attributes the method under test consults. Real method objects are then
invoked through ``Task.<method>(stub, ...)`` so coverage tracks them.

Focus areas:

- ``_check_pipeline`` — source-component validation + status.name composition
- ``_build_task`` — subprocess-config shape
- ``_file_checksum`` — SHA-256 of a real temp file
- ``_is_debugging`` / ``_get_attach_subprocesses`` — sys.modules probes
- ``is_task_complete`` / ``is_attached`` / ``has_attached_debugger`` /
  ``get_connection_count`` / ``is_debug_available`` / ``get_status`` —
  accessors
- ``reset_idle_timer`` / ``send_scheduled_updates`` — state setters

Two methods are already exercised by separate, security-focused tests:

- ``_resolve_pipeline`` — see ``test_env_var_exfil.py``
- ``_write_task_file`` — see ``test_temp_file_security.py``
"""

from __future__ import annotations

import hashlib
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from ai.constants import CONST_STATUS_HISTORY_LIMIT
from ai.modules.task.task_engine import (
    CONST_TRACE_PAYLOAD_CAP,
    CONST_TRACE_PREVIEW_BYTES,
    Task,
    build_main_env,
    cap_trace_payload,
)
from ai.modules.task.task_metrics import TaskMetrics
from ai.modules.task.venv_spawn import (
    VENV_ENV_ID_ENV,
    VENV_ISOLATED_ENV,
    VENV_TOKEN_ENV,
    VENV_TRACE_EVENT,
    ProcessGuard,
    VenvChild,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(*, source='src-id', task_name=None, pipeline=None, status=None):
    """
    Build a Task with __init__ bypassed.

    Tests seed only the attributes consumed by the method under test:
    ``id``, ``source``, ``_task_name``, ``_pipeline``, ``_status``,
    ``_threads``, ``_pipelineTraceLevel``, ``token``.

    Args:
        source: id of the source component to look up in ``_check_pipeline``.
        task_name: optional task name used by ``_check_pipeline`` to compose
            ``status.name``.
        pipeline: pipeline dict to attach (default empty).
        status: optional TASK_STATUS-shaped stand-in; auto-built if None.

    Returns:
        Task: bare instance ready for method calls.
    """
    t = Task.__new__(Task)
    t.id = 'task-test'
    t.token = 'tk_test'
    t.client_id = 'user-1'
    t.team_id = 'team-1'
    t.org_id = 'org-1'
    t.source = source
    # Real tasks always carry their project id; _forward_task_event stamps
    # it into every forwarded body (identity safety net).
    t.project_id = 'proj-test'
    t._task_name = task_name
    t._pipeline = pipeline if pipeline is not None else {}
    t._threads = 4
    t._pipelineTraceLevel = None
    t._run_kind = 'dev'
    t._owner_kind = 'user'
    t._status = status if status is not None else SimpleNamespace(name='', state=0, exitMessage='')
    t._debugger = None
    t._debug_port = None
    t._idle_time = 5
    t._status_updated = False
    t.public_auth = 'pk_test'
    t.info = {}
    # Run-log continuum state consulted by _forward_task_event's stamping
    # safety net (see Task.stamp_log_event): fresh-stream counter + no writer.
    t._log_seq_next = 1
    t._run_log = None
    # debug_message is normally inherited from DAPBase and requires
    # _call_debug_message to be wired by __init__. Bypass with a MagicMock.
    t.debug_message = MagicMock()
    return t


# ---------------------------------------------------------------------------
# _check_pipeline
# ---------------------------------------------------------------------------


def test_check_pipeline_raises_when_source_missing():
    """A pipeline whose ``source`` id is absent from components raises ValueError."""
    t = _task(source='not-there')
    pipeline = {'components': [{'id': 'other', 'config': {}}]}
    with pytest.raises(ValueError, match='source component "not-there"'):
        Task._check_pipeline(t, pipeline)


def test_check_pipeline_creates_config_dict_if_missing():
    """A source component without ``config`` gets an empty dict inserted."""
    t = _task(source='src')
    component = {'id': 'src'}
    pipeline = {'components': [component]}
    Task._check_pipeline(t, pipeline)
    assert component['config'] == {'mode': 'Source', 'type': 'Unknown'}


def test_check_pipeline_fills_mode_and_type_defaults():
    """Missing mode defaults to 'Source'; missing type defaults to the component's provider."""
    t = _task(source='src')
    component = {'id': 'src', 'provider': 'kafka', 'config': {}}
    Task._check_pipeline(t, {'components': [component]})
    assert component['config']['mode'] == 'Source'
    assert component['config']['type'] == 'kafka'


def test_check_pipeline_preserves_existing_mode_and_type():
    """When mode/type are already set, they are not overwritten."""
    t = _task(source='src')
    component = {
        'id': 'src',
        'provider': 'kafka',
        'config': {'mode': 'Custom', 'type': 'overridden'},
    }
    Task._check_pipeline(t, {'components': [component]})
    assert component['config']['mode'] == 'Custom'
    assert component['config']['type'] == 'overridden'


def test_check_pipeline_composes_status_name_from_task_name():
    """status.name = f'{task_name}.{component_name | source_id}'."""
    t = _task(source='src', task_name='daily-ingest')
    component = {'id': 'src', 'name': 'reader'}
    Task._check_pipeline(t, {'components': [component]})
    assert t._status.name == 'daily-ingest.reader'


def test_check_pipeline_falls_back_to_task_id_and_source_id():
    """When neither task_name nor component.name are set, ids are used."""
    t = _task(source='src')
    Task._check_pipeline(t, {'components': [{'id': 'src'}]})
    assert t._status.name == 'task-test.src'


def test_check_pipeline_uses_config_name_when_component_name_missing():
    """If the component has no top-level name, fall back to config.name."""
    t = _task(source='src')
    component = {'id': 'src', 'config': {'name': 'from-config'}}
    Task._check_pipeline(t, {'components': [component]})
    assert t._status.name == 'task-test.from-config'


# ---------------------------------------------------------------------------
# _build_task
# ---------------------------------------------------------------------------


def test_build_task_returns_subprocess_config_shape(tmp_path, monkeypatch):
    """The returned dict matches the contract the engine subprocess expects."""
    # Pin sys.executable / makedirs so the function does not touch the real fs.
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'bin' / 'engine.exe'))
    monkeypatch.setattr(os, 'makedirs', lambda p, exist_ok=False: None)

    pipeline = {
        'version': 2,
        'source': 'src',
        'project_id': 'proj-1',
        'name': 'my-pipeline',
        'description': 'desc',
        'components': [{'id': 'src'}],
    }
    t = _task(pipeline=pipeline)

    config = Task._build_task(t, pipeline)

    assert config['type'] == 'pipeline'
    assert config['taskId'] == 'tk_test'
    assert config['config']['threadCount'] == 4
    assert config['config']['pipelineTraceLevel'] is None
    assert config['config']['pipeline'] == {
        'version': 2,
        'source': 'src',
        'project_id': 'proj-1',
        'name': 'my-pipeline',
        'description': 'desc',
        'components': [{'id': 'src'}],
    }
    assert config['config']['keystore'] == 'kvsfile://data/keystore.json'
    # Trusted identity travels IN THE TASK FILE (never the environment).
    assert config['identity'] == {'userId': 'user-1', 'teamId': 'team-1', 'orgId': 'org-1'}
    # Dev runs anchor node storage at the owner's whole tree.
    assert config['storage'] == {'root': 'users/user-1/files'}


def test_build_task_deploy_storage_anchor(monkeypatch, tmp_path):
    """Deploy runs anchor node storage at a task-specific TEAM subtree —
    no user dependency, and concurrent deployments never share storage.
    """
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'engine.exe'))
    monkeypatch.setattr(os, 'makedirs', lambda p, exist_ok=False: None)

    pipeline = {'source': 'src', 'components': []}
    t = _task(pipeline=pipeline)
    t._run_kind = 'deploy'
    t._owner_kind = 'team'
    config = Task._build_task(t, pipeline)
    assert config['storage'] == {'root': 'teams/team-1/files/tasks/proj-test'}


def test_build_task_deploy_without_team_refuses(monkeypatch, tmp_path):
    """A deploy run with no team has no valid anchor — fail loudly."""
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'engine.exe'))
    monkeypatch.setattr(os, 'makedirs', lambda p, exist_ok=False: None)

    t = _task(pipeline={'components': []})
    t._run_kind = 'deploy'
    t._owner_kind = 'team'
    t.team_id = ''
    with pytest.raises(ValueError, match='team_id'):
        Task._build_task(t, {'components': []})


def test_build_task_dev_without_client_gets_no_anchor(monkeypatch, tmp_path):
    """An anonymous dev run (client_id='' — OSS/standalone launch) carries NO
    anchor rather than failing the launch: identity.userId rides empty too,
    so the subprocess's engine_file_store() yields None and the storage
    tools disable themselves. The pin: the shared 'users//files' prefix must
    never be composed as an anchor.
    """
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'engine.exe'))
    monkeypatch.setattr(os, 'makedirs', lambda p, exist_ok=False: None)

    pipeline = {'source': 'src', 'components': []}
    t = _task(pipeline=pipeline)
    t.client_id = ''
    config = Task._build_task(t, pipeline)
    assert config['storage'] == {'root': ''}


def test_build_task_supplies_pipeline_version_default(monkeypatch, tmp_path):
    """An absent ``version`` field defaults to 1."""
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'engine.exe'))
    monkeypatch.setattr(os, 'makedirs', lambda p, exist_ok=False: None)

    pipeline = {'source': 'src', 'components': []}
    t = _task(pipeline=pipeline)
    config = Task._build_task(t, pipeline)
    assert config['config']['pipeline']['version'] == 1


# ---------------------------------------------------------------------------
# _file_checksum
# ---------------------------------------------------------------------------


def test_file_checksum_matches_sha256_of_file_contents(tmp_path):
    """The function returns the SHA-256 hex digest of the file body."""
    p = tmp_path / 'sample.bin'
    body = b'hello world\n' * 1024  # spans multiple 8 KiB reads
    p.write_bytes(body)

    t = _task()
    result = Task._file_checksum(t, str(p))
    assert result == hashlib.sha256(body).hexdigest()


def test_file_checksum_empty_file_yields_empty_sha256(tmp_path):
    """SHA-256 of an empty file is the canonical e3b0...b855."""
    p = tmp_path / 'empty.bin'
    p.write_bytes(b'')

    t = _task()
    assert Task._file_checksum(t, str(p)) == hashlib.sha256(b'').hexdigest()


# ---------------------------------------------------------------------------
# _is_debugging / _get_attach_subprocesses
# ---------------------------------------------------------------------------


def test_is_debugging_false_when_pydevd_absent(monkeypatch):
    """Without `pydevd` loaded, _is_debugging returns False."""
    monkeypatch.delitem(sys.modules, 'pydevd', raising=False)
    monkeypatch.delitem(sys.modules, 'debugpy', raising=False)
    assert Task._is_debugging(_task()) is False


def test_is_debugging_false_when_only_pydevd_loaded(monkeypatch):
    """Loading pydevd alone is not enough — debugpy must also be present."""
    monkeypatch.setitem(sys.modules, 'pydevd', MagicMock())
    monkeypatch.delitem(sys.modules, 'debugpy', raising=False)
    assert Task._is_debugging(_task()) is False


def test_is_debugging_true_when_both_present(monkeypatch):
    """When both modules are loaded, _is_debugging returns True."""
    monkeypatch.setitem(sys.modules, 'pydevd', MagicMock())
    monkeypatch.setitem(sys.modules, 'debugpy', MagicMock())
    assert Task._is_debugging(_task()) is True


def test_get_attach_subprocesses_false_when_not_debugging(monkeypatch):
    """If not running under a debugger, subprocess-attach is always False."""
    monkeypatch.delitem(sys.modules, 'pydevd', raising=False)
    assert Task._get_attach_subprocesses(_task()) is False


def test_get_attach_subprocesses_false_when_setup_missing(monkeypatch):
    """A pydevd module without SetupHolder.setup falls through to False."""
    monkeypatch.setitem(sys.modules, 'pydevd', MagicMock(spec=[]))
    monkeypatch.setitem(sys.modules, 'debugpy', MagicMock())
    assert Task._get_attach_subprocesses(_task()) is False


def test_get_attach_subprocesses_reads_multiprocess_flag(monkeypatch):
    """When pydevd.SetupHolder.setup['multiprocess'] is set, return its value."""
    pydevd = MagicMock()
    pydevd.SetupHolder = SimpleNamespace(setup={'multiprocess': True})
    monkeypatch.setitem(sys.modules, 'pydevd', pydevd)
    monkeypatch.setitem(sys.modules, 'debugpy', MagicMock())
    assert Task._get_attach_subprocesses(_task()) is True


def test_get_attach_subprocesses_swallows_unexpected_errors(monkeypatch):
    """Any exception while probing pydevd is caught and yields False."""
    pydevd = MagicMock()
    # Reading SetupHolder raises:
    type(pydevd).SetupHolder = property(lambda self: (_ for _ in ()).throw(RuntimeError('oops')))
    monkeypatch.setitem(sys.modules, 'pydevd', pydevd)
    monkeypatch.setitem(sys.modules, 'debugpy', MagicMock())
    assert Task._get_attach_subprocesses(_task()) is False


# ---------------------------------------------------------------------------
# State accessors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'state, expected',
    [
        (0, False),  # NONE
        (1, False),  # STARTING
        (2, False),  # INITIALIZING
        (3, False),  # RUNNING
        (4, False),  # STOPPING
        (5, True),  # COMPLETED
        (6, True),  # CANCELLED
    ],
)
def test_is_task_complete(state, expected):
    """Only COMPLETED (5) and CANCELLED (6) are treated as terminal states."""
    status = SimpleNamespace(state=state, name='', exitMessage='')
    t = _task(status=status)
    assert Task.is_task_complete(t) is expected


def test_is_attached_returns_true_for_matching_connection():
    """is_attached compares against ``_debugger`` by equality."""
    t = _task()
    conn = MagicMock()
    t._debugger = conn
    assert Task.is_attached(t, conn) is True


def test_is_attached_returns_false_when_no_debugger():
    """When no debugger is attached, is_attached returns False."""
    t = _task()
    assert Task.is_attached(t, MagicMock()) is False


def test_is_attached_returns_false_for_other_connection():
    """A different connection than the attached debugger returns False."""
    t = _task()
    t._debugger = MagicMock(name='primary')
    assert Task.is_attached(t, MagicMock(name='other')) is False


def test_has_attached_debugger_reflects_debugger_field():
    """has_attached_debugger is True iff ``_debugger`` is not None."""
    t = _task()
    assert Task.has_attached_debugger(t) is False
    t._debugger = MagicMock()
    assert Task.has_attached_debugger(t) is True


def test_get_connection_count_is_zero_or_one():
    """get_connection_count returns 1 with a debugger, 0 without."""
    t = _task()
    assert Task.get_connection_count(t) == 0
    t._debugger = MagicMock()
    assert Task.get_connection_count(t) == 1


def test_is_debug_available_requires_debug_port():
    """is_debug_available is True iff ``_debug_port`` is non-None."""
    t = _task()
    assert Task.is_debug_available(t) is False
    t._debug_port = 5566
    assert Task.is_debug_available(t) is True


def test_get_status_returns_the_status_object():
    """get_status returns the same TASK_STATUS instance that was attached."""
    status = SimpleNamespace(state=3)
    t = _task(status=status)
    assert Task.get_status(t) is status


def test_reset_idle_timer_zeroes_the_field():
    """reset_idle_timer sets ``_idle_time`` back to zero."""
    t = _task()
    t._idle_time = 999
    Task.reset_idle_timer(t)
    assert t._idle_time == 0


def test_send_scheduled_updates_flips_the_flag():
    """send_scheduled_updates marks status as needing a broadcast."""
    t = _task()
    assert t._status_updated is False
    Task.send_scheduled_updates(t)
    assert t._status_updated is True


def test_on_metrics_updated_flips_status_updated_flag():
    """_on_metrics_updated flips ``_status_updated`` to True."""
    t = _task()
    assert t._status_updated is False
    Task._on_metrics_updated(t)
    assert t._status_updated is True


# ---------------------------------------------------------------------------
# _update_status — dispatch over event types
# ---------------------------------------------------------------------------


def _make_status_for_update():
    """Build a status namespace with the attributes _update_status touches."""
    return SimpleNamespace(
        name='',
        state=0,
        exitMessage='',
        status='',
        notes=[],
        currentObject=None,
        currentSize=0,
        totalSize=0,
        totalCount=0,
        completedSize=0,
        completedCount=0,
        failedSize=0,
        failedCount=0,
        wordsSize=0,
        wordsCount=0,
        rateSize=0,
        rateCount=0,
        errors=[],
        warnings=[],
        metrics={},
    )


def test_update_status_object_event_sets_current_object_and_size():
    """An ``apaevt_status_object`` event sets ``currentObject`` and ``currentSize``."""
    t = _task(status=_make_status_for_update())
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_object',
            'body': {'object': 'file.txt', 'size': 1024},
        },
    )
    assert t._status.currentObject == 'file.txt'
    assert t._status.currentSize == 1024


def test_update_status_counts_event_populates_every_counter():
    """An ``apaevt_status_counts`` event writes every counter field on the status."""
    t = _task(status=_make_status_for_update())
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_counts',
            'body': {
                'totalSize': 1,
                'totalCount': 2,
                'completedSize': 3,
                'completedCount': 4,
                'failedSize': 5,
                'failedCount': 6,
                'wordsSize': 7,
                'wordsCount': 8,
                'rateSize': 9,
                'rateCount': 10,
            },
        },
    )
    assert t._status.totalSize == 1
    assert t._status.totalCount == 2
    assert t._status.completedSize == 3
    assert t._status.completedCount == 4
    assert t._status.failedSize == 5
    assert t._status.failedCount == 6
    assert t._status.wordsSize == 7
    assert t._status.wordsCount == 8
    assert t._status.rateSize == 9
    assert t._status.rateCount == 10


def test_update_status_error_event_appends_to_errors():
    """An ``apaevt_status_error`` event appends to ``status.errors``."""
    t = _task(status=_make_status_for_update())
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_error',
            'body': {'message': 'disk full'},
        },
    )
    assert t._status.errors == ['disk full']


def test_update_status_errors_buffer_trims_to_limit():
    """Error buffer keeps only the most recent CONST_STATUS_HISTORY_LIMIT entries."""
    t = _task(status=_make_status_for_update())
    t._status.errors = [f'err-{i}' for i in range(CONST_STATUS_HISTORY_LIMIT)]
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_error',
            'body': {'message': 'err-new'},
        },
    )
    assert len(t._status.errors) == CONST_STATUS_HISTORY_LIMIT
    assert t._status.errors[-1] == 'err-new'
    assert 'err-0' not in t._status.errors  # oldest evicted


def test_update_status_warning_event_appends_to_warnings():
    """An ``apaevt_status_warning`` event appends to ``status.warnings``."""
    t = _task(status=_make_status_for_update())
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_warning',
            'body': {'message': 'memory pressure'},
        },
    )
    assert t._status.warnings == ['memory pressure']


def test_update_status_warnings_buffer_trims_to_limit():
    """Warning buffer keeps only the most recent CONST_STATUS_HISTORY_LIMIT entries."""
    t = _task(status=_make_status_for_update())
    t._status.warnings = [f'warn-{i}' for i in range(CONST_STATUS_HISTORY_LIMIT)]
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_warning',
            'body': {'message': 'warn-new'},
        },
    )
    assert len(t._status.warnings) == CONST_STATUS_HISTORY_LIMIT
    assert t._status.warnings[-1] == 'warn-new'
    assert 'warn-0' not in t._status.warnings  # oldest evicted


def test_update_status_download_event_sets_status_string():
    """An ``apaevt_status_download`` event sets a human-readable status string."""
    t = _task(status=_make_status_for_update())
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_download',
            'body': {'info': {'name': 'whisper-tiny'}},
        },
    )
    assert 'Downloading' in t._status.status
    assert 'whisper-tiny' in t._status.status


def test_update_status_message_event_sets_status_field():
    """An ``apaevt_status_message`` event copies the message into ``status.status``."""
    t = _task(status=_make_status_for_update())
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_message',
            'body': {'message': 'processing 50%'},
        },
    )
    assert t._status.status == 'processing 50%'


def test_update_status_user_event_with_empty_notes_clears_notes():
    """An ``apaevt_status_user`` event with empty notes resets ``status.notes``."""
    t = _task(status=_make_status_for_update())
    t._status.notes = ['old note']
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_user',
            'body': {'notes': []},
        },
    )
    assert t._status.notes == []


def test_update_status_user_event_replaces_token_placeholders_in_strings():
    """String notes have {token} / {public_auth} placeholders substituted."""
    t = _task(status=_make_status_for_update())
    t.token = 'tk_x'
    t.public_auth = 'pk_x'
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_user',
            'body': {'notes': ['use {token} with {public_auth}']},
        },
    )
    assert t._status.notes == ['use tk_x with pk_x']


def test_update_status_user_event_replaces_placeholders_in_dict_values():
    """Dict notes have placeholders replaced in every string value, keeping other types."""
    t = _task(status=_make_status_for_update())
    t.token = 'tk_x'
    t.public_auth = 'pk_x'
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_user',
            'body': {'notes': [{'msg': 'token is {token}', 'count': 42}]},
        },
    )
    assert t._status.notes == [{'msg': 'token is tk_x', 'count': 42}]


def test_update_status_user_event_keeps_unknown_note_types_as_is():
    """A non-string, non-dict note (e.g. int) is appended without modification."""
    t = _task(status=_make_status_for_update())
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_user',
            'body': {'notes': [42]},
        },
    )
    assert t._status.notes == [42]


def test_update_status_info_event_merges_into_info_dict():
    """An ``apaevt_status_info`` event merges the body into ``self.info``."""
    t = _task(status=_make_status_for_update())
    t.info = {'existing': 'value'}
    Task._update_status(
        t,
        {
            'event': 'apaevt_status_info',
            'body': {'info': {'new_key': 'new_value'}},
        },
    )
    assert t.info == {'existing': 'value', 'new_key': 'new_value'}


def test_update_status_unknown_event_is_silently_ignored():
    """An event not in the dispatch table leaves status untouched."""
    t = _task(status=_make_status_for_update())
    original_status = t._status.status
    Task._update_status(
        t,
        {
            'event': 'apaevt_unknown_event',
            'body': {'whatever': 'data'},
        },
    )
    assert t._status.status == original_status


# ---------------------------------------------------------------------------
# _forward_task_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_task_event_debugger_routes_to_debugger_send_event():
    """A DEBUGGER event is sent directly to ``self._debugger.send_event``."""
    from rocketride import EVENT_TYPE
    from unittest.mock import AsyncMock

    t = _task()
    t._debugger = MagicMock()
    t._debugger.send_event = AsyncMock()
    t.id = 'task-1'

    await Task._forward_task_event(t, EVENT_TYPE.DEBUGGER, {'event': 'output', 'body': {'x': 1}})
    t._debugger.send_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_forward_task_event_debugger_skipped_when_no_debugger_attached():
    """If ``_debugger`` is None, DEBUGGER events are dropped."""
    from rocketride import EVENT_TYPE

    t = _task()
    t._debugger = None
    # Should not raise.
    await Task._forward_task_event(t, EVENT_TYPE.DEBUGGER, {'event': 'output'})


@pytest.mark.asyncio
async def test_forward_task_event_non_debugger_routes_to_server_broadcast():
    """A non-DEBUGGER event is routed through the TaskServer broadcast API."""
    from rocketride import EVENT_TYPE
    from unittest.mock import AsyncMock

    t = _task()
    t._debugger = None
    server = MagicMock()
    server.broadcast_task_event = AsyncMock()
    t._server = server
    t.token = 'tk_x'

    payload = {'event': 'summary', 'body': {}}
    await Task._forward_task_event(t, EVENT_TYPE.SUMMARY, payload)

    server.broadcast_task_event.assert_awaited_once()
    args = server.broadcast_task_event.await_args
    assert args.kwargs['token'] == 'tk_x'
    assert args.kwargs['event'] == payload


@pytest.mark.asyncio
async def test_forward_task_event_debugger_swallows_send_failure():
    """A failed send_event call is logged but does not propagate."""
    from rocketride import EVENT_TYPE
    from unittest.mock import AsyncMock

    t = _task()
    t._debugger = MagicMock()
    t._debugger.send_event = AsyncMock(side_effect=RuntimeError('socket broken'))

    # Should not raise.
    await Task._forward_task_event(t, EVENT_TYPE.DEBUGGER, {'event': 'output'})


# ---------------------------------------------------------------------------
# _pipeline_uses_rocketride_db
# ---------------------------------------------------------------------------


def test_pipeline_uses_rocketride_db_detects_each_provider():
    """Any of the three RocketRide cloud DB providers triggers DSN injection."""
    for provider in ('rocketride_sql', 'rocketride_vector', 'rocketride_graph'):
        t = _task(pipeline={'components': [{'id': 'a', 'provider': 'chat'}, {'id': 'b', 'provider': provider}]})
        assert Task._pipeline_uses_rocketride_db(t), provider


def test_pipeline_uses_rocketride_db_false_without_db_nodes():
    """Ordinary pipelines never trigger provisioning."""
    t = _task(pipeline={'components': [{'id': 'a', 'provider': 'chat'}, {'id': 'b', 'provider': 'db_postgres'}]})
    assert not Task._pipeline_uses_rocketride_db(t)


def test_pipeline_uses_rocketride_db_tolerates_malformed_components():
    """Missing components / non-dict entries must not raise at task start."""
    assert not Task._pipeline_uses_rocketride_db(_task(pipeline={}))
    t = _task(pipeline={'components': ['not-a-dict', {'no-provider': True}]})
    assert not Task._pipeline_uses_rocketride_db(t)


# ---------------------------------------------------------------------------
# _build_subprocess_env — RocketRide DB credential hygiene
# ---------------------------------------------------------------------------

_DB_PIPELINE = {'components': [{'id': 'db', 'provider': 'rocketride_sql'}]}


def _env_task(pipeline=None):
    t = _task(pipeline=pipeline if pipeline is not None else {})
    t.client_id = 'client-env-test'
    return t


def _patch_resolve(monkeypatch, fake):
    import ai.account

    monkeypatch.setattr(ai.account.account, 'resolve_db_dsn', fake)


@pytest.mark.asyncio
async def test_subprocess_env_scrubs_broker_credentials(monkeypatch):
    """The broker credential can mint ANY tenant's DSN — it must never reach
    node subprocesses, and neither may a parent-level DSN or stale error.
    """
    monkeypatch.setenv('ROCKETRIDE_DB_BROKER_URL', 'https://broker.example')
    monkeypatch.setenv('ROCKETRIDE_DB_BROKER_TOKEN', 'super-secret')
    monkeypatch.setenv('ROCKETRIDE_DB_DSN', 'postgresql://stale@parent/db')
    monkeypatch.setenv('ROCKETRIDE_DB_RESOLVE_ERROR', 'stale reason')

    env = await Task._build_subprocess_env(_env_task())  # no DB nodes

    assert 'ROCKETRIDE_DB_BROKER_URL' not in env
    assert 'ROCKETRIDE_DB_BROKER_TOKEN' not in env
    assert 'ROCKETRIDE_DB_DSN' not in env
    assert 'ROCKETRIDE_DB_RESOLVE_ERROR' not in env
    # Identity rides the task file (#1686), never the environment.
    assert 'ROCKETRIDE_CLIENT_ID' not in env


@pytest.mark.asyncio
async def test_subprocess_env_injects_resolved_dsn(monkeypatch):
    # Capture the tenant OUTSIDE the stub and assert after: _build_subprocess_env
    # converts resolver exceptions into ROCKETRIDE_DB_RESOLVE_ERROR, so an
    # AssertionError raised inside fake_resolve would be swallowed and surface
    # as a missing DSN key here instead of the real tenant mismatch.
    seen = {}

    async def fake_resolve(tenant_id):
        # The tenant is the ORG (B6) — the user is only the OSS fallback.
        seen['tenant'] = tenant_id
        return 'postgresql://tenant@pooler/db?sslmode=require'

    _patch_resolve(monkeypatch, fake_resolve)
    env = await Task._build_subprocess_env(_env_task(pipeline=_DB_PIPELINE))
    assert env['ROCKETRIDE_DB_DSN'] == 'postgresql://tenant@pooler/db?sslmode=require'
    assert seen['tenant'] == 'org-1'


@pytest.mark.asyncio
async def test_subprocess_env_dsn_tenant_is_the_org(monkeypatch):
    """The DB tenant is the ORG, not the user: a deploy run (client_id='')
    still resolves, and an org switch cannot silently re-point a user's DB
    nodes at another database.
    """
    seen = {}

    async def fake_resolve(tenant_id):
        seen['tenant'] = tenant_id
        return 'postgresql://tenant@pooler/db'

    _patch_resolve(monkeypatch, fake_resolve)
    # A deploy-shaped task: no client identity at all, org present.
    t = _env_task(pipeline=_DB_PIPELINE)
    t.client_id = ''
    env = await Task._build_subprocess_env(t)
    assert env['ROCKETRIDE_DB_DSN'] == 'postgresql://tenant@pooler/db'
    assert seen['tenant'] == 'org-1'


@pytest.mark.asyncio
async def test_subprocess_env_dsn_falls_back_to_client_without_an_org(monkeypatch):
    """OSS/single-user (no org concept): the user stays the tenant."""
    seen = {}

    async def fake_resolve(tenant_id):
        seen['tenant'] = tenant_id
        return 'postgresql://tenant@pooler/db'

    _patch_resolve(monkeypatch, fake_resolve)
    t = _env_task(pipeline=_DB_PIPELINE)
    t.org_id = ''
    await Task._build_subprocess_env(t)
    assert seen['tenant'] == 'client-env-test'


@pytest.mark.asyncio
async def test_subprocess_env_stale_dsn_does_not_survive_broker_failure(monkeypatch):
    """A parent-env DSN must not become the node's DSN when resolution fails —
    it could point at another tenant. The failure reason is passed down instead.
    """
    monkeypatch.setenv('ROCKETRIDE_DB_DSN', 'postgresql://stale@parent/other-tenant')

    async def fake_resolve(client_id):
        raise RuntimeError('DB broker request failed: HTTP 503')

    _patch_resolve(monkeypatch, fake_resolve)
    t = _env_task(pipeline=_DB_PIPELINE)
    env = await Task._build_subprocess_env(t)

    assert 'ROCKETRIDE_DB_DSN' not in env
    assert env['ROCKETRIDE_DB_RESOLVE_ERROR'] == 'DB broker request failed: HTTP 503'
    t.debug_message.assert_called_once()


@pytest.mark.asyncio
async def test_subprocess_env_unconfigured_account_is_nonfatal(monkeypatch):
    async def fake_resolve(client_id):
        raise NotImplementedError('sign in')

    _patch_resolve(monkeypatch, fake_resolve)
    env = await Task._build_subprocess_env(_env_task(pipeline=_DB_PIPELINE))
    assert 'ROCKETRIDE_DB_DSN' not in env
    assert 'ROCKETRIDE_DB_RESOLVE_ERROR' not in env


# ---------------------------------------------------------------------------
# _accumulate_analytics — run analytics in the status body
# ---------------------------------------------------------------------------


def _analytics_task():
    """A task with a REAL status model and fresh analytics state."""
    from rocketride import TASK_STATUS

    t = _task(status=TASK_STATUS())
    t._an_open_by_pipe = {}
    t._an_component_open = {}
    t._an_idle_total = 0.0
    t._an_idle_longest = 0.0
    t._an_idle_longest_at = 0.0
    t._an_idle_since = 0.0
    return t


def test_analytics_interleaved_pipes_correlate_by_pipe():
    """
    The pipe id is the correlation key: BEGIN[parse]:0, BEGIN[parse]:32,
    END[parse]:0, END[parse]:32 must yield two DISTINCT durations — a
    component-keyed accumulator would clobber pipe 0's begin with pipe 32's.
    """
    t = _analytics_task()
    t0 = 1_000.0

    Task._accumulate_analytics(t, 'begin', 0, 'parse', ['a.txt'], {'eventTime': t0, 'logSeq': 100})
    Task._accumulate_analytics(t, 'begin', 32, 'parse', ['b.txt'], {'eventTime': t0 + 0.5, 'logSeq': 101})
    Task._accumulate_analytics(t, 'end', 0, 'parse', ['a.txt'], {'eventTime': t0 + 1.0})
    Task._accumulate_analytics(t, 'end', 32, 'parse', ['b.txt'], {'eventTime': t0 + 3.0})

    docs = t._status.slowestDocs
    assert [(d.name, d.elapsed, d.beginSeq) for d in docs] == [('b.txt', 2.5, 101), ('a.txt', 1.0, 100)]
    assert t._status.completionSeconds == 3.5
    # Correlation state fully consumed.
    assert t._an_open_by_pipe == {}


def test_analytics_component_stats_key_by_pipe_and_reenter():
    """Enter/leave pairs interleave across pipes and reenter within one."""
    t = _analytics_task()
    t0 = 2_000.0

    # Interleaved across pipes: each leave must pair with ITS pipe's enter.
    Task._accumulate_analytics(t, 'enter', 0, 'parse', [], {'eventTime': t0})
    Task._accumulate_analytics(t, 'enter', 32, 'parse', [], {'eventTime': t0 + 1.0})
    Task._accumulate_analytics(t, 'leave', 0, 'parse', [], {'eventTime': t0 + 2.0})
    Task._accumulate_analytics(t, 'leave', 32, 'parse', [], {'eventTime': t0 + 2.5})

    stat = t._status.componentStats['parse']
    assert stat.calls == 2
    assert stat.totalSeconds == 3.5  # 2.0 + 1.5
    assert stat.maxSeconds == 2.0

    # Reentrancy within ONE pipe: LIFO within the (pipe, component) stack.
    Task._accumulate_analytics(t, 'enter', 0, 'llm', [], {'eventTime': t0})
    Task._accumulate_analytics(t, 'enter', 0, 'llm', [], {'eventTime': t0 + 1.0})
    Task._accumulate_analytics(t, 'leave', 0, 'llm', [], {'eventTime': t0 + 1.5})
    Task._accumulate_analytics(t, 'leave', 0, 'llm', [], {'eventTime': t0 + 4.0})
    llm = t._status.componentStats['llm']
    assert llm.calls == 2
    assert llm.totalSeconds == 4.5  # inner 0.5 + outer 4.0
    assert llm.maxSeconds == 4.0


def test_analytics_slowest_list_bounded_and_sorted():
    """The slowest list keeps the configured cap, slowest first."""
    from ai.constants import CONST_ANALYTICS_SLOWEST_DOCS

    t = _analytics_task()
    for i in range(CONST_ANALYTICS_SLOWEST_DOCS + 5):
        Task._accumulate_analytics(t, 'begin', i, 'p', [f'doc-{i}'], {'eventTime': 100.0, 'logSeq': i})
        Task._accumulate_analytics(t, 'end', i, 'p', [], {'eventTime': 100.0 + float(i + 1)})

    docs = t._status.slowestDocs
    assert len(docs) == CONST_ANALYTICS_SLOWEST_DOCS
    elapsed = [d.elapsed for d in docs]
    assert elapsed == sorted(elapsed, reverse=True)
    # The fastest completions fell off the bounded list.
    assert min(elapsed) > 1.0


def test_analytics_reset_clears_state():
    """_reset_status clears analytics fields AND correlation state."""
    t = _analytics_task()
    t._status_trace = []
    t.info = {}
    Task._accumulate_analytics(t, 'begin', 0, 'p', ['x'], {'eventTime': 1.0, 'logSeq': 1})
    Task._accumulate_analytics(t, 'enter', 0, 'p', [], {'eventTime': 1.0})
    Task._accumulate_analytics(t, 'leave', 0, 'p', [], {'eventTime': 2.0})
    Task._accumulate_analytics(t, 'end', 0, 'p', [], {'eventTime': 3.0})
    assert t._status.componentStats and t._status.slowestDocs

    Task._reset_status(t)
    assert t._status.componentStats == {}
    assert t._status.slowestDocs == []
    assert t._status.completionSeconds == 0.0
    assert t._an_open_by_pipe == {} and t._an_component_open == {}


def test_analytics_idle_between_completions():
    """
    Pipe-unused time: quiet stretches BETWEEN completions accumulate (total
    + longest + when the longest began); overlapping completions never
    count as quiet. All published numbers are server-computed.
    """
    t = _analytics_task()
    t0 = 3_000.0

    # First completion — nothing before it counts (never went quiet).
    Task._accumulate_analytics(t, 'begin', 0, 'p', ['a'], {'eventTime': t0, 'logSeq': 1})
    Task._accumulate_analytics(t, 'end', 0, 'p', [], {'eventTime': t0 + 1.0})
    assert t._an_idle_since == t0 + 1.0
    assert t._status.idleSeconds == 0.0

    # 4s quiet closes at the next begin; the marker clears while busy. The
    # longest stretch remembers WHEN it began.
    Task._accumulate_analytics(t, 'begin', 0, 'p', ['b'], {'eventTime': t0 + 5.0, 'logSeq': 2})
    assert t._status.idleSeconds == 4.0
    assert t._status.idleLongestSeconds == 4.0
    assert t._status.idleLongestAt == t0 + 1.0
    assert t._an_idle_since == 0.0

    # Overlap: pipe 1 begins before pipe 0 ends — no quiet in between.
    Task._accumulate_analytics(t, 'begin', 1, 'p', ['c'], {'eventTime': t0 + 6.0, 'logSeq': 3})
    Task._accumulate_analytics(t, 'end', 0, 'p', [], {'eventTime': t0 + 7.0})
    assert t._an_idle_since == 0.0  # pipe 1 still busy
    Task._accumulate_analytics(t, 'end', 1, 'p', [], {'eventTime': t0 + 8.0})
    assert t._an_idle_since == t0 + 8.0

    # A shorter 1s gap grows the total but not the longest (or its stamp).
    Task._accumulate_analytics(t, 'begin', 0, 'p', ['d'], {'eventTime': t0 + 9.0, 'logSeq': 4})
    assert t._status.idleSeconds == 5.0
    assert t._status.idleLongestSeconds == 4.0
    assert t._status.idleLongestAt == t0 + 1.0


def test_analytics_idle_refresh_extends_open_stretch():
    """
    The periodic publish path folds the STILL-OPEN quiet stretch into the
    status: total grows, and once the open stretch beats the recorded
    longest it becomes the longest — with ITS start as the stamp. Trace
    events never arrive during silence, so this is what keeps a quiet
    pipe's numbers current.
    """
    t = _analytics_task()
    t0 = 4_000.0

    # One closed 2s gap, then quiet from t0+5.
    Task._accumulate_analytics(t, 'begin', 0, 'p', ['a'], {'eventTime': t0, 'logSeq': 1})
    Task._accumulate_analytics(t, 'end', 0, 'p', [], {'eventTime': t0 + 1.0})
    Task._accumulate_analytics(t, 'begin', 0, 'p', ['b'], {'eventTime': t0 + 3.0, 'logSeq': 2})
    Task._accumulate_analytics(t, 'end', 0, 'p', [], {'eventTime': t0 + 5.0})

    # 1s into the silence: total extends, closed 2s gap is still longest.
    Task._refresh_idle_status(t, t0 + 6.0)
    assert t._status.idleSeconds == 3.0
    assert t._status.idleLongestSeconds == 2.0
    assert t._status.idleLongestAt == t0 + 1.0

    # 10s in: the open stretch is now the longest, stamped at ITS start.
    Task._refresh_idle_status(t, t0 + 15.0)
    assert t._status.idleSeconds == 12.0
    assert t._status.idleLongestSeconds == 10.0
    assert t._status.idleLongestAt == t0 + 5.0

    # The provisional publishes never double-count: closing the gap at the
    # next begin lands on the same numbers a fresh reader would compute.
    Task._accumulate_analytics(t, 'begin', 0, 'p', ['c'], {'eventTime': t0 + 20.0, 'logSeq': 3})
    assert t._status.idleSeconds == 17.0
    assert t._status.idleLongestSeconds == 15.0
    assert t._status.idleLongestAt == t0 + 5.0

    # While busy, refresh republishes the closed totals unchanged.
    Task._refresh_idle_status(t, t0 + 60.0)
    assert t._status.idleSeconds == 17.0


def test_analytics_idle_reset():
    """_reset_status clears the pipe-unused counters with the rest."""
    t = _analytics_task()
    t._status_trace = []
    t.info = {}
    Task._accumulate_analytics(t, 'begin', 0, 'p', ['a'], {'eventTime': 1.0, 'logSeq': 1})
    Task._accumulate_analytics(t, 'end', 0, 'p', [], {'eventTime': 2.0})
    Task._accumulate_analytics(t, 'begin', 0, 'p', ['b'], {'eventTime': 5.0, 'logSeq': 2})
    Task._accumulate_analytics(t, 'end', 0, 'p', [], {'eventTime': 6.0})
    assert t._status.idleSeconds == 3.0 and t._an_idle_since == 6.0

    Task._reset_status(t)
    assert t._status.idleSeconds == 0.0
    assert t._status.idleLongestSeconds == 0.0
    assert t._status.idleLongestAt == 0.0
    assert t._an_idle_total == 0.0 and t._an_idle_since == 0.0


# ---------------------------------------------------------------------------
# cap_trace_payload — the 1MB trace/flow payload clamp
# ---------------------------------------------------------------------------


def test_task_rejects_unknown_run_classifications():
    """run_kind/trigger are a CLOSED vocabulary, validated at construction.

    Both gate storage anchors, run-log scoping, and token ownership — a
    value outside the vocabulary must fail before it can pick a scope.
    ('' trigger is the interactive-dev spelling and stays valid.)
    """
    from unittest.mock import MagicMock

    common = dict(
        server=MagicMock(), id='t-1', project_id='p-1', source='s-1', token='tk', public_auth='pk', pipeline={}
    )
    with pytest.raises(ValueError, match='run_kind'):
        Task(**common, run_kind='prod')
    with pytest.raises(ValueError, match='trigger'):
        Task(**common, trigger='cron')


def test_cap_trace_payload_passes_small_payloads_through():
    """Payloads under the cap pass through IDENTICALLY (same object)."""
    payload = {'op': 'x', 'data': 'y' * 1000}
    assert cap_trace_payload(payload) is payload
    # Falsy payloads are untouched too (no marker for nothing).
    assert cap_trace_payload({}) == {}
    assert cap_trace_payload(None) is None


def test_cap_trace_payload_truncates_oversized_payloads():
    """An over-cap payload becomes the honest marker with a bounded preview."""
    blob = {'data': 'z' * (CONST_TRACE_PAYLOAD_CAP + 100)}
    capped = cap_trace_payload(blob)
    assert capped['truncated'] is True
    assert capped['originalBytes'] > CONST_TRACE_PAYLOAD_CAP
    assert len(capped['preview']) == CONST_TRACE_PREVIEW_BYTES
    # The marker CLIPS to the cap — consumers still get (just under) the
    # full megabyte, and the marker never exceeds the cap itself.
    import json as _json

    assert len(_json.dumps(capped)) <= CONST_TRACE_PAYLOAD_CAP


def test_cap_trace_payload_bound_holds_for_escape_heavy_payloads():
    """The cap must hold for the marker AS SERIALIZED, not the raw slice.

    `preview` holds already-serialized JSON text; re-serializing escapes
    every quote and backslash in it, so an object-heavy payload (unlike the
    plain-'z' fixture above, which needs no escaping) inflates the marker.
    The clamp must size the SERIALIZED marker under the cap.
    """
    import json as _json

    # Thousands of tiny dicts full of quotes and backslashes — every one
    # of the preview's structural characters re-escapes on serialization.
    blob = {'data': [{'k': 'v"\\'}] * (CONST_TRACE_PAYLOAD_CAP // 12)}
    assert len(_json.dumps(blob)) > CONST_TRACE_PAYLOAD_CAP
    capped = cap_trace_payload(blob)
    assert capped['truncated'] is True
    assert len(_json.dumps(capped)) <= CONST_TRACE_PAYLOAD_CAP
    # The trimmed preview still carries real content, not an empty husk.
    assert len(capped['preview']) > CONST_TRACE_PAYLOAD_CAP // 4


def test_cap_trace_payload_leaves_unserializable_payloads_alone():
    """Unserializable payloads pass through — the transport owns that error."""
    payload = {'bad': object()}
    assert cap_trace_payload(payload) is payload


# ---------------------------------------------------------------------------
# venv child fan-in (step 8.4)
# ---------------------------------------------------------------------------


def _child(env_id='v1', name='v1'):
    """A VenvChild with no live process -- the fan-in never touches one."""
    return VenvChild(env_id=env_id, name=name, process=None, port=5601, tmpfile='')


def _fanin_task(*, trace_level=None, main_started=False):
    """A Task seeded with just the state ``_on_child_event`` reads."""
    t = _task()
    t._pipelineTraceLevel = trace_level
    t._main_engine_started = main_started
    t._status_trace = []
    t._status = SimpleNamespace(name='', state=0, exitMessage='', status='', errors=[], warnings=[])
    t._task_metrics = None
    t._forward_task_event = AsyncMock()
    t._send_status_update = AsyncMock()
    return t


def test_effective_engine_arg_prefers_the_launch_request():
    """Main takes --trace= from the launch args first and only falls back to the server's
    own, so a child inheriting just the fallback would carry a different level than main.
    """
    t = _task()
    t._launch_args = {'args': ['--trace=3']}
    with patch('ai.modules.task.task_engine.startup_args', return_value=['--trace=1']):
        assert Task._effective_engine_arg(t, '--trace=') == '--trace=3'


def test_effective_engine_arg_splits_combined_launch_args():
    t = _task()
    t._launch_args = {'args': ['--verbose --trace=2']}
    with patch('ai.modules.task.task_engine.startup_args', return_value=[]):
        assert Task._effective_engine_arg(t, '--trace=') == '--trace=2'


def test_effective_engine_arg_falls_back_to_startup_args():
    t = _task()
    t._launch_args = {'args': []}
    with patch('ai.modules.task.task_engine.startup_args', return_value=['--other', '--trace=1']):
        assert Task._effective_engine_arg(t, '--trace=') == '--trace=1'


def test_effective_engine_arg_is_none_when_the_run_set_no_level():
    t = _task()
    t._launch_args = {}
    with patch('ai.modules.task.task_engine.startup_args', return_value=['--port=5566']):
        assert Task._effective_engine_arg(t, '--trace=') is None


# The prefix parameter is what item 5 added: venv children now inherit --node_path= by the
# same rule as --trace=, so a workspace-local node resolves inside a child too.


def test_effective_engine_arg_reads_node_path_from_startup_args():
    """The nodes-test server passes --node_path= on its own command line, never per launch."""
    t = _task()
    t._launch_args = {}
    startup = ['--port=5566', '--node_path=e:\\repo\\nodes\\test\\fixtures']
    with patch('ai.modules.task.task_engine.startup_args', return_value=startup):
        assert Task._effective_engine_arg(t, '--node_path=') == '--node_path=e:\\repo\\nodes\\test\\fixtures'


def test_effective_engine_arg_keeps_the_two_prefixes_independent():
    """One helper, two flags: asking for one must not answer with the other."""
    t = _task()
    t._launch_args = {'args': ['--trace=2']}
    with patch('ai.modules.task.task_engine.startup_args', return_value=['--node_path=/w']):
        assert Task._effective_engine_arg(t, '--trace=') == '--trace=2'
        assert Task._effective_engine_arg(t, '--node_path=') == '--node_path=/w'


@pytest.mark.asyncio
async def test_child_metrics_are_merged_under_the_env_as_source():
    """A child's >MET must land in its OWN slot, or it erases main's timers and counters.

    Autospec, not a bare MagicMock: the defect this covers was a call to a keyword the real
    signature did not have, and a bare mock accepts any keyword at all -- so the route was
    exercised live and the TypeError went to the stdout reader unnoticed.
    """
    t = _fanin_task()
    t._task_metrics = create_autospec(TaskMetrics, instance=True)

    payload = {'timers': {'gpu_compute': 12.0}}
    await Task._on_child_event(
        t, _child(env_id='v2', name='v2'), {'event': 'apaevt_status_metrics', 'body': {'metrics': payload}}
    )

    t._task_metrics.merge_subprocess_metrics.assert_called_once_with(payload, source='v2')


@pytest.mark.asyncio
async def test_child_metrics_without_a_metrics_object_are_dropped():
    """Children are spawned before TaskMetrics exists, so the route must tolerate None."""
    t = _fanin_task()
    t._task_metrics = None

    await Task._on_child_event(t, _child(), {'event': 'apaevt_status_metrics', 'body': {'metrics': {}}})


@pytest.mark.asyncio
async def test_child_status_state_does_not_lift_the_billing_gate():
    """>SVC must never reach set_service_up: a child is not the run's readiness."""
    t = _fanin_task()
    t._task_metrics = create_autospec(TaskMetrics, instance=True)

    await Task._on_child_event(t, _child(), {'event': 'apaevt_status_state', 'body': {'service': True}})

    t._task_metrics.set_service_up.assert_not_called()
    assert t._status.state == 0


@pytest.mark.asyncio
async def test_child_status_message_sets_the_run_status_before_the_main_engine():
    """The window that turns a silent 30-second death into visible install progress."""
    t = _fanin_task(main_started=False)

    await Task._on_child_event(
        t, _child(name='v1'), {'event': 'apaevt_status_message', 'body': {'message': 'Downloading torch'}}
    )

    assert t._status.status == '[v1] Downloading torch'
    t._send_status_update.assert_awaited()


@pytest.mark.asyncio
async def test_child_status_message_is_ignored_once_the_main_engine_exists():
    """Both halves matter: a test of only the first passes on an always-set implementation,
    which would let a child overwrite main's status hundreds of times per run.
    """
    t = _fanin_task(main_started=True)
    t._status.status = 'main is talking'

    await Task._on_child_event(
        t, _child(), {'event': 'apaevt_status_message', 'body': {'message': 'Downloading torch'}}
    )

    assert t._status.status == 'main is talking'
    t._send_status_update.assert_not_awaited()


@pytest.mark.asyncio
async def test_child_status_window_reopens_on_restart():
    """The regression that forced a per-run flag over ``_engine_process is None``: that
    attribute is never nulled, so on a restarted task the window would never reopen.
    """
    t = _fanin_task(main_started=True)

    t._main_engine_started = False  # what start_task does for the next run

    await Task._on_child_event(t, _child(), {'event': 'apaevt_status_message', 'body': {'message': 'again'}})

    assert t._status.status == '[v1] again'


@pytest.mark.asyncio
async def test_child_traces_are_suppressed_without_a_trace_level():
    """A child emits >DBG whether or not the run asked for tracing, so forwarding ungated
    would deliver volume that =0 does not.
    """
    t = _fanin_task(trace_level=None)

    await Task._on_child_event(t, _child(), {'event': 'apaevt_trace', 'body': {'op': 'enter', 'id': 1}})

    t._forward_task_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_child_traces_forward_renamed_and_tagged_when_tracing():
    t = _fanin_task(trace_level='full')

    await Task._on_child_event(
        t, _child(env_id='v1', name='parse'), {'event': 'apaevt_trace', 'body': {'op': 'enter', 'id': 1}}
    )

    t._forward_task_event.assert_awaited_once()
    _event_type, message = t._forward_task_event.await_args.args
    assert message['event'] == VENV_TRACE_EVENT
    assert message['body']['env'] == {'id': 'v1', 'name': 'parse'}


@pytest.mark.asyncio
async def test_child_error_reaches_the_run_and_the_tail():
    """>ERR* arrives as apaevt_status_error, NOT as an output event -- a tail built only from
    output would silently lose the startup diagnostic this feeds.
    """
    t = _fanin_task()
    child = _child(name='v1')

    await Task._on_child_event(t, child, {'event': 'apaevt_status_error', 'body': {'message': 'InvalidParam'}})

    assert t._status.errors == ['[v1] InvalidParam']
    assert any('InvalidParam' in line for line in child.tail)


@pytest.mark.asyncio
async def test_unknown_child_event_is_logged_but_not_forwarded():
    t = _fanin_task()
    child = _child()

    await Task._on_child_event(t, child, {'event': 'apaevt_future_thing', 'body': {}})

    t._forward_task_event.assert_not_awaited()
    assert child.tail


def test_render_child_output_is_the_bare_line():
    """The mirror is meant to read like the child's console: prefixing every line with the
    event name buries the banner and any traceback in it.
    """
    assert Task._render_child_event({'event': 'output', 'body': {'output': 'Traceback...\n'}}) == 'Traceback...'


def test_render_child_blank_output_stays_blank():
    """Earned from the first live run: falling back to the event name turned every blank line
    the child printed into the literal word "output" (8 of them in one short run).
    """
    assert Task._render_child_event({'event': 'output', 'body': {'output': '\n'}}) == ''


def test_render_child_named_event_carries_its_message():
    rendered = Task._render_child_event({'event': 'apaevt_status_error', 'body': {'message': 'InvalidParam'}})
    assert rendered == 'apaevt_status_error: InvalidParam'


def test_render_child_event_without_text_falls_back_to_the_name():
    assert Task._render_child_event({'event': 'apaevt_trace', 'body': {'op': 'enter'}}) == 'apaevt_trace'


# ---------------------------------------------------------------------------
# _teardown_venv_children — the 8.5B guard wiring
# ---------------------------------------------------------------------------


def _teardown_task(children=None, guard=None):
    t = _task()
    t._venv_children = list(children or [])
    t._venv_guard = guard
    t._server = MagicMock()
    t.debug_message = MagicMock()
    return t


@pytest.mark.asyncio
async def test_teardown_closes_the_guard_when_there_are_no_children():
    """The case the guard exists for is precisely the case the per-child loop cannot cover.

    A child is appended to ``_venv_children`` only after ``_spawn_one_venv_child`` returns, while
    ``assign`` happens before the readiness wait — so a child that hung or died during startup
    leaves an EMPTY list and a live job. Tearing the guard down inside the loop would leak the
    handle and skip the backstop on the only path that needed it.
    """
    guard = create_autospec(ProcessGuard, instance=True)
    guard.terminate_all.return_value = 0
    t = _teardown_task(children=[], guard=guard)

    await Task._teardown_venv_children(t)

    guard.terminate_all.assert_called_once()
    guard.close.assert_called_once()
    assert t._venv_guard is None, 'a stale guard would be reused by a restarted task'


@pytest.mark.asyncio
async def test_teardown_tolerates_no_guard_at_all():
    """The legacy path never creates one, and teardown runs on every _terminated path."""
    t = _teardown_task(children=[], guard=None)

    await Task._teardown_venv_children(t)  # must not raise

    assert t._venv_guard is None


@pytest.mark.asyncio
async def test_teardown_reports_children_the_cooperative_phase_failed_to_reap():
    """If the job has to finish someone off, that is logged — otherwise the backstop silently
    masks a defect in the cooperative path it is supposed to be a backstop for.
    """
    guard = create_autospec(ProcessGuard, instance=True)
    guard.terminate_all.return_value = 0
    survivor = MagicMock()
    survivor.returncode = None  # kill_process did not reap it
    child = VenvChild(env_id='v1', name='v1', process=survivor, port=1, tmpfile='t.json')
    child.pump = None
    t = _teardown_task(children=[child], guard=guard)

    with patch('ai.modules.task.task_engine.kill_process', new=AsyncMock()):
        await Task._teardown_venv_children(t)

    logged = ' '.join(str(c) for c in t.debug_message.call_args_list)
    assert 'had to finish off' in logged and 'v1' in logged


# ---------------------------------------------------------------------------
# main engine subprocess environment (step 8.7A)
# ---------------------------------------------------------------------------


def test_main_env_never_carries_an_environment_id():
    # Pre-set on purpose: over a clean base a set-only implementation passes too, proving no pop.
    env = build_main_env({'PATH': '/x', VENV_ENV_ID_ENV: 'v1'}, None, False, avoid_mocks=False)
    assert VENV_ENV_ID_ENV not in env


@pytest.mark.parametrize('isolated', [True, False])
def test_main_env_stamps_the_isolated_flag_both_ways(isolated):
    # Both pre-set: a clean base passes on a set-only implementation and proves neither direction.
    base = {VENV_ENV_ID_ENV: 'v1', VENV_ISOLATED_ENV: '1'}
    env = build_main_env(base, None, isolated, avoid_mocks=False)
    assert (VENV_ISOLATED_ENV in env) is isolated


def test_main_env_differs_from_its_base_in_the_venv_keys_only():
    # The general invariant, and it must survive both increments: main's env differs from the
    # baseline in exactly the venv variables and nothing else. avoidMocks is held fixed across the
    # comparison -- it legitimately removes a third key.
    base = {'PATH': '/x', 'ROCKETRIDE_MOCK': '/m', VENV_ENV_ID_ENV: 'v1'}
    env = build_main_env(base, 'tok-1', True, avoid_mocks=False)
    assert set(base) - set(env) == {VENV_ENV_ID_ENV}
    assert set(env) - set(base) == {VENV_TOKEN_ENV, VENV_ISOLATED_ENV}
    assert env['PATH'] == '/x'
    assert env['ROCKETRIDE_MOCK'] == '/m'


def test_main_env_under_off_adds_the_flag_for_an_isolated_document():
    # Under =0 the flag IS added and IS inert: scoping_enabled(USE_OFF, True) is False, so the
    # document fact travels while the mode decides. "=0 adds nothing" is the tempting wording and
    # it is false -- a test written to it would have to be weakened until it asserted nothing.
    env = build_main_env({'PATH': '/x'}, None, True, avoid_mocks=False)
    assert env[VENV_ISOLATED_ENV] == '1'
    assert set(env) - {'PATH'} == {VENV_ISOLATED_ENV}


def test_main_env_without_a_run_token_adds_nothing():
    env = build_main_env({'PATH': '/x', VENV_ENV_ID_ENV: 'v1'}, None, False, avoid_mocks=False)
    assert set(env) == {'PATH'}


def test_main_env_strips_mocks_only_under_avoid_mocks():
    base = {'ROCKETRIDE_MOCK': '/m'}
    assert 'ROCKETRIDE_MOCK' in build_main_env(base, None, False, avoid_mocks=False)
    assert 'ROCKETRIDE_MOCK' not in build_main_env(base, None, False, avoid_mocks=True)


def test_main_env_does_not_mutate_the_base():
    # The call site passes os.environ itself, so a missing copy would pop the variables out of
    # the SERVER's own environment and degrade every later run.
    base = {VENV_ENV_ID_ENV: 'v1', VENV_ISOLATED_ENV: '1', 'ROCKETRIDE_MOCK': '/m'}
    build_main_env(base, 'tok', False, avoid_mocks=True)
    assert base == {VENV_ENV_ID_ENV: 'v1', VENV_ISOLATED_ENV: '1', 'ROCKETRIDE_MOCK': '/m'}
