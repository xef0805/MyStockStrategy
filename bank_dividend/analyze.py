"""从已保存的回测数据生成曲线、年度收益和交易频率统计。"""
import csv
import datetime as dt
from pathlib import Path

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'results'

def rows(name):
    with (OUT/name).open(encoding='utf-8-sig',newline='') as f: return list(csv.DictReader(f))

def stats(period):
    tr=rows(f'{period}_2%_交易明细.csv'); daily=rows(f'{period}_2%_每日资产.csv')
    entry=[x for x in tr if x['类型']=='建仓买入']; cutoff=entry[-1]['日期']
    after=[x for x in tr if x['日期']>cutoff]
    wave=[x for x in after if x['类型'].startswith('wave')]
    div=[x for x in after if x['类型']=='分红再投买入']
    days=sorted({x['日期'] for x in after}); wdays=sorted({x['日期'] for x in wave})
    weeks={(dt.date.fromisoformat(x).isocalendar().year,dt.date.fromisoformat(x).isocalendar().week) for x in days}
    totalweeks={(dt.date.fromisoformat(x['日期']).isocalendar().year,dt.date.fromisoformat(x['日期']).isocalendar().week) for x in daily if x['日期']>cutoff}
    print(period,'建仓完成',cutoff,'后续天数',len(days),'波段天数',len(wdays),'波段笔数',len(wave),'分红买入笔数',len(div),'活跃周数',len(weeks),'总周数',len(totalweeks),'单周活动比',round(len(weeks)/len(totalweeks),4),'每月天数',round(len(days)/((dt.date.fromisoformat(daily[-1]['日期'])-dt.date.fromisoformat(cutoff)).days/365.2425*12),2))
    # 按年末市值计算年收益，第一年从初始本金计算。
    last={}
    for row in daily: last[row['日期'][:4]]=float(row['总资产'])
    prev=1_000_000
    for year,val in sorted(last.items()):
        print(year,round(val/prev*100-100,2),round(val/10000,2)); prev=val
    peak=0; peakdate=''; worst=0; interval=None
    for row in daily:
        v=float(row['总资产'])
        if v>peak: peak=v; peakdate=row['日期']
        draw=v/peak-1
        if draw<worst: worst=draw; interval=(peakdate,row['日期'])
    print('最大回撤',round(worst*100,2),interval)

if __name__=='__main__':
    from visualize import make_all
    for p in ('10年','5年','1年'): stats(p)
    make_all()
