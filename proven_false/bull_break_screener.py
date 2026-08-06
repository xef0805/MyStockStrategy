# -*- coding: utf-8 -*-
"""
bull_break_screener.py —— 一阳穿四线选股（A股沪深主板，严格版 + 打分排名）
=========================================================================
选股逻辑：当日一根大阳线向上穿越 5/10/20/30 日均线
  1) 当日收阳线：现价（收盘后为收盘价）> 今开
  2) 今开低于至少 --min-cross 根均线（默认 2，即至少穿越两条线）
  3) 现价站上全部四根均线：price > max(MA5, MA10, MA20, MA30)
  4) 大阳线：当日涨幅 >= --min-pct（默认 5%）
  5) 放量：量比 >= --min-vr（默认 1.5，今日成交量 / 前5日均量）
  6) 位置：偏离 MA30 <= --max-dev%（默认 12，只买刚起涨的，排除高位长阳）
  7) 趋势：MA30 走平或向上（5日斜率 >= 0，--no-ma30-up 可关）
  8) 新鲜度：近 5 个交易日内首次收盘站上四线（--no-fresh 可关，
     排除高位反复活跃的老龙头）

股票池（默认）：仅沪深主板（沪 600/601/603/605，深 000/001/002/003），
剔除 ST/*ST、科创板(688)、创业板(300/301/302)、北交所。
可用 --all-boards 放开板块限制，--include-st 保留 ST 股。

均线口径：MA_n =（当日现价 + 前 n-1 个交易日收盘价）/ n，与行情软件实时均线一致。

实现方式（两段式，兼顾速度与准确性）：
  初筛：新浪轻量K线接口（不复权，每只约0.1秒，含量价比过滤）
  复核：候选股用新浪前复权日线复验（默认开启，--no-verify 关闭），
        剔除除权除息造成的均线误穿越。本地运行若不在乎耗时，
        可把 get_kline_lite 换成前复权路径全程复权。

打分模型（默认开启，满分 100，经验权重，非统计概率）：
  穿越强度 15：穿越均线数 2/3/4 根 → 9/13/15 分
  K线质量  15：阳线实体率×10 + 收盘在当日区间位置×5
  量能     20：量比 2.5~4 最佳(20)，1.5~2.5 → 16；4~6 天量仅 9，≥6 → 3
  MACD     25：DIF>DEA(10) + 红柱(5) + 红柱放大(5) + 近3日金叉(5)
  KDJ      10：J∈[50,90) 强势区(10)，100~120 → 3，≥120 → 0（超买重罚）
  位置     15：偏离MA30 ≤5% → 15，5~8% → 12，8~12% → 8，12~15% → 3
指标基于轻量K线（不复权）计算，除权除息附近可能略有失真。

输出：每次运行自动在脚本同级创建 output/YYYYMMDD_HHMM/ 文件夹，
      内含 results.csv（选股+打分结果）和 run.log（运行日志）。

数据源：新浪财经（免费，无需 token）。
依赖：requests、akshare（前复权复核用）、pandas（打分/导出用）。

用法：
  python bull_break_screener.py                 # 沪深主板扫描 + 打分排名（默认）
  python bull_break_screener.py --no-score      # 只选股不打分
  python bull_break_screener.py --no-verify     # 跳过前复权复核（更快）
  python bull_break_screener.py --min-pct 3 --min-cross 1 --min-vr 0   # 放宽条件
  python bull_break_screener.py --strict        # 开盘须低于全部四线
  python bull_break_screener.py --date 2026-08-05   # 回看历史某天
"""

import argparse
import json
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests

HEADERS = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
LIST_URL = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=hs_a")
HQ_URL = "https://hq.sinajs.cn/list={symbol}"
KLINE_LITE_URL = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/x/"
                  "CN_MarketDataService.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={n}")

_KLINE_CACHE = {}   # symbol -> DataFrame(day/open/high/low/close/volume)，初筛打分复用
_AK = None          # akshare 惰性单例
log = logging.getLogger("bull_break")


