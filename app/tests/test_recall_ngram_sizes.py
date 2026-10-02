import unittest

from recall_core import counter_recall, ngrams


class RecallNgramSizeTests(unittest.TestCase):
    def test_invalid_sizes_cannot_turn_missing_output_into_perfect_recall(self):
        for size in (0,-1,-100,True,False,1.5,'3',None):
            with self.subTest(size=size):
                with self.assertRaises(ValueError):
                    counter_recall(ngrams(['source','text','missing'],size),ngrams([],size))

    def test_positive_sizes_preserve_order_repetitions_and_short_inputs(self):
        tokens=['a','b','a','b']
        self.assertEqual([('a',),('b',),('a',),('b',)],ngrams(tokens,1))
        self.assertEqual([('a','b'),('b','a'),('a','b')],ngrams(tokens,2))
        self.assertEqual([('a','b','a'),('b','a','b')],ngrams(tokens,3))
        self.assertEqual([],ngrams(tokens,5))
        self.assertEqual([],ngrams([],3))
        self.assertEqual(2/3,counter_recall(ngrams(tokens,2),ngrams(['a','b','a'],2)))
        self.assertEqual(['a','b','a','b'],tokens)
