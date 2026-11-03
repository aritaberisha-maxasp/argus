"""``python -m argus.audit``: the nightly copy of the audit log and its check (#83)."""

import sys

from argus.audit.nightly import main

sys.exit(main())