def setup_logger(log_path):
    """同时输出到控制台和 run.log。"""
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(ch)
    log.addHandler(fh)


# ---------------------------------------------------------------- 数据获取

def _get(url, retries=3, timeout=15):
    for attempt in range(retries):
        try:
            return requests.get(url, headers=HEADERS, timeout=timeout)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(0.8 + attempt)


def get_market_date():
    """用贵州茅台的实时行情获取当前交易日期（用于判断历史K线是否已含今日）。"""
    try:
        r = _get(HQ_URL.format(symbol="sh600519"))
        return r.text.split(",")[30]          # 如 2026-08-06
    except Exception:
        return None


def get_stock_list():
    """获取全部A股列表及当日快照（代码/名称/今开/现价/昨收/涨跌幅/成交量）。"""
    stocks, page = [], 1
    while True:
        r = _get(LIST_URL.format(page=page))
        data = json.loads(r.text)
        if not data:
            break
        for d in data:
            try:
                stocks.append({
                    "symbol": d["symbol"],                 # 如 sh600519 / sz000001 / bj920000
                    "code": d["code"],
                    "name": d["name"],
                    "open": float(d["open"]),
                    "price": float(d["trade"]),            # 现价（收盘后为收盘价）
                    "pre_close": float(d["settlement"]),   # 昨收
                    "pct": float(d["changepercent"]),      # 涨跌幅 %
                    "volume": float(d["volume"]),          # 当日成交量（股）
                })
            except (KeyError, ValueError):
                continue
        page += 1
        time.sleep(0.1)
    return stocks


def is_main_board(symbol):
    """沪深主板：沪 600/601/603/605，深 000/001/002/003（002/003 为原中小板，已并入主板）。"""
    return (symbol.startswith(("sh600", "sh601", "sh603", "sh605"))
            or symbol.startswith(("sz000", "sz001", "sz002", "sz003")))


def get_kline_lite(symbol, need=80, retries=3):
    """轻量K线（不复权）：DataFrame(day/open/high/low/close/volume)，带缓存。"""
    if symbol in _KLINE_CACHE:
        return _KLINE_CACHE[symbol]
    import pandas as pd
    for attempt in range(retries):
        try:
            r = _get(KLINE_LITE_URL.format(symbol=symbol, n=need), timeout=10)
            full = pd.DataFrame(json.loads(re.search(r"x\((.*)\)", r.text, re.S).group(1)))
            for col in ("open", "high", "low", "close", "volume"):
                full[col] = full[col].astype(float)
            _KLINE_CACHE[symbol] = full
            return full
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(0.8 + attempt)
    return None


def get_history_bars_qfq(symbol, need=35, retries=3):
    """复核路径：新浪前复权日线（经 akshare），返回 [(date, open, close, volume), ...]。"""
    global _AK
    for attempt in range(retries):
        try:
            if _AK is None:
                import akshare as _ak_mod
                _AK = _ak_mod
            df = _AK.stock_zh_a_daily(symbol=symbol, adjust="qfq")
            if df is None or df.empty:
                return []
            tail = df.tail(need)
            return [(str(d)[:10], float(o), float(c), float(v))
                    for d, o, c, v in zip(tail["date"], tail["open"], tail["close"], tail["volume"])]
        except Exception:
            if attempt == retries - 1:
                return []
            time.sleep(0.8 + attempt)
    return []


def get_snapshots(symbols):
    """批量实时快照：{symbol: {name, open, high, low, price, pre_close, volume, date, time}}"""
    out = {}
    for i in range(0, len(symbols), 60):
        r = _get(HQ_URL.format(symbol=",".join(symbols[i:i + 60])), timeout=10)
        for line in r.text.strip().split("\n"):
            m = re.match(r'var hq_str_(\w+)="([^"]*)"', line.strip())
            if not m or not m.group(2):
                continue
            p = m.group(2).split(",")
            try:
                out[m.group(1)] = {"name": p[0], "open": float(p[1]), "pre_close": float(p[2]),
                                   "price": float(p[3]), "high": float(p[4]), "low": float(p[5]),
                                   "volume": float(p[8]), "date": p[30], "time": p[31]}
            except (IndexError, ValueError):
                continue
        time.sleep(0.2)
    return out


