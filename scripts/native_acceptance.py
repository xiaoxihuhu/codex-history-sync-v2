from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "test_native_architecture"
    )
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    payload = {
        "action": "native-acceptance",
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "successful": result.wasSuccessful(),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
