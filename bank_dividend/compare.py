"""以相同建仓、分红及波段规则比较银行和长江电力。"""
import csv
from pathlib import Path
from backtest import run

OUT = Path(__file__).resolve().parent / 'results'
PORTFOLIOS = [
    ('双银行', ['601398', '601939']),
    ('工行+长江电力', ['601398', '600900']),
    ('长江电力', ['600900']),
]
PERIODS = [('10年','2016-10-30'),('5年','2021-10-30'),('1年','2025-10-30')]

def write(path, headers, rows):
    with path.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f); writer.writerow(headers); writer.writerows(rows)

if __name__=='__main__':
    results=[]
    for period,start in PERIODS:
        for name,codes in PORTFOLIOS:
            r=run(start,.02,codes)
            print(period,name,round(r['final']/10000,2),round(r['annual']*100,2),round(r['mdd']*100,2),'波段',r['buys'],r['sells'],flush=True)
            results.append([period,name,','.join(codes),r['start'],r['end'],round(r['final'],2),round(r['annual']*100,4),round(r['mdd']*100,4),round(r['dividend'],2),r['buys'],r['sells'],round(r['fees'],2),round(r['tax'],2)])
            write(OUT/f'组合_{period}_{name}_每日资产.csv',['日期','总资产','分红再投入资金','波段现金']+[c+'股数' for c in codes],r['daily'])
            write(OUT/f'组合_{period}_{name}_交易明细.csv',['日期','类型','股票','股数','收盘成交价','成交金额','交易费用','分红税'],r['trades'])
    write(OUT/'组合比较.csv',['区间','组合','股票代码','开始','结束','期末资产','年化收益率%','最大回撤%','累计税前分红','波段买入次数','波段卖出次数','交易费用','已扣股息税'],results)
