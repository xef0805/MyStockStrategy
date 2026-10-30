"""理想化近似：当日收盘价形成信号，并假设以同一收盘价成交。"""
import csv, math, datetime as dt
from pathlib import Path
from market import CODES, DATA, DIV, DATES, anniversary

ROOT = Path(__file__).resolve().parent

def run(start, threshold, codes=None):
    CODES = list(codes or ['601398', '601939'])
    days=[x for x in DATES if start<=x<='2026-10-30']
    corecash=900000.; wavecash=100000.; lots={c:[] for c in CODES}; waveqty={c:0 for c in CODES}
    used=set(); entry_count=0; pending=None
    fees=taxes=dividend=0.; sales=buys=signals=skips=0; trades=[]; equity=[]; daily=[]
    def qty(c): return sum(x['qty'] for x in lots[c])
    def value(day,field): return corecash+wavecash+sum(qty(c)*DATA[c][day][field] for c in CODES)
    def fee(v,side,day): return max(5,v*.0003)+v*(.00001+((.001 if day<'2023-08-28' else .0005) if side=='sell' else 0))
    def purchase(c,day,amount,kind):
        nonlocal corecash,wavecash,fees,buys
        if DATA[c][day]['suspended']: return False
        cash=wavecash if kind=='wave' else corecash
        budget=min(amount,cash); price=DATA[c][day]['close']
        n=math.floor(budget/price/100)*100
        while n>0 and n*price+fee(n*price,'buy',day)>budget: n-=100
        if not n: return False
        v=n*price; f=fee(v,'buy',day); fees+=f
        if kind=='wave': wavecash-=v+f; waveqty[c]+=n; buys+=1
        else: corecash-=v+f
        lots[c].append({'qty':n,'date':dt.date.fromisoformat(day),'divs':[]})
        trades.append([day,kind+'买入',c,n,round(price,4),round(v,2),round(f,2),0]); return True
    for i,day in enumerate(days):
        date=dt.date.fromisoformat(day); week=date.isocalendar()[:2]
        for c in CODES:
            div=DIV[c].get(day,0)
            if div:
                for lot in lots[c]: lot['divs'].append(div)
                d=qty(c)*div; dividend+=d; corecash+=d
        pending=None
        if threshold is not None and entry_count>=10 and i>0:
            sig=[]
            for c in CODES:
                if DATA[c][day]['suspended']: continue
                prev=DATA[c][days[i-1]]['close']; ref=round(prev-DIV[c].get(day,0)+1e-8,2)
                change=DATA[c][day]['close']/ref-1
                if change>threshold: sig.append((abs(change),c,'sell'))
                elif change < -threshold: sig.append((abs(change),c,'buy'))
            if sig:
                _,c,side=max(sig); pending=(c,side); signals+=1
        entry=entry_count<10 and (week,'buy') not in used
        if entry:
            c=CODES[entry_count%len(CODES)]
            if purchase(c,day,90000.,'建仓'):
                entry_count+=1; used.add((week,'buy'))
        elif entry_count>=10:
            if threshold is not None and pending:
                c,side=pending
                if (week,side) not in used:
                    if side=='buy':
                        if purchase(c,day,value(day,'close')*.05,'wave'): used.add((week,'buy'))
                        else: skips+=1
                    else:
                        p=DATA[c][day]['close']
                        n=min(waveqty[c],math.floor(value(day,'close')*.05/p/100)*100)
                        if n:
                            v=n*p; f=fee(v,'sell',day); tax=0.; remaining=n
                            while remaining:
                                lot=lots[c][0]; take=min(remaining,lot['qty']); acquired=lot['date']
                                rate=0 if date>anniversary(acquired,12) else (.2 if date<=anniversary(acquired,1) else .1)
                                tax+=take*sum(lot['divs'])*rate
                                lot['qty']-=take; remaining-=take
                                if not lot['qty']: lots[c].pop(0)
                            waveqty[c]-=n; wavecash+=v-f-tax; taxes+=tax; fees+=f; sales+=1
                            trades.append([day,'wave卖出',c,n,round(p,4),round(v,2),round(f,2),round(tax,2)]); used.add((week,'sell'))
                        else: skips+=1
            # 分红统一转入长期仓，使用未占用的每周买入额度，按两只长期仓市值平衡。
            if (week,'buy') not in used:
                available=[c for c in CODES if not DATA[c][day]['suspended']]
                if available:
                    c=min(available,key=lambda x:(qty(x)-waveqty[x])*DATA[x][day]['close'])
                    if purchase(c,day,min(corecash,value(day,'close')*.05),'分红再投'): used.add((week,'buy'))
        total=value(day,'close'); equity.append(total)
        daily.append([day,round(total,2),round(corecash,2),round(wavecash,2)]+[qty(c) for c in CODES])
    peak=equity[0]; mdd=0
    for v in equity: peak=max(peak,v); mdd=min(mdd,v/peak-1)
    n=(dt.date.fromisoformat(days[-1])-dt.date.fromisoformat(days[0])).days/365.2425
    return {'start':days[0],'end':days[-1],'final':equity[-1],'annual':(equity[-1]/1e6)**(1/n)-1,'mdd':mdd,'dividend':dividend,'fees':fees,'tax':taxes,'buys':buys,'sells':sales,'signals':signals,'skips':skips,'wavecash':wavecash,'corecash':corecash,'trades':trades,'daily':daily,'waveqty':waveqty}

if __name__=='__main__':
    out=ROOT / 'results'; out.mkdir(exist_ok=True); results=[]
    for label,start in [('10年','2016-10-30'),('5年','2021-10-30'),('1年','2025-10-30')]:
        for t in [None,.01,.015,.02,.025,.03]:
            name='不波段' if t is None else str(t*100).rstrip('0').rstrip('.')+'%'
            r=run(start,t)
            print(label,name,{k:round(v,4) if isinstance(v,float) else v for k,v in r.items() if k not in ('trades','daily','waveqty')},flush=True)
            results.append({'period':label,'threshold':name,**{k:v for k,v in r.items() if k not in ('trades','daily','waveqty')}})
            if t is None or t==.02:
                with (out / f'{label}_{name}_交易明细.csv').open('w',encoding='utf-8-sig',newline='') as f:
                    w=csv.writer(f); w.writerow(['日期','类型','股票','股数','收盘成交价','成交金额','交易费用','分红税']); w.writerows(r['trades'])
                with (out / f'{label}_{name}_每日资产.csv').open('w',encoding='utf-8-sig',newline='') as f:
                    w=csv.writer(f); w.writerow(['日期','总资产','分红再投入资金','波段现金','工商银行股数','建设银行股数']); w.writerows(r['daily'])
    with (out / '回测汇总.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=results[0].keys()); w.writeheader(); w.writerows(results)
