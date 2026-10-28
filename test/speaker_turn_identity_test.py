import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))
from speaker_turn_identity import interval_identity


class IntervalIdentityTest(unittest.TestCase):
    profiles = [{'name': 'Alice', 'model': 'fixture', 'embedding': [1., 0., 0.]},
                {'name': 'Bob', 'model': 'fixture', 'embedding': [0., 1., 0.]}]

    def identify(self, vector, **options):
        return interval_identity(vector, self.profiles, model='fixture', duration=options.pop('duration', 6), **options)

    def test_clear_interval_and_unrelated_neighbour_are_independent(self):
        self.assertEqual(self.identify([.6, 0, .8])['name'], 'Alice')
        self.assertIsNone(self.identify([0, 0, 1])['name'])

    def test_short_and_overlapping_speech_never_get_a_name(self):
        for duration in (.5, 1, 3.99):
            self.assertEqual(self.identify([1, 0, 0], duration=duration)['reason'], 'short_interval')
        self.assertEqual(self.identify([1, 0, 0], overlapping=True)['reason'], 'overlap')

    def test_lookalikes_and_low_similarity_abstain(self):
        self.assertEqual(self.identify([.6, .55, .58])['reason'], 'ambiguous_identity')
        self.assertEqual(self.identify([.3, .2, .93])['reason'], 'low_similarity')

    def test_invalid_vectors_and_times_fail(self):
        for vector in ([0, 0, 0], [math.nan, 0, 1], [math.inf, 1, 0], [[1, 0, 0]], [1, 0]):
            with self.assertRaises(ValueError):
                self.identify(vector)
        for duration in (0, -1, math.nan, math.inf):
            with self.assertRaises(ValueError):
                self.identify([1, 0, 0], duration=duration)

    def test_model_mismatch_or_duplicate_profiles_cannot_pass(self):
        for profiles in ([{**self.profiles[0], 'model': 'other'}], self.profiles + [self.profiles[0]]):
            with self.assertRaises(ValueError):
                interval_identity([1, 0, 0], profiles, model='fixture', duration=6)

    def test_empty_registry_abstains(self):
        self.assertEqual(interval_identity([1, 0, 0], [], model='fixture', duration=6)['reason'], 'no_profiles')


if __name__ == '__main__':
    unittest.main()
