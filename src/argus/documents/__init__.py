"""The original documents, kept exactly as received (ADR 0001, NF-01, NF-02).

Every email and each of its attachments is stored byte for byte in blob storage under its
SHA-256 fingerprint. The blob store never overwrites a file and checks the fingerprint again on
every read. The database keeps which file came from which email and which case it belongs to,
and writes the fingerprint into the audit log itself.
"""
