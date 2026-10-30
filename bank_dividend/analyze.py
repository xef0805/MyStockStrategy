"""从已保存的回测数据生成曲线、年度收益和交易频率统计。"""
import csv
import datetime as dt
from pathlib import Path

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'results'

def rows(name):
    with (OUT/name).open(encoding='utf-8-sig',newline='') as f: return list(csv.DictReader(f))

def curve(period):
    active=rows(f'{period}_2%_每日资产.csv'); baseline=rows(f'{period}_不波段_每日资产.csv')
    width,height=1100,490; l,r,t,b=90,30,65,65; pw=width-l-r; ph=height-t-b
    vals=[float(x['总资产'])/10000 for a in (active,baseline) for x in a]
    low=0; high=max(vals)*1.09; start=dt.date.fromisoformat(active[0]['日期']); end=dt.date.fromisoformat(active[-1]['日期'])
    def x(d): return l+(dt.date.fromisoformat(d)-start).days/(end-start).days*pw
    def y(v): return t+ph-(v-low)/(high-low)*ph
    svg=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
         '<rect width="100%" height="100%" fill="#ffffff"/>',
         f'<text x="{l}" y="32" font-family="Microsoft YaHei,Arial" font-size="23" font-weight="bold" fill="#172b4d">{period}账户资产曲线（万元）</text>',
         '<text x="90" y="54" font-family="Microsoft YaHei,Arial" font-size="13" fill="#6b778c">收盘价信号与收盘价成交，含分红再投入；理想化假设</text>']
    for i in range(6):
        val=high*i/5; yy=y(val)
        svg += [f'<line x1="{l}" y1="{yy:.2f}" x2="{width-r}" y2="{yy:.2f}" stroke="#e6eaf0"/>',f'<text x="{l-10}" y="{yy+5:.2f}" text-anchor="end" font-family="Arial" font-size="13" fill="#67758a">{val:.0f}</text>']
    step=2 if (end-start).days>2500 else 1
    lastyear=start.year
    for yr in range(start.year,end.year+1):
        if (yr-start.year)%step: continue
        d=max(start,dt.date(yr,1,1)); xx=x(d.isoformat())
        svg.append(f'<text x="{xx:.2f}" y="{height-b+24}" text-anchor="middle" font-family="Arial" font-size="12" fill="#67758a">{yr}</text>')
    for data,color,style in [(baseline,'#9aa6b2','5 4'),(active,'#1673b1','')]:
        pts=' '.join(f'{x(a["日期"]):.2f},{y(float(a["总资产"])/10000):.2f}' for a in data)
        svg.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2.3" stroke-dasharray="{style}"/>')
    svg += [f'<text x="{width-320}" y="34" font-family="Microsoft YaHei,Arial" font-size="14" fill="#1673b1">━━ 2% 波段</text>',
            f'<text x="{width-185}" y="34" font-family="Microsoft YaHei,Arial" font-size="14" fill="#718096">┄┄ 不做波段</text>', '</svg>']
    (OUT/f'{period}_收益曲线.svg').write_text('\n'.join(svg),encoding='utf-8')

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
    for p in ('10年','5年','1年'): curve(p); stats(p)
