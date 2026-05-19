from __future__ import annotations

import unittest

from tests.architecture.checks import DISALLOWED_ROOTS, find_legacy_import_violations


class LegacyImportCheckTests(unittest.TestCase):
    def test_app_owned_code_has_no_legacy_imports(self) -> None:
        self.assertEqual(find_legacy_import_violations(), [])

    def test_util_is_now_treated_as_legacy_import_root(self) -> None:
        self.assertIn("util", DISALLOWED_ROOTS)
        self.assertIn("utils", DISALLOWED_ROOTS)


if __name__ == "__main__":
    unittest.main()
