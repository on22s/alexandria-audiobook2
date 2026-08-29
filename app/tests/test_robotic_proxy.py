import unittest

from experiments.robotic_proxy import mean_chance


class RoboticChanceTest(unittest.TestCase):
    def test_chance_is_computed_on_the_metric_population(self):
        all_groups = [["a", "b"], ["a", "b", "c", "d"]]
        metric_groups = [all_groups[0]]
        self.assertEqual(0.5, mean_chance(metric_groups))
        self.assertNotEqual(mean_chance(all_groups), mean_chance(metric_groups))


if __name__ == "__main__":
    unittest.main()
