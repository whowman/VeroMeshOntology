"""Print the decision log's schema (service/src/veromesh/decisions.py, SCHEMA) for the backup tests."""

import re
from pathlib import Path

source = (Path(__file__).resolve().parents[2] / "service/src/veromesh/decisions.py").read_text()
print(re.search(r'SCHEMA = """(.*?)"""', source, re.S)[1])