# ---------------------------------------------------------------- 选股逻辑

def check_stock(stock, hist, min_pct, strict, scan_date, market_date,
                min_cross=2, min_vr=1.5, max_dev=12.0, ma30_up=True, fresh_days=5):
    """命中返回结果字典，否则返回 None。hist 为 [(date, open, close, volume), ...] 升序。

    max_dev: 现价偏离MA30上限%%（<=0 不限制）；ma30_up: MA30五日斜率须>=0；
    fresh_days: >0 时要求近 fresh_days 日首次收盘站上四线。"""
    if scan_date is None:                       # 实时/最近交易日模式
        if hist and market_date and hist[-1][0] >= market_date:
            hist = hist[:-1]                    # 收盘后历史K线已含今日，去掉避免重复
        today_open, price, pct = stock["open"], stock["price"], stock["pct"]
        today_vol = stock.get("volume", 0.0)
        date_str = market_date or "最近交易日"
        if today_open <= 0 or price <= 0:       # 停牌
            return None
        closes = [c for _, _, c, _ in hist] + [price]
        vols = [v for *_, v in hist]
    else:                                       # 历史回看模式
        bars = [b for b in hist if b[0] <= scan_date]
        if not bars or bars[-1][0] != scan_date:
            return None                         # 该日未交易或数据不足
        _, today_open, price, today_vol = bars[-1]
        if len(bars) < 2:
            return None
        prev_close = bars[-2][2]
        pct = (price / prev_close - 1) * 100 if prev_close else 0.0
        date_str = scan_date
        closes = [c for _, _, c, _ in bars]
        vols = [v for *_, v in bars[:-1]]

    if len(closes) < 40:                        # 次新股数据不足（MA30斜率/新鲜度需要）
        return None
    mas = {n: sum(closes[-n:]) / n for n in (5, 10, 20, 30)}
    ma_vals = list(mas.values())
    ma_max, ma_min = max(ma_vals), min(ma_vals)
    dev = (price / mas[30] - 1) * 100           # 偏离MA30 %

    if max_dev > 0 and dev > max_dev:           # 位置：高位长阳不买
        return None
    if ma30_up and mas[30] < sum(closes[-35:-5]) / 30:   # MA30 五日斜率向下
        return None
    if fresh_days > 0:                          # 新鲜度：近5日首次站上四线
        L = len(closes)
        for t in range(L - 1 - fresh_days, L - 1):       # 前 fresh_days 天（不含今日）
            ma_t = max(sum(closes[t - n + 1:t + 1]) / n for n in (5, 10, 20, 30))
            if closes[t] > ma_t:
                return None

    cond_yang = price > today_open                                # 阳线
    cond_price_above = price > ma_max                             # 现价站上全部四线
    if strict:
        cond_open_below = today_open < ma_min                     # 开盘在四线全部之下
        cross_n = 4 if cond_open_below else 0
    else:
        cross_n = sum(1 for v in ma_vals if today_open < v)       # 开盘压在下方的均线数
        cond_open_below = cross_n >= min_cross

    # 量比：今日成交量 / 前5个交易日均量
    prev5 = vols[-5:] if len(vols) >= 5 else []
    vr = today_vol / (sum(prev5) / len(prev5)) if prev5 and sum(prev5) > 0 and today_vol > 0 else 0.0
    cond_vr = vr >= min_vr if min_vr > 0 else True

    if cond_yang and cond_open_below and cond_price_above and pct >= min_pct and cond_vr:
        return {
            "日期": date_str,
            "代码": stock["symbol"],
            "名称": stock["name"],
            "今开": round(today_open, 2),
            "现价": round(price, 2),
            "涨幅%": round(pct, 2),
            "量比": round(vr, 2),
            "MA5": round(mas[5], 2),
            "MA10": round(mas[10], 2),
            "MA20": round(mas[20], 2),
            "MA30": round(mas[30], 2),
            "偏离MA30%": round(dev, 1),
            "穿越均线数": cross_n,
        }
    return None


