import unittest
from datetime import datetime, timedelta, timezone
from fahad_ai_lens import fvg

START = datetime(2026, 11, 24, 14, 30, tzinfo=timezone.utc)


class FvgTest(unittest.TestCase):
    def bars(self, candles):
        return [dict(closed_at=(START + timedelta(minutes=i*5+j)).isoformat(),
                     open=o, high=h, low=l, close=c)
                for i, (o,h,l,c) in enumerate(candles) for j in range(1,6)]

    def test_first_retest(self):
        candles = [(99,100,98,99),(99,105,98,104),(104,107,103,106),(103,107,102,106)]
        for side, sign in [('CALL',1),('PUT',-1)]:
            data = candles if sign==1 else [(200-o,200-l,200-h,200-c) for o,h,l,c in candles]
            bars = self.bars(data)
            closed = START+timedelta(minutes=20)
            setup = fvg.candidate(bars, START, closed, side)
            self.assertEqual(setup['target'], setup['entry']+sign*10)
            self.assertIsNone(fvg.candidate(bars[1:], START, closed, side))
            repeated = self.bars(data + [data[-1]])
            self.assertIsNone(fvg.candidate(repeated, START, closed+timedelta(minutes=5), side))

    def test_confirmed_structure(self):
        rows = [dict(bucket=(START+timedelta(hours=4*i)).isoformat(), high=h, low=l)
                for i,(h,l) in enumerate([(4,2),(6,3),(4,1),(8,4),(6,2),(7,3)])]
        self.assertIsNone(fvg.bias(rows[:-1]))
        self.assertEqual(fvg.bias(rows),'CALL')
