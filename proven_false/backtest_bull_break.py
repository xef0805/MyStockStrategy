# -*- coding: utf-8 -*-
"""
backtest_bull_break.py —— 一阳穿四线策略历史回测（支持多种入场/出场结构对比）
==============================================================================
回测规则：
  过往 --days 个交易日（默认 120，约半年），每个交易日 T 收盘后对
  沪深主板（非 ST）全市场执行"一阳穿四线"选股 + 打分（与 bull_break_screener.py
  完全相同的条件和评分公式），取综合评分 ≥ --min-score（默认 80）的前 --top 名。

  入场（--entry）：
    close     信号日 T 收盘价买入（T 收盘涨停买不进则剔除）
    pullback  信号日只记录；之后 --pb-days 天内（默认 3）等回踩：
              盘中最低价触及 MA10 且收盘收回 MA10 上方 → 当日收盘买入；
              期间收盘跌破 MA10 则放弃该信号（破位）
  出场（--exit）：
    next      入场次日收盘卖出
    hold      持有 --hold-days 个交易日（默认 5）收盘卖出
    ma10      收盘跌破 MA10 卖出，最长持有 --max-hold 天（默认 10）

  费用：默认每边固定 --fee-yuan 5 元（设为 0 改用比例费率 --cost 万三）。
  除权日过滤：买入日/卖出日及持有期间有除权的一律不买（前复权模式下）。
  评分为王：只买综合评分 ≥ --min-score 的股票（默认 80，0 为不限制）。

  资金核算：
    exit=next —— 每日满仓复利、余额重新均分（与实盘脚本口径一致）
    exit=hold/ma10 —— 每笔固定仓位（capital/top），按卖出日确认盈亏累计；
                     持仓可能重叠（最大重叠数日志会提示），不等同于满仓复利

  --compare：忽略单一入场/出场设置，一次跑完 5 种结构组合并输出对比表：
    ① 追涨+隔夜  ② 追涨+持有5天  ③ 追涨+MA10止损
    ④ 回踩+持有5天  ⑤ 回踩+MA10止损

数据口径：信号与收益均使用前复权日线（akshare 新浪源）。
已知简化（解读结果时请注意）：
  · 卖出端未处理跌停卖不出；收盘买入假设尾盘竞价能成交
  · 停牌日无法买/卖的交易直接剔除
  · 股票池为"当前"主板名单，已退市股不在内，半年窗口影响很小
  · 评分是经验权重，胜率结果不构成任何投资建议

输出：脚本同级 output/backtest_YYYYMMDD_HHMM/ 文件夹，内含
  trades*.csv  逐笔交易明细    daily*.csv  每日组合收益与账户余额
  equity*.png  资金曲线折线图  run.log     运行日志与汇总/对比表
  data.pkl     --cache 时的K线缓存（复跑秒开）

依赖：requests、akshare、pandas、numpy、matplotlib（画图用）；
与 bull_break_screener.py 放在同一目录。

用法：
  python backtest_bull_break.py --load-cache output/backtest_xxx/data.pkl --compare
  python backtest_bull_break.py --load-cache output/backtest_xxx/data.pkl --entry pullback --exit ma10
  python backtest_bull_break.py --cache        # 无缓存时先跑这个（取数约4分钟）
"""

import argparse
import logging
import pickle
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from proven_false.bull_break_screener import get_stock_list, is_main_board, get_kline_lite

log = logging.getLogger("backtest")
_AK = None

COMPARE_CONFIGS = [
    ("追涨+隔夜",      {"entry": "close",    "exit": "next"}),
    ("追涨+持有5天",   {"entry": "close",    "exit": "hold", "hold_days": 5}),
    ("追涨+MA10止损",  {"entry": "close",    "exit": "ma10", "max_hold": 10}),
    ("回踩+持有5天",   {"entry": "pullback", "exit": "hold", "hold_days": 5}),
    ("回踩+MA10止损",  {"entry": "pullback", "exit": "ma10", "max_hold": 10}),
]


