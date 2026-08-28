import unittest

from intervals import merge_intervals


class HiddenMergeIntervalsTests(unittest.TestCase):
    def test_nested_and_disjoint_intervals(self):
        self.assertEqual(
            merge_intervals([(8, 10), (1, 5), (2, 3), (12, 13)]),
            [(1, 5), (8, 10), (12, 13)],
        )

    def test_empty_input(self):
        self.assertEqual(merge_intervals([]), [])


if __name__ == "__main__":
    unittest.main()

