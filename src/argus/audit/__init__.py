"""The nightly copy of the audit log into the locked document storage (issue #83, ADR 0005).

The hash chain in the database catches a change to an old event, but not the newest events
being cut off. Every night :func:`argus.audit.nightly.write_copy` writes the new events into a
file nobody can change or delete, and :func:`argus.audit.nightly.check_copies` compares the
database with all those files. Run both with ``python -m argus.audit``.
"""
