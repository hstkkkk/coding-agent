import unittest

from intervals import merge_intervals


class MergeIntervalsTests(unittest.TestCase):
    def test_touching_intervals_are_merged(self):
        self.assertEqual(merge_intervals([(1, 2), (2, 4)]), [(1, 4)])

    def test_overlapping_intervals_are_merged(self):
        self.assertEqual(merge_intervals([(1, 3), (2, 5)]), [(1, 5)])


if __name__ == "__main__":
    unittest.main()