def setup_logger(log_path):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(); ch.setFormatter(fmt)
    fh = logging.FileHandler(log_path, encoding="utf-8"); fh.setFormatter(fmt)
    log.addHandler(ch); log.addHandler(fh)


def get_kline_qfq_full(symbol, need, retries=2):
    """前复权日线完整字段（akshare 新浪源）：date/open/high/low/close/volume 升序。"""
    global _AK
    for attempt in range(retries):
        try:
            if _AK is None:
                import akshare as _ak_mod
                _AK = _ak_mod
            df = _AK.stock_zh_a_daily(symbol=symbol, adjust="qfq")
            if df is None or len(df) < 45:
                return None
            df = df.tail(need)
            out = pd.DataFrame({
                "date": df["date"].astype(str).str[:10],
                "open": df["open"].astype(float), "high": df["high"].astype(float),
                "low": df["low"].astype(float), "close": df["close"].astype(float),
                "volume": df["volume"].astype(float),
            })
            return out.reset_index(drop=True)
        except Exception:
            if attempt == retries - 1:
                return None
            time.sleep(0.8 + attempt)
    return None


# ---------------------------------------------------------------- 数据

def fetch_universe(args):
    """返回 {symbol: DataFrame(date/open/high/low/close/volume/ex)} 与名称表。"""
    stocks = get_stock_list()
    log.info("全市场 %d 只", len(stocks))
    if not args.all_boards:
        stocks = [s for s in stocks if is_main_board(s["symbol"])]
    if not args.include_st:
        stocks = [s for s in stocks if "ST" not in s["name"].upper()]
    names = {s["symbol"]: s["name"] for s in stocks}
    log.info("股票池 %d 只，开始拉取 %s 日线（线程 %d）...",
             len(stocks), "前复权" if args.adjust == "qfq" else "不复权", args.threads)

    data, done, t0 = {}, 0, time.time()
    need = args.days + 100          # 指标预热 + 缓冲

    def one(s):
        if args.adjust == "qfq":
            df = get_kline_qfq_full(s["symbol"], need)
            if df is None:
                return s["symbol"], None
            # 除权日检测：复权因子 qfq收盘/不复权收盘 在除权日跳变
            raw = get_kline_lite(s["symbol"], need=need, retries=2)
            df["ex"] = False
            if raw is not None and len(raw):
                raw_df = raw.rename(columns={"day": "date"})[["date", "close"]]
                raw_df.columns = ["date", "raw_close"]
                m = df.merge(raw_df, on="date", how="left")
                factor = m["close"] / m["raw_close"]
                df["ex"] = (factor.pct_change().abs() > 0.001).fillna(False).values
            return s["symbol"], df
        full = get_kline_lite(s["symbol"], need=need, retries=2)
        if full is None:
            return s["symbol"], None
        out = full.rename(columns={"day": "date"})[
            ["date", "open", "high", "low", "close", "volume"]].copy()
        out["ex"] = False                            # 不复权模式无法检测，过滤关闭
        return s["symbol"], out

    with ThreadPoolExecutor(max_workers=args.threads) as ex:
        futs = [ex.submit(one, s) for s in stocks]
        for fut in as_completed(futs):
            done += 1
            if done % 200 == 0:
                log.info("取数进度 %d/%d，成功 %d，耗时 %.0fs",
                         done, len(stocks), len(data), time.time() - t0)
            sym, df = fut.result()
            if df is not None and len(df) >= 45:
                data[sym] = df[["date", "open", "high", "low", "close", "volume", "ex"]
                               ].reset_index(drop=True)
    log.info("取数完成：%d 只有效，耗时 %.0fs", len(data), time.time() - t0)
    return data, names


# ---------------------------------------------------------------- 指标与信号

