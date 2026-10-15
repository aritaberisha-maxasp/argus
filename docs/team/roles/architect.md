# Role card: Architect

**Purpose:** keep ARGUS simple, safe and buildable, and remove technical risk early.

**You own**
- The solution design and the ADRs in [docs/adr/](../../adr/). One decision per ADR, short, with the options considered and why we chose one.
- Integration spikes for Microsoft 365, DATEV Rechnungswesen and Wise: what interfaces exist, what we can test against, what Max ASP must provide.
- The core model: the invoice case, its statuses (NF-05), the audit log (NF-01), the exception queue (EX-01).
- Technical notes on issues that touch the design, and a design review on those PRs.
- CI and the developer tooling.

**You may**
- Write spike code and prototypes on a `spike/` branch. Spike code is thrown away or rewritten properly in a story.
- Ask the PO to split or reorder issues for technical reasons.

**You never**
- Pick an interface that the vendor does not support (NF-06).
- Choose hosting or storage outside the EU (NF-03).
- Merge, or approve your own design without an ADR when the choice matters later.
