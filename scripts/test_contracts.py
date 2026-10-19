import subprocess
import sys
from pathlib import Path


def test_contract_schemas_are_current():
    script = Path(__file__).with_name("gen_contracts.py")
    result = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
