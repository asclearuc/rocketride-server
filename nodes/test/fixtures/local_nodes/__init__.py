"""Package marker for the `vtest_*` conflict fixtures (design §8.2).

Empty on purpose, and NOT modelled on ``nodes/src/nodes/__init__.py``: that one exists to
run a shared ``depends()`` baseline, and a shared requirement file here would destroy the
fixtures' defining property — one pin each and nothing else.

The directory is named ``local_nodes`` because the engine's ``--node_path=<dir>`` scans
exactly ``<dir>/local_nodes`` and imports its nodes as ``local_nodes.<node>``
(``services.cpp``, ``python/init.cpp``, ``docs/README-nodes.md``). That puts the fixtures
outside ``dist/server``, where ``REQUIREMENTS_GLOBS`` cannot reach their pins — which is the
whole reason they live here rather than in the shipped node tree.
"""
