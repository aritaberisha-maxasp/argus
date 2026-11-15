"""``python -m argus.intake sweep|removal|digest``: the scheduled jobs in Azure (#125, #61)."""

import sys

from argus.intake.jobs import main

sys.exit(main())