def build_features(df, fresh_days=5):
    """向量化预计算每只股票的均线/MACD/KDJ/量比/涨幅/位置/新鲜度。"""
    c, hi, lo, vol = df["close"], df["high"], df["low"], df["volume"]
    f = df.copy()
    for n in (5, 10, 20, 30):
        f[f"ma{n}"] = c.rolling(n).mean()
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    f["dif"] = ema12 - ema26
    f["dea"] = f["dif"].ewm(span=9, adjust=False).mean()
    f["bar"] = 2 * (f["dif"] - f["dea"])
    low9, high9 = lo.rolling(9).min(), hi.rolling(9).max()
    rsv = ((c - low9) / (high9 - low9).replace(0, np.nan) * 100).fillna(50)
    f["K"] = rsv.ewm(alpha=1 / 3, adjust=False).mean()
    f["D"] = f["K"].ewm(alpha=1 / 3, adjust=False).mean()
    f["J"] = 3 * f["K"] - 2 * f["D"]
    f["vr"] = vol / vol.rolling(5).mean().shift(1)
    f["pct"] = c.pct_change() * 100
    f["ma_max"] = f[["ma5", "ma10", "ma20", "ma30"]].max(axis=1)
    f["dev"] = (c / f["ma30"] - 1) * 100                     # 偏离MA30 %
    f["ma30_up"] = f["ma30"] >= f["ma30"].shift(5)           # MA30 五日斜率走平/向上
    above = c > f["ma_max"]
    recent_above = above.shift(1).rolling(fresh_days).max()  # 前fresh_days天是否站上过
    f["fresh"] = above & recent_above.fillna(0).eq(0)        # 近5日首次站上四线
    return f


def signal_and_score(f, i, args):
    """第 i 行（信号日 T）是否命中策略；命中返回 (score, 明细dict)，否则 None。"""
    r = f.iloc[i]
    o, p = r["open"], r["close"]
    mas = [r["ma5"], r["ma10"], r["ma20"], r["ma30"]]
    if any(pd.isna(m) for m in mas) or o <= 0 or p <= 0:
        return None
    ma_max, ma_min = max(mas), min(mas)
    if not (p > o and p > ma_max):                     # 阳线且收盘站上全部四线
        return None
    if args.strict:
        if not o < ma_min:
            return None
        cross_n = 4
    else:
        cross_n = sum(1 for m in mas if o < m)
        if cross_n < args.min_cross:
            return None
    if pd.isna(r["pct"]) or r["pct"] < args.min_pct:   # 大阳线
        return None
    if pd.isna(r["vr"]) or r["vr"] < args.min_vr:      # 放量
        return None
    if args.max_dev > 0 and r["dev"] > args.max_dev:   # 位置：高位长阳不买
        return None
    if not args.no_ma30_up and not r["ma30_up"]:       # MA30 须走平/向上
        return None
    if not args.no_fresh and not r["fresh"]:           # 近5日首次站上四线
        return None

    rng = r["high"] - r["low"]
    body = (p - o) / rng if rng > 0 else 0.0
    close_pos = (p - r["low"]) / rng if rng > 0 else 0.0
    golden = any(f["dif"].iloc[i - k - 1] <= f["dea"].iloc[i - k - 1]
                 and f["dif"].iloc[i - k] > f["dea"].iloc[i - k] for k in range(1, 4))
    bar_up = f["bar"].iloc[i] > f["bar"].iloc[i - 1]
    j = r["J"]

    s1 = {0: 0, 1: 4, 2: 9, 3: 13, 4: 15}[cross_n]                    # 穿越强度 15
    s2 = body * 10 + close_pos * 5                                    # K线质量 15
    vr = r["vr"]
    s3 = 5 if vr < 1 else 10 if vr < 1.5 else 16 if vr < 2.5 else 20 if vr < 4 \
        else 9 if vr < 6 else 3                                       # 量能 20（天量重罚）
    s4 = (10 if r["dif"] > r["dea"] else 0) + (5 if r["bar"] > 0 else 0) \
        + (5 if bar_up else 0) + (5 if golden else 0)                 # MACD 25
    s5 = 10 if 50 <= j < 90 else 7 if 90 <= j < 100 else 3 if 100 <= j < 120 \
        else 0 if j >= 120 else 4                                     # KDJ 10（超买重罚）
    s6 = 15 if r["dev"] <= 5 else 12 if r["dev"] <= 8 else 8 if r["dev"] <= 12 \
        else 3 if r["dev"] <= 15 else 0                               # 位置 15
    score = round(s1 + s2 + s3 + s4 + s5 + s6, 1)

    return score, {"穿越均线数": cross_n, "实体率": round(body, 2), "量比": round(vr, 2),
                   "MACD柱": round(r["bar"], 3), "柱放大": bool(bar_up), "近3日金叉": bool(golden),
                   "J": round(j, 1), "偏离MA30%": round(r["dev"], 1), "综合评分": score}


