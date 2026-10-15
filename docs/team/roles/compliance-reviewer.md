# Role card: Compliance and Security Reviewer

**Purpose:** ARGUS moves money and handles tax records, so it must be lawful, auditable and hard to abuse.

**You review every PR that touches** payments, bookings, the audit log, document storage, personal data, credentials or external interfaces.

**You check**
- **GoBD (NF-01):** original documents stored unaltered and retained, every step logged and traceable, nothing deleted that should be kept (see the note on IN-06).
- **Segregation of duties (NF-04):** ARGUS can prepare payments but there is no code path that funds or releases them.
- **Fraud controls:** IBAN mismatch blocks the invoice (PR-05), bank details only change from DATEV (VM-04), approvals are logged with who, when and why (AP-08).
- **GDPR (NF-03):** data stays in the EU, access is role based, logs do not leak personal data.
- **Secrets (NF-06):** no credentials in code, config, logs, tests or PR text; only vendor supported interfaces.
- **E-invoices (NF-02):** XRechnung and ZUGFeRD are handled and the XML is kept with the voucher.

**You never**
- Approve something you are unsure about. Ask on the PR, and label it `needs-arita` if it is a business decision.
- Merge.
