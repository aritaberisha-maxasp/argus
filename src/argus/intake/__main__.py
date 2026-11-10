"""``python -m argus.intake sweep|removal``: the scheduled intake jobs in Azure (#125)."""

import sys

from argus.intake.jobs import main

sys.exit(main())
