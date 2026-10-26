import tempfile
import unittest

import golden


class CliGoldenTest(unittest.TestCase):
    maxDiff = None

    def test_cli_matches_golden(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(golden.run_cli_sequence(tmp), golden.load_golden())


if __name__ == "__main__":
    unittest.main()
