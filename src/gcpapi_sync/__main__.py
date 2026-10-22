"""``python -m gcpapi_sync`` runs the command line interface."""

import sys

from .cli import main

sys.exit(main())