# ---------------------------------------------------------------- 交易模拟

def _is_limit_up(row):
    """主板收盘涨停近似判定：涨幅≥9.8% 且收盘价=最高价。"""
    return row["pct"] >= 9.8 and row["close"] >= row["high"] - 1e-9


def simulate_trade(f, i_T, args):
    """给定信号日位置 i_T，按入场/出场规则模拟一笔交易。

    返回 (trade_dict, None) 或 (None, 跳过原因)。"""
    dates = f["date"].values
    n_bars = len(f)

    # ---- 入场 ----
    if args.entry == "close":
        ei = i_T
        row = f.iloc[ei]
        if not args.allow_limit_up and _is_limit_up(row):
            return None, "limit"
        entry_price = row["close"]
    else:                                            # pullback 回踩 MA10
        ei = None
        for j in range(i_T + 1, min(i_T + 1 + args.pb_days, n_bars)):
            row = f.iloc[j]
            ma10 = row["ma10"]
            if pd.isna(ma10):
                continue
            if row["close"] < ma10:                  # 破位，信号作废
                break
            if row["low"] <= ma10:                   # 盘中触线且收回（未破位已保证 close>=ma10）
                if not args.allow_limit_up and _is_limit_up(row):
                    return None, "limit"
                ei, entry_price = j, row["close"]
                break
        if ei is None:
            return None, "nopull"

    # ---- 出场 ----
    if args.exit == "next":
        xi = ei + 1
    elif args.exit == "hold":
        xi = ei + args.hold_days
    else:                                            # ma10 止损
        xi = None
        for j in range(ei + 1, min(ei + 1 + args.max_hold, n_bars)):
            if f["close"].iloc[j] < f["ma10"].iloc[j]:
                xi = j
                break
        if xi is None:
            xi = min(ei + args.max_hold, n_bars - 1)
    if xi >= n_bars or xi <= ei:
        return None, "nosell"

    # ---- 除权过滤：买入日、卖出日及持有期间 ----
    if bool(f["ex"].iloc[ei:xi + 1].any()):
        return None, "ex"

    exit_price = float(f["close"].iloc[xi])
    if entry_price <= 0 or exit_price <= 0:
        return None, "bad_px"
    return {"信号日": dates[i_T], "买入日": dates[ei], "卖出日": dates[xi],
            "持有天数": int(xi - ei), "买入价": round(entry_price, 2),
            "卖出价": round(exit_price, 2),
            "毛收益%": round((exit_price / entry_price - 1) * 100, 2)}, None


# ---------------------------------------------------------------- 单配置回测

