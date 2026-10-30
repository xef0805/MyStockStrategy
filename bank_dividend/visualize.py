"""生成可读的账户资产曲线、方案差额和关键时点数据表。"""
import csv
import datetime as dt
import math
from pathlib import Path

OUT=Path(__file__).resolve().parent/'results'

def read(name):
    with (OUT/name).open(encoding='utf-8-sig',newline='') as f:
        return list(csv.DictReader(f))

def checkpoints(daily, period):
    end=dt.date.fromisoformat(daily[-1]['日期'])
    start=dt.date.fromisoformat(daily[0]['日期'])
    selected=[daily[0]]
    if period=='1年':
        targets=[end-dt.timedelta(days=n) for n in (300,240,180,120,60)]
    else:
        targets=[dt.date(y,12,31) for y in range(start.year,end.year)]
    for target in targets:
        candidates=[r for r in daily if dt.date.fromisoformat(r['日期'])<=target]
        if candidates and candidates[-1]['日期']!=selected[-1]['日期']: selected.append(candidates[-1])
    if selected[-1]['日期']!=daily[-1]['日期']: selected.append(daily[-1])
    return selected

def svg_text(x,y,value,size=15,color='#24344a',anchor='start',weight='normal'):
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-family="Microsoft YaHei,Arial,sans-serif" font-size="{size}" font-weight="{weight}" fill="{color}">{value}</text>'

def graph(period, lines, title, filename, show_delta):
    data=[read(file) for _,_,file in lines]
    by_date=[{r['日期']:float(r['总资产'])/10000 for r in ds} for ds in data]
    dates=[r['日期'] for r in data[0]]
    assert all(list(d)==dates for d in by_date), '曲线日期不一致'
    start=dt.date.fromisoformat(dates[0]); end=dt.date.fromisoformat(dates[-1]); span=(end-start).days
    width=1500; left=115; right=120; top=138; plot_h=360; pw=width-left-right
    bottom_top=620; delta_h=155; table_top=890 if show_delta else 640
    picks=checkpoints(data[0],period); table_bottom=table_top+len(picks)*34+66
    height=table_bottom+40
    vals=[v for series in by_date for v in series.values()]
    vmin=min(vals); vmax=max(vals); pad=max(2,(vmax-vmin)*.12)
    lo=max(0,math.floor((vmin-pad)/10)*10); hi=math.ceil((vmax+pad)/10)*10
    x=lambda d:left+(dt.date.fromisoformat(d)-start).days/span*pw
    y=lambda v:top+plot_h-(v-lo)/(hi-lo)*plot_h
    s=[f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
       '<rect width="100%" height="100%" fill="#fff"/>',
       svg_text(left,48,title,27,'#132f4c',weight='bold'),
       svg_text(left,78,'账户资产 = 股票市值 + 现金（万元）；含分红再投入。纵轴非零起点。',16,'#52657a'),
       svg_text(left,104,'收盘价同时用于信号和成交，属于理想化假设。',15,'#9b5e00')]
    for i in range(6):
        v=lo+(hi-lo)*i/5; yy=y(v)
        s += [f'<line x1="{left}" y1="{yy:.2f}" x2="{left+pw}" y2="{yy:.2f}" stroke="#e4eaf0"/>',svg_text(left-14,yy+5,f'{v:.0f}',14,'#607386','end')]
    for p in picks:
        xx=x(p['日期']); s.append(f'<line x1="{xx:.2f}" y1="{top}" x2="{xx:.2f}" y2="{top+plot_h}" stroke="#eff2f5"/>')
    prev_x=-1000; prev_label=''
    for p in picks:
        d=p['日期']; xx=x(d); label=d[:7] if period=='1年' else d[:4]
        if xx-prev_x>=85 and label!=prev_label:
            s.append(svg_text(xx,top+plot_h+22,label,13,'#607386','middle'))
            prev_x=xx; prev_label=label
    for idx in reversed(range(len(lines))):
        name,color,_=lines[idx]; values=by_date[idx]
        pts=' '.join(f'{x(d):.2f},{y(values[d]):.2f}' for d in dates)
        s.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="3" stroke-linejoin="round"/>')
        for p in picks:
            d=p['日期']; s.append(f'<circle cx="{x(d):.2f}" cy="{y(values[d]):.2f}" r="3.8" fill="{color}"/>')
    for i,(name,color,_) in enumerate(lines):
        s.append(svg_text(left+i*310,top+plot_h+53,f'━━ {name}：{by_date[i][dates[-1]]:.2f} 万',15,color,weight='bold'))
    if show_delta:
        ds=[by_date[0][d]-by_date[1][d] for d in dates]
        bound=max(1,max(abs(v) for v in ds))*1.15
        dy=lambda v:bottom_top+delta_h/2-v/bound*(delta_h/2)
        s += [svg_text(left,bottom_top-20,'波段方案比不做波段多 / 少（万元）',18,'#253f59',weight='bold')]
        for v in (-bound,0,bound):
            yy=dy(v); s += [f'<line x1="{left}" y1="{yy:.2f}" x2="{left+pw}" y2="{yy:.2f}" stroke="#dce4ed"/>',svg_text(left-14,yy+5,f'{v:.1f}',13,'#607386','end')]
        s.append('<polyline points="'+' '.join(f'{x(d):.2f},{dy(v):.2f}' for d,v in zip(dates,ds))+'" fill="none" stroke="#df7e24" stroke-width="2.5"/>')
        s.append(svg_text(left+pw,bottom_top+delta_h+30,f'期末差额 {ds[-1]:+.2f} 万元',16,'#b35a0b','end',weight='bold'))
    s += [svg_text(left,table_top,'关键时点账户资产（万元）',19,'#253f59',weight='bold')]
    headers=['交易日']+[line[0] for line in lines]+(['波段减对照'] if show_delta else [])
    cols=[left,left+255,left+550,left+845,left+1090]
    for xx,label in zip(cols,headers): s.append(svg_text(xx,table_top+38,label,15,'#52657a',weight='bold'))
    records=[]
    for idx,p in enumerate(picks):
        d=p['日期']; vals=[series[d] for series in by_date]; values=[d]+[f'{v:.2f}' for v in vals]+([f'{vals[0]-vals[1]:+.2f}'] if show_delta else [])
        yy=table_top+72+idx*34
        s.append(f'<line x1="{left}" y1="{yy+9}" x2="{left+pw}" y2="{yy+9}" stroke="#eef1f4"/>')
        for j,(xx,v) in enumerate(zip(cols,values)): s.append(svg_text(xx,yy,v,15,lines[j-1][1] if 0<j<=len(lines) else '#293d52'))
        records.append(values)
    s.append('</svg>'); (OUT/filename).write_text('\n'.join(s),encoding='utf-8')
    with (OUT/filename.replace('.svg','_关键时点.csv')).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.writer(f); w.writerow(headers); w.writerows(records)

def make_all():
    for p in ('10年','5年','1年'):
        graph(p,[('2% 波段','#1268a8',f'{p}_2%_每日资产.csv'),('不做波段','#82919e',f'{p}_不波段_每日资产.csv')],f'{p}银行组合：账户资产与差额',f'{p}_收益曲线.svg',True)
        graph(p,[('双银行','#1268a8',f'组合_{p}_双银行_每日资产.csv'),('工行＋长江电力','#d77a16',f'组合_{p}_工行+长江电力_每日资产.csv'),('长江电力','#3b9176',f'组合_{p}_长江电力_每日资产.csv')],f'{p}三种组合：账户资产对比',f'组合比较_{p}.svg',False)

if __name__=='__main__': make_all()
