"""检查仓位、交易频率及已公布回测结果。"""
import datetime as dt
import unittest
from backtest import run

class BacktestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = run('2016-10-30', .02)

    def test_reproducible_result(self):
        self.assertAlmostEqual(self.r['final'], 3339970.5474, places=3)
        self.assertAlmostEqual(self.r['daily'][-1][1], self.r['final'], places=2)

    def test_weekly_limit_and_wave_stock(self):
        counts, wave = {}, {'601398': 0, '601939': 0}
        for date, kind, stock, n, *_ in self.r['trades']:
            week=dt.date.fromisoformat(date).isocalendar()[:2]
            side='sell' if kind=='wave卖出' else 'buy'
            counts[(week,side)] = counts.get((week,side),0)+1
            self.assertLessEqual(counts[(week,side)],1)
            if kind=='wave买入': wave[stock]+=n
            if kind=='wave卖出': wave[stock]-=n
            self.assertGreaterEqual(wave[stock],0)
        self.assertEqual(wave, self.r['waveqty'])

    def test_cash_nonnegative_and_base_buys(self):
        self.assertEqual(sum(t[1]=='建仓买入' for t in self.r['trades']),10)
        for day in self.r['daily']:
            self.assertGreaterEqual(day[2],-0.01)
            self.assertGreaterEqual(day[3],-0.01)

    def test_hold_only_has_no_wave_orders(self):
        result=run('2025-10-30',None)
        self.assertEqual(result['buys'],0)
        self.assertEqual(result['sells'],0)
        self.assertEqual(result['wavecash'],100000)

    def test_power_suspension_does_not_trade_and_entry_completes(self):
        from market import DATA
        result=run('2021-10-30',.02,['601398','600900'])
        entries=[t for t in result['trades'] if t[1]=='建仓买入']
        self.assertEqual(len(entries),10)
        for day, _, code, *_ in result['trades']:
            self.assertFalse(DATA[code][day]['suspended'])
        self.assertTrue(DATA['600900']['2021-11-29']['suspended'])

if __name__=='__main__': unittest.main()