def run_one(args, feats, names, signal_days, tag, run_dir):
    """跑一种入场/出场组合，输出明细并返回汇总 dict。"""
    position = args.capital / args.top
    fee_pct = (args.fee_yuan * 2 / position * 100) if args.fee_yuan > 0 else args.cost * 100
    skip = {"limit": 0, "ex": 0, "nopull": 0, "nosell": 0, "bad_px": 0}
    trades = []

    for T in signal_days:
        cands = []
        for sym, f in feats.items():
            if T not in f.index:
                continue
            i = f.index.get_loc(T)
            if isinstance(i, slice) or i < 35:
                continue
            r = signal_and_score(f, i, args)
            if not r:
                continue
            score, detail = r
            cands.append({"代码": sym, "名称": names.get(sym, ""), "_f": f, "_i": i, **detail})
        if args.min_score > 0:
            cands = [c for c in cands if c["综合评分"] >= args.min_score]
        cands.sort(key=lambda x: -x["综合评分"])
        for rank, c in enumerate(cands[:args.top], 1):
            trade, why = simulate_trade(c["_f"], c["_i"], args)
            if why:
                skip[why] += 1
                continue
            trade.update({"代码": c["代码"], "名称": c["名称"],
                          "穿越均线数": c["穿越均线数"], "实体率": c["实体率"], "量比": c["量比"],
                          "MACD柱": c["MACD柱"], "J": c["J"], "偏离MA30%": c["偏离MA30%"],
                          "综合评分": c["综合评分"], "当日排名": rank, "当日候选数": len(cands)})
            trade["净收益%"] = round(trade["毛收益%"] - fee_pct, 2)
            trades.append(trade)

    label = f"{args.entry}+{args.exit}" + (f"{args.hold_days}天" if args.exit == "hold"
                                           else "止损" if args.exit == "ma10" else "")
    log.info("=" * 60)
    log.info("【%s】交易 %d 笔 ｜ 剔除：涨停 %d、除权 %d、未回踩 %d、无法卖出 %d",
             label, len(trades), skip["limit"], skip["ex"], skip["nopull"], skip["nosell"])
    if not trades:
        log.info("【%s】无交易", label)
        return {"配置": label, "笔数": 0}

    tdf = pd.DataFrame(trades)
    tdf.to_csv(run_dir / f"trades{tag}.csv", index=False, encoding="utf-8-sig")
    n = len(tdf)
    win_g = (tdf["毛收益%"] > 0).mean() * 100
    win_n = (tdf["净收益%"] > 0).mean() * 100
    avg_g, avg_n = tdf["毛收益%"].mean(), tdf["净收益%"].mean()
    wins = tdf.loc[tdf["净收益%"] > 0, "净收益%"]
    losses = tdf.loc[tdf["净收益%"] <= 0, "净收益%"]
    pl_ratio = (wins.mean() / abs(losses.mean())) if len(wins) and len(losses) else float("nan")

    if args.exit == "next":
        # 每日满仓复利、余额重新均分（按买入日聚合）
        daily = tdf.groupby("买入日").agg(持仓数=("代码", "count"),
                                        日均毛收益=("毛收益%", "mean"),
                                        日均净收益=("净收益%", "mean")).reset_index()
        balance, balances, fees = args.capital, [], []
        for _, d in daily.iterrows():
            balance *= (1 + d["日均毛收益"] / 100)
            fee = args.fee_yuan * 2 * d["持仓数"] if args.fee_yuan > 0 else balance * args.cost
            balance -= fee
            fees.append(fee)
            balances.append(round(balance, 2))
        daily["账户余额"] = balances
        daily["当日盈亏"] = daily["账户余额"].diff().fillna(
            daily["账户余额"] - args.capital).round(2)
        overlap = args.top
    else:
        # 每笔固定仓位，按卖出日确认盈亏累计（持仓可重叠）
        tdf["盈亏元"] = (tdf["毛收益%"] / 100 * position
                       - (args.fee_yuan * 2 if args.fee_yuan > 0 else position * args.cost)).round(2)
        daily = tdf.groupby("卖出日").agg(持仓数=("代码", "count"),
                                        日均毛收益=("毛收益%", "mean"),
                                        日均净收益=("净收益%", "mean"),
                                        当日盈亏=("盈亏元", "sum")).reset_index().sort_values("卖出日")
        bal, balances = args.capital, []
        for pnl in daily["当日盈亏"]:
            bal += pnl
            balances.append(round(bal, 2))
        daily["账户余额"] = balances
        fees = [args.fee_yuan * 2 * n] if args.fee_yuan > 0 else [position * args.cost * n]
        # 最大同时持仓数
        iv = sorted([(t["买入日"], 1) for t in trades] + [(t["卖出日"], -1) for t in trades])
        cur = overlap = 0
        for _, d in iv:
            cur += d
            overlap = max(overlap, cur)
        if overlap * position > args.capital:
            log.warning("【%s】最大同时持仓 %d 只（%.0f 元），超出本金，"
                        "余额曲线按卖出日确认盈亏近似", label, overlap, overlap * position)
    daily.to_csv(run_dir / f"daily{tag}.csv", index=False, encoding="utf-8-sig")

    bal_s = daily["账户余额"]
    dd = (bal_s / bal_s.cummax() - 1).min() * 100
    final_balance = bal_s.iloc[-1]
    day_win = (daily["当日盈亏"] > 0).mean() * 100
    total_fee = sum(fees)

    log.info("【%s】净胜率 %.1f%% ｜ 单笔净均值 %.2f%% ｜ 盈亏比 %.2f ｜ "
             "费用 %.0f 元 ｜ 期末 %.0f 元（%+.2f%%）｜ 最大回撤 %.2f%%",
             label, win_n, avg_n, pl_ratio, total_fee, final_balance,
             (final_balance / args.capital - 1) * 100, dd)

    # 资金曲线
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
        plt.rcParams["axes.unicode_minus"] = False
        fig, ax = plt.subplots(figsize=(11, 5.5))
        x = pd.to_datetime(daily["买入日"] if args.exit == "next" else daily["卖出日"])
        ax.plot(x, daily["账户余额"], lw=1.8, color="#c0392b", label="账户余额")
        ax.axhline(args.capital, ls="--", lw=1, color="gray", label=f"本金 {args.capital:.0f} 元")
        ax.set_title(f"一阳穿四线 · {label}（{signal_days[0]} ~ {signal_days[-1]}）")
        ax.set_xlabel("日期"); ax.set_ylabel("账户余额（元）")
        ax.grid(alpha=0.3); ax.legend()
        fig.tight_layout()
        fig.savefig(run_dir / f"equity{tag}.png", dpi=150)
        plt.close(fig)
    except ImportError:
        if tag == "":
            log.warning("缺少 matplotlib，未生成资金曲线图")

    return {"配置": label, "笔数": n, "净胜率%": round(win_n, 1),
            "单笔净均值%": round(avg_n, 2), "盈亏比": round(pl_ratio, 2),
            "期末余额": round(final_balance, 0), "总收益%": round((final_balance / args.capital - 1) * 100, 2),
            "最大回撤%": round(dd, 2)}


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="一阳穿四线策略历史回测（多结构对比）")
    ap.add_argument("--days", type=int, default=120, help="回测交易日数，默认 120（约半年）")
    ap.add_argument("--top", type=int, default=2, help="每日买入前N名，默认 2")
    ap.add_argument("--capital", type=float, default=10000, help="本金，默认 1 万")
    ap.add_argument("--fee-yuan", type=float, default=5.0,
                    help="每边固定手续费（元），默认 5；设为 0 则改用 --cost 比例费率")
    ap.add_argument("--cost", type=float, default=0.0003,
                    help="比例费率（双边合计），仅 --fee-yuan 0 时生效，默认 0.0003（万三）")
    ap.add_argument("--entry", choices=["close", "pullback"], default="close",
                    help="入场方式：close 信号日收盘（默认）/ pullback 回踩MA10")
    ap.add_argument("--pb-days", type=int, default=3, help="回踩入场等待天数，默认 3")
    ap.add_argument("--exit", dest="exit_", choices=["next", "hold", "ma10"], default="next",
                    help="出场方式：next 次日（默认）/ hold 持有N天 / ma10 跌破MA10止损")
    ap.add_argument("--hold-days", type=int, default=5, help="hold 模式持有天数，默认 5")
    ap.add_argument("--max-hold", type=int, default=10, help="ma10 模式最长持有天数，默认 10")
    ap.add_argument("--compare", action="store_true",
                    help="对比模式：一次跑完 5 种入场×出场组合并输出对比表")
    ap.add_argument("--adjust", choices=["qfq", "none"], default="qfq",
                    help="价格口径：qfq 前复权（默认，准确）/ none 不复权（快，除权日失真）")
    ap.add_argument("--min-pct", type=float, default=5.0)
    ap.add_argument("--min-cross", type=int, default=2)
    ap.add_argument("--min-vr", type=float, default=1.5)
    ap.add_argument("--max-dev", type=float, default=12.0,
                    help="现价偏离MA30上限%%，默认 12；0 为不限制")
    ap.add_argument("--no-ma30-up", action="store_true", help="不要求 MA30 走平/向上（默认要求）")
    ap.add_argument("--no-fresh", action="store_true",
                    help="不要求近5日首次站上四线（默认要求，排除老龙头）")
    ap.add_argument("--min-score", type=float, default=80.0,
                    help="只买综合评分≥此值的股票，默认 80；0 为不限制")
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--allow-limit-up", action="store_true",
                    help="允许买入收盘涨停的股票（默认剔除，模拟真实能买进去）")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--include-st", action="store_true")
    ap.add_argument("--all-boards", action="store_true")
    ap.add_argument("--cache", action="store_true", help="把K线数据缓存到 data.pkl")
    ap.add_argument("--load-cache", default=None, help="直接加载 data.pkl 跳过取数")
    args = ap.parse_args()
    # argparse 不允许 --exit 作为属性名冲突，统一改回 args.exit
    args.exit = args.exit_

    run_dir = Path(__file__).resolve().parent / "output" / f"backtest_{datetime.now():%Y%m%d_%H%M}"
    run_dir.mkdir(parents=True, exist_ok=True)
    setup_logger(run_dir / "run.log")
    log.info("输出目录：%s", run_dir)
    log.info("参数：%s", {k: v for k, v in vars(args).items() if k != "exit_"})

    if args.load_cache:
        with open(args.load_cache, "rb") as f:
            data, names = pickle.load(f)
        log.info("已加载缓存：%d 只股票", len(data))
    else:
        data, names = fetch_universe(args)
        if args.cache:
            with open(run_dir / "data.pkl", "wb") as f:
                pickle.dump((data, names), f)
            log.info("K线缓存已保存：%s", run_dir / "data.pkl")

    log.info("预计算指标 ...")
    feats = {}
    for sym, df in data.items():
        f = build_features(df)
        f.index = f["date"]
        feats[sym] = f

    # 交易日历：全部股票日期的并集；剔除最后一天（可能盘中未收完）
    all_dates = sorted({d for f in feats.values() for d in f["date"]})
    usable = all_dates[:-1]
    # 波段模式需在数据末端留出买入等待+卖出空间，信号日窗口整体前移
    tail = 1 if (not args.compare and args.exit == "next") else args.pb_days + args.max_hold + 2
    if len(usable) < args.days + tail:
        log.error("可用交易日不足（%d 天，需要 %d），请检查数据", len(usable), args.days + tail)
        return
    signal_days = usable[-(args.days + tail):-tail]
    log.info("回测区间：%s ~ %s（%d 个信号日，末端预留 %d 天用于卖出）",
             signal_days[0], signal_days[-1], len(signal_days), tail)

    t0 = time.time()
    if args.compare:
        summaries = []
        for name, cfg in COMPARE_CONFIGS:
            for k_, v_ in cfg.items():
                setattr(args, k_, v_)
            summaries.append(run_one(args, feats, names, signal_days,
                                     f"_{name}", run_dir))
        log.info("=" * 60)
        log.info("结构对比汇总（%s ~ %s，本金 %.0f 元，每日前 %d 名，评分≥%.0f）",
                 signal_days[0], signal_days[-1], args.capital, args.top, args.min_score)
        sdf = pd.DataFrame(summaries)
        for line in sdf.to_string(index=False).split("\n"):
            log.info(line)
        log.info("=" * 60)
    else:
        run_one(args, feats, names, signal_days, "", run_dir)
    log.info("总耗时 %.0fs，输出目录：%s", time.time() - t0, run_dir)


if __name__ == "__main__":
    main()