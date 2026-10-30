"""读取随项目保存的不复权行情及现金分红数据。"""
import calendar
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CODES = ['601398', '601939']
ALL_CODES = ['601398', '601939', '600900']
DATA, DIV = {}, {}
for code in ALL_CODES:
    with (ROOT / 'data' / f'{code}_qq_none.json').open(encoding='utf-8') as f:
        rows = json.load(f)
    DATA[code] = {r[0]: {'open': float(r[1]), 'close': float(r[2]), 'suspended': False} for r in rows}
    with (ROOT / 'data' / f'{code}_dividends.json').open(encoding='utf-8') as f:
        dividends = json.load(f)['result']['data']
    DIV[code] = {r['EX_DIVIDEND_DATE'][:10]: float(r['PRETAX_BONUS_RMB']) / 10
                 for r in dividends if r['EX_DIVIDEND_DATE'] and r['PRETAX_BONUS_RMB']
                 and not r['BONUS_IT_RATIO']}

DATES = sorted(DATA['601398'])
# 停牌日沿用最近收盘价估值，但不得产生信号或成交。
for code in ALL_CODES:
    previous = None
    for day in DATES:
        if day in DATA[code]:
            previous = DATA[code][day]['close']
        elif previous is not None:
            DATA[code][day] = {'open': previous, 'close': previous, 'suspended': True}

def anniversary(date, months):
    month = date.month - 1 + months
    year, month = date.year + month // 12, month % 12 + 1
    return date.replace(year=year, month=month, day=min(date.day, calendar.monthrange(year, month)[1]))
