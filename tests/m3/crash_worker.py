"""Real process termination for registered transition fault points."""

import os
from pathlib import Path
import sys

from rsi_lab.serialization import canonical_bytes
from rsi_lab.studies.transition import run_transition

output = Path(sys.argv[1])
point = sys.argv[2] if len(sys.argv) > 2 else None


def fault_hook(observed):
    if observed == point:
        os._exit(86)


result = run_transition(output, fault_hook=fault_hook)
sys.stdout.buffer.write(canonical_bytes(result))
sys.stdout.buffer.write(b"\n")
