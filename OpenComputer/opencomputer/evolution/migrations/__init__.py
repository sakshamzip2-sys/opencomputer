"""SQL migration files for the OpenComputer Evolution storage layer.

Each file is named ``NNN_<description>.sql`` where NNN is a zero-padded
integer version number.  The migration runner in ``storage.py`` discovers
these files, sorts by version, and applies any that have not yet been
recorded in the ``schema_version`` table.

When Sub-project F ships its framework (``opencomputer/agent/migrations/``),
this directory will be consumed by that framework and
``storage.apply_pending`` replaced with a thin call into it.
See ``OpenComputer/docs/evolution/design.md §5.1`` for the refactor path.
"""