# ---------------------------------------------------------------- 打分排名

def score_results(results, market_date, threads=24):
    """对命中股票计算 量比/MACD/KDJ 等指标并打分（复用初筛缓存的K线，需 pandas）。"""
    import numpy as np
    import pandas as pd
    symbols = [r["代码"] for r in results]
    snaps = get_snapshots(symbols)

    def feats(sym):
        s = snaps.get(sym)
        full = get_kline_lite(sym, need=80)     # 命中缓存，不再请求
        if not s or s["open"] <= 0 or full is None or len(full) < 35:
            return None
        today = {"day": s["date"], "open": s["open"], "high": s["high"], "low": s["low"],
                 "close": s["price"], "volume": s["volume"]}
        if len(full) and full.iloc[-1]["day"] >= s["date"]:
            full = full.iloc[:-1]
        full = pd.concat([full, pd.DataFrame([today])], ignore_index=True)
        c, hi, lo, vol = full["close"], full["high"], full["low"], full["volume"]

        mas = {n: c.tail(n).mean() for n in (5, 10, 20, 30)}
        ma_max = max(mas.values())
        o, p = s["open"], s["price"]
        if not ((p > o) and (o < ma_max) and (p > ma_max)):
            return None                                   # 最新价下信号已失效

        ema12 = c.ewm(span=12, adjust=False).mean()
        ema26 = c.ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        bar = 2 * (dif - dea)
        golden = any(dif.iloc[-i - 1] <= dea.iloc[-i - 1] and dif.iloc[-i] > dea.iloc[-i]
                     for i in range(1, 4))
        low9, high9 = lo.rolling(9).min(), hi.rolling(9).max()
        rsv = ((c - low9) / (high9 - low9).replace(0, np.nan) * 100).fillna(50)
        K = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        D = K.ewm(alpha=1 / 3, adjust=False).mean()
        J = 3 * K - 2 * D
        prev5 = vol.iloc[-6:-1]
        vr = s["volume"] / prev5.mean() if len(prev5) == 5 and prev5.mean() > 0 else np.nan
        rng = s["high"] - s["low"]
        body = (p - o) / rng if rng > 0 else 0.0
        close_pos = (p - s["low"]) / rng if rng > 0 else 0.0
        cross_n = sum(1 for v in mas.values() if o < v)
        pct = (p / s["pre_close"] - 1) * 100

        dev = (p / mas[30] - 1) * 100
        s1 = {0: 0, 1: 4, 2: 9, 3: 13, 4: 15}[cross_n]                    # 穿越强度 15
        s2 = body * 10 + close_pos * 5                                    # K线质量 15
        s3 = 5 if vr < 1 else 10 if vr < 1.5 else 16 if vr < 2.5 else 20 if vr < 4 \
            else 9 if vr < 6 else 3                                       # 量能 20（天量重罚）
        s4 = (10 if dif.iloc[-1] > dea.iloc[-1] else 0) + (5 if bar.iloc[-1] > 0 else 0) \
            + (5 if bar.iloc[-1] > bar.iloc[-2] else 0) + (5 if golden else 0)             # MACD 25
        j = J.iloc[-1]
        s5 = 10 if 50 <= j < 90 else 7 if 90 <= j < 100 else 3 if 100 <= j < 120 \
            else 0 if j >= 120 else 4                                     # KDJ 10（超买重罚）
        s6 = 15 if dev <= 5 else 12 if dev <= 8 else 8 if dev <= 12 else 3 if dev <= 15 else 0  # 位置 15
        score = round(s1 + s2 + s3 + s4 + s5 + s6, 1)

        return {"代码": sym, "名称": s["name"], "今开": o, "现价": round(p, 2), "涨幅%": round(pct, 2),
                "MA5": round(mas[5], 2), "MA10": round(mas[10], 2),
                "MA20": round(mas[20], 2), "MA30": round(mas[30], 2),
                "穿越均线数": cross_n, "实体率": round(body, 2), "量比": round(vr, 2),
                "DIF": round(dif.iloc[-1], 3), "DEA": round(dea.iloc[-1], 3),
                "MACD柱": round(bar.iloc[-1], 3), "柱放大": bool(bar.iloc[-1] > bar.iloc[-2]),
                "近3日金叉": bool(golden), "K": round(K.iloc[-1], 1), "D": round(D.iloc[-1], 1),
                "J": round(j, 1), "偏离MA30%": round((p / mas[30] - 1) * 100, 1), "综合评分": score}

    scored = []
    with ThreadPoolExecutor(max_workers=threads) as ex:
        for v in ex.map(feats, symbols):
            if v:
                scored.append(v)
    scored.sort(key=lambda x: (-x["综合评分"], -x["涨幅%"]))
    return scored


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="一阳穿四线选股（沪深主板，默认打分排名）")
    ap.add_argument("--min-pct", type=float, default=5.0, help="大阳线最小涨幅%%，默认 5")
    ap.add_argument("--min-cross", type=int, default=2, help="至少穿越几根均线，默认 2")
    ap.add_argument("--min-vr", type=float, default=1.5, help="最小量比，默认 1.5；0 为不限制")
    ap.add_argument("--max-dev", type=float, default=12.0,
                    help="现价偏离MA30上限%%，默认 12；0 为不限制")
    ap.add_argument("--no-ma30-up", action="store_true", help="不要求 MA30 走平/向上（默认要求）")
    ap.add_argument("--no-fresh", action="store_true",
                    help="不要求近5日首次站上四线（默认要求，排除老龙头）")
    ap.add_argument("--strict", action="store_true",
                    help="严格模式：开盘价须低于全部四根均线（经典一阳穿四线）")
    ap.add_argument("--date", default=None, help="回看历史日期 YYYY-MM-DD，默认最近交易日")
    ap.add_argument("--threads", type=int, default=24, help="并发线程数，默认 24")
    ap.add_argument("--include-st", action="store_true", help="保留 ST/*ST 股票（默认剔除）")
    ap.add_argument("--all-boards", action="store_true",
                    help="放开板块限制（默认仅沪深主板，剔除科创板/创业板/北交所）")
    ap.add_argument("--no-verify", action="store_true",
                    help="跳过前复权复核（更快，但除权除息日附近可能有误信号）")
    ap.add_argument("--no-score", action="store_true",
                    help="跳过打分排名（默认开启，需 pandas）")
    ap.add_argument("--min-score", type=float, default=80.0,
                    help="只保留综合评分≥此值的股票，默认 80；0 为不限制（需开启打分）")
    args = ap.parse_args()

    # 输出目录：脚本同级 output/YYYYMMDD_HHMM/
    run_dir = Path(__file__).resolve().parent / "output" / datetime.now().strftime("%Y%m%d_%H%M")
    run_dir.mkdir(parents=True, exist_ok=True)
    setup_logger(run_dir / "run.log")
    log.info("输出目录：%s", run_dir)
    log.info("参数：min_pct=%s min_cross=%s min_vr=%s strict=%s date=%s "
             "include_st=%s all_boards=%s no_verify=%s no_score=%s",
             args.min_pct, args.min_cross, args.min_vr, args.strict, args.date,
             args.include_st, args.all_boards, args.no_verify, args.no_score)

    t_start = time.time()
    market_date = get_market_date()
    log.info("行情日期：%s", market_date or "未知")

    log.info("正在获取 A 股全市场列表与快照 ...")
    stocks = get_stock_list()
    log.info("全市场共 %d 只", len(stocks))
    if not args.all_boards:
        stocks = [s for s in stocks if is_main_board(s["symbol"])]
        log.info("仅保留沪深主板后：%d 只", len(stocks))
    if not args.include_st:
        stocks = [s for s in stocks if "ST" not in s["name"].upper()]
        log.info("剔除 ST 后：%d 只", len(stocks))
    if args.date is None:
        stocks = [s for s in stocks if s["price"] > 0 and s["open"] > 0]
        log.info("剔除停牌后：%d 只", len(stocks))

    log.info("开始初筛（涨幅≥%.1f%%、穿越≥%d线、量比≥%.1f、偏离MA30≤%.1f%%%s%s%s，线程 %d）...",
             args.min_pct, args.min_cross, args.min_vr, args.max_dev,
             "、严格模式" if args.strict else "",
             "" if not args.no_ma30_up else "、不限MA30方向",
             "" if not args.no_fresh else "、不限新鲜度", args.threads)

    results, done, t0 = [], 0, time.time()

    def worker(s):
        full = get_kline_lite(s["symbol"], need=80)
        if full is None or len(full) < 40:
            return None
        hist = [(d, o, c, v) for d, o, c, v in
                zip(full["day"], full["open"], full["close"], full["volume"])]
        return check_stock(s, hist, args.min_pct, args.strict, args.date, market_date,
                           args.min_cross, args.min_vr, args.max_dev,
                           not args.no_ma30_up, 0 if args.no_fresh else 5)

    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = [ex.submit(worker, s) for s in stocks]
        for fut in as_completed(futs):
            done += 1
            if done % 500 == 0:
                log.info("初筛进度 %d/%d，已命中 %d 只，耗时 %.0fs",
                         done, len(stocks), len(results), time.time() - t0)
            r = fut.result()
            if r:
                results.append(r)
    log.info("初筛完成：命中 %d 只，耗时 %.0fs", len(results), time.time() - t0)

    # 第二段：候选股用前复权数据复核，剔除除权除息造成的误信号
    if results and not args.no_verify:
        log.info("正在用前复权数据复核 %d 只候选 ...", len(results))
        by_symbol = {s["symbol"]: s for s in stocks}
        verified = []
        for i, r in enumerate(results, 1):
            s = by_symbol[r["代码"]]
            hist = get_history_bars_qfq(s["symbol"], need=45)
            if not hist:
                log.warning("复核取数失败，剔除：%s %s", r["代码"], r["名称"])
                continue
            v = check_stock(s, hist, args.min_pct, args.strict, args.date, market_date,
                            args.min_cross, args.min_vr, args.max_dev,
                            not args.no_ma30_up, 0 if args.no_fresh else 5)
            if v:
                verified.append(v)
            if i % 20 == 0:
                log.info("复核进度 %d/%d", i, len(results))
        dropped = len(results) - len(verified)
        if dropped:
            log.info("复核剔除 %d 只（除权除息误信号或取数失败）", dropped)
        results = verified

    results.sort(key=lambda x: -x["涨幅%"])

    if results and not args.no_score:
        log.info("正在计算 量比/MACD/KDJ 指标并打分 ...")
        try:
            results = score_results(results, market_date, args.threads)
            log.info("打分完成，%d 只股票信号在最新价下仍然成立", len(results))
            if args.min_score > 0:
                before = len(results)
                results = [r for r in results if r["综合评分"] >= args.min_score]
                log.info("评分≥%.0f 过滤：%d → %d 只", args.min_score, before, len(results))
        except ImportError:
            log.warning("缺少 pandas/numpy，跳过打分（pip install pandas 后可开启）")

    log.info("扫描完成，命中 %d 只，总耗时 %.0fs", len(results), time.time() - t_start)

    csv_path = run_dir / "results.csv"
    if results:
        try:
            import pandas as pd
            df = pd.DataFrame(results)
            for line in df.to_string(index=False).split("\n"):
                log.info(line)
            df.to_csv(csv_path, index=True, index_label="排名", encoding="utf-8-sig")
        except ImportError:
            import csv
            for r in results:
                log.info(str(r))
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.DictWriter(f, fieldnames=results[0].keys())
                w.writeheader()
                w.writerows(results)
        log.info("结果已保存：%s", csv_path)
    else:
        log.info("无符合条件的股票，可放宽 --min-pct / --min-cross / --min-vr")
    log.info("日志文件：%s", run_dir / "run.log")


if __name__ == "__main__":
    main()