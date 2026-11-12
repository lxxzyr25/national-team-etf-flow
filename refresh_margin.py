# -*- coding: utf-8 -*-
"""Build a standalone mainland-market financing balance dashboard."""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import akshare as ak
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "_margin_cache.json")
BACKFILL_CACHE = os.path.join(ROOT, "_margin_official_backfill.json")
CAP_CACHE = os.path.join(ROOT, "_float_mcap_cache.json")
PAGE = os.path.join(ROOT, "financing.html")
TZ_CN = timezone(timedelta(hours=8))
BACKFILL_DATES_PER_RUN = 250


def _column(frame, *names):
    for name in names:
        if name in frame.columns:
            return name
    raise ValueError("Missing expected data column: " + "/".join(names))


def _series(frame, date_names, balance_names, buy_names):
    date_col = next((name for name in date_names if name in frame.columns), None)
    bal_col = _column(frame, *balance_names)
    buy_col = _column(frame, *buy_names)
    rows = {}
    for idx, row in frame.iterrows():
        raw_day = row[date_col] if date_col else idx
        day = pd.to_datetime(raw_day, errors="coerce")
        balance = pd.to_numeric(row[bal_col], errors="coerce")
        buy = pd.to_numeric(row[buy_col], errors="coerce")
        if pd.isna(day) or pd.isna(balance) or pd.isna(buy):
            continue
        rows[day.strftime("%Y-%m-%d")] = {"balance": float(balance), "buy": float(buy)}
    return rows


def fetch_sse_margin(start_date, end_date):
    """Fetch the full paginated interval from the SSE's public summary API."""
    result = {}
    headers = {
        "Referer": "https://www.sse.com.cn/market/othersdata/margin/sum/",
        "User-Agent": "Mozilla/5.0",
    }
    url = "https://query.sse.com.cn/commonSoaQuery.do"
    params = {
            "isPagination": "true",
            "pageHelp.pageSize": "2000",
            "pageHelp.pageNo": "1",
            "pageHelp.beginPage": "1",
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": "1",
            "stockCode": "",
            "beginDate": start_date,
            "endDate": end_date,
            "sqlId": "RZRQ_HZ_INFO",
    }
    response = requests.get(url, params=params, headers=headers, timeout=30)
    response.raise_for_status()
    payload = response.json()
    page_help = payload.get("pageHelp") or {}
    page_count = int(page_help.get("pageCount") or 1)
    for page in range(1, page_count + 1):
        if page > 1:
            params["pageHelp.pageNo"] = str(page)
            response = requests.get(url, params=params, headers=headers, timeout=30)
            response.raise_for_status()
            payload = response.json()
            page_help = payload.get("pageHelp") or {}
        for item in page_help.get("data") or []:
            raw_day = str(item.get("opDate", ""))
            day = pd.to_datetime(raw_day, format="%Y%m%d", errors="coerce")
            balance = pd.to_numeric(item.get("rzye"), errors="coerce")
            buy = pd.to_numeric(item.get("rzmre"), errors="coerce")
            if pd.notna(day) and pd.notna(balance) and pd.notna(buy):
                result[day.strftime("%Y-%m-%d")] = {
                    "balance": float(balance), "buy": float(buy)
                }
        time.sleep(0.1)
    return result


def fetch_market_data(rebuild=False):
    """Refresh official data or resume the clean all-history backfill."""
    today = datetime.now(TZ_CN).date()
    end = today.strftime("%Y%m%d")
    previous = load_cache()
    old_rows = previous.get("rows", [])
    backfill_pending = os.path.exists(BACKFILL_CACHE)
    if backfill_pending:
        with open(BACKFILL_CACHE, encoding="utf-8") as f:
            progress = json.load(f)
    elif rebuild:
        sh = fetch_sse_margin("20100331", end)
        if len(sh) < 1000:
            raise RuntimeError(f"SSE returned only {len(sh)} history rows; refusing to start a short rebuild")
        progress = {"source": "SSE/SZSE official", "sse": sh, "szse": {}}
        backfill_pending = True
        with open(BACKFILL_CACHE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, separators=(",", ":"))
    else:
        start = (today - timedelta(days=30)).strftime("%Y%m%d")
        sh = fetch_sse_margin(start, end)
        if not sh:
            raise RuntimeError("SSE official margin endpoint returned no recent observations")
        progress = {"source": "SSE/SZSE official", "sse": sh, "szse": {}}

    sh, sz = progress.get("sse", {}), progress.setdefault("szse", {})
    missing = [day for day in sorted(sh, reverse=True) if day not in sz]
    batch = missing[:BACKFILL_DATES_PER_RUN]
    for day in batch:
        # SZSE publishes the aggregate by date and reports monetary values in
        # 亿元. Retry transient exchange failures; keep the checkpoint if still
        # unavailable so the next scheduled run resumes at this date.
        last_error = None
        for attempt in range(3):
            try:
                response = requests.get(
                    "https://www.szse.cn/api/report/ShowReport/data",
                    params={
                        "SHOWTYPE": "JSON", "CATALOGID": "1837_xxpl",
                        "txtDate": day, "tab1PAGENO": "1", "random": str(time.time()),
                    },
                    headers={
                        "Referer": "https://www.szse.cn/disclosure/margin/object/index.html",
                        "User-Agent": "Mozilla/5.0",
                    },
                    timeout=30,
                )
                response.raise_for_status()
                payload = response.json()
                item = (payload[0].get("data") or [None])[0]
                if not item:
                    raise ValueError(f"SZSE returned invalid values for {day}")
                balance = float(str(item["jrrzye"]).replace(",", ""))
                buy = float(str(item["jrrzmr"]).replace(",", ""))
                sz[day] = {"balance": balance * 1e8, "buy": buy * 1e8}
                time.sleep(0.1)
                break
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(1 + attempt * 2)
        else:
            if backfill_pending:
                with open(BACKFILL_CACHE, "w", encoding="utf-8") as f:
                    json.dump(progress, f, ensure_ascii=False, separators=(",", ":"))
            raise RuntimeError(f"SZSE backfill paused at {day}: {last_error}") from last_error

    if batch and backfill_pending:
        with open(BACKFILL_CACHE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, separators=(",", ":"))

    missing = [day for day in sh if day not in sz]
    if missing:
        if len(old_rows) >= 30:
            print(f"Official history rebuild: {len(sz)}/{len(sh)} SZSE dates complete; retaining old published cache until complete")
            return old_rows
        raise RuntimeError(f"Official history rebuild in progress: {len(sz)}/{len(sh)} SZSE dates complete")

    combined = {row["date"]: row for row in old_rows} if not backfill_pending else {}
    for day in sorted(set(sh) & set(sz)):
        sh_item, sz_item = sh[day], sz[day]
        total = sh_item["balance"] + sz_item["balance"]
        combined[day] = {
            "date": day,
            "sh_balance": round(sh_item["balance"] / 1e8, 4),
            "sz_balance": round(sz_item["balance"] / 1e8, 4),
            "balance": round(total / 1e8, 4),
            "buy": round((sh_item["buy"] + sz_item["buy"]) / 1e8, 4),
            "flow_est": None,
        }
    if backfill_pending and len(combined) < 1000:
        raise RuntimeError("Official history rebuild produced too few overlapping dates")
    if not backfill_pending and len(combined) < 30:
        raise RuntimeError("Official refresh produced too few observations")
    result, previous_total = [], None
    for day in sorted(combined):
        row = combined[day]
        total = row["balance"]
        row["flow_est"] = round(total - previous_total, 4) if previous_total is not None else None
        result.append(row)
        previous_total = total
    if backfill_pending:
        os.remove(BACKFILL_CACHE)
    return result


def fetch_index(dates):
    if not dates:
        return {}
    result = {}
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
    for offset in range(0, len(dates), 900):
        chunk = dates[offset:offset + 900]
        url = (
            "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
            "&ut=7eea3edcaed734bea9cbfc24409ed989&klt=101&fqt=0&secid=1.000300"
            f"&beg={chunk[0].replace('-', '')}&end={chunk[-1].replace('-', '')}&lmt=1000"
        )
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        lines = (response.json().get("data") or {}).get("klines") or []
        result.update({f[0]: float(f[2]) for line in lines if len(f := line.split(",")) >= 3})
    return result


def _float_cap_from_exchange_frames(sh, sz):
    """Return total A-share float market cap in yuan from exchange summaries."""
    sh_value = 0.0
    sh_label = "单日情况" if "单日情况" in sh.columns else "项目"
    for category in ("主板A", "科创板"):
        row = sh.loc[sh[sh_label].astype(str).str.strip() == "流通市值"]
        if row.empty or category not in row.columns:
            raise ValueError(f"SSE summary missing {category} float market cap")
        sh_value += float(pd.to_numeric(row.iloc[0][category], errors="raise")) * 1e8

    sz_categories = ("主板A股", "中小板", "创业板A股")
    sz_value = 0.0
    for category in sz_categories:
        row = sz.loc[sz["证券类别"].astype(str).str.strip() == category]
        if row.empty:
            # Some post-reform summaries consolidate the former SME board.
            if category == "中小板":
                continue
            raise ValueError(f"SZSE summary missing {category} float market cap")
        sz_value += float(pd.to_numeric(row.iloc[0]["流通市值"], errors="raise"))
    if sh_value <= 0 or sz_value <= 0:
        raise ValueError("Non-positive exchange float market cap")
    return sh_value + sz_value


def update_float_cap_cache(rows):
    """Incrementally backfill official daily float-cap data from 2021-12-27."""
    try:
        with open(CAP_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {"source": "SSE/SZSE daily exchange summaries", "values": {}}
    values = cache.setdefault("values", {})
    # SSE's daily overview format is available from 2021-12-27; walk backwards
    # from the latest date so the dashboard gradually gains a longer ratio line.
    dates = [
        r["date"] for r in reversed(rows)
        if r["date"] >= "2021-12-27" and r["date"] not in values
    ]
    for day in dates[:20]:
        try:
            sh = ak.stock_sse_deal_daily(date=day.replace("-", ""))
            sz = ak.stock_szse_summary(date=day.replace("-", ""))
            values[day] = round(_float_cap_from_exchange_frames(sh, sz) / 1e8, 4)
        except Exception as exc:
            print(f"Float cap unavailable for {day}: {exc}", file=sys.stderr)
    cache["updated_at"] = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S (北京时间)")
    with open(CAP_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
    return values


def load_cache():
    try:
        with open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def build_page(rows, updated):
    indexes = fetch_index([x["date"] for x in rows])
    caps = update_float_cap_cache(rows)
    for row in rows:
        row["index"] = indexes.get(row["date"])
        row["float_mcap"] = caps.get(row["date"])
        row["margin_cap_pct"] = round(row["balance"] / row["float_mcap"] * 100, 4) if row.get("float_mcap") else None
    data = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return PAGE_TEMPLATE.replace("__DATA__", data).replace("__UPDATED__", updated)


PAGE_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>沪深融资余额分析</title><script src="https://registry.npmmirror.com/echarts/5.5.0/files/dist/echarts.min.js"></script>
<style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1a1d24;--mut:#7a8194;--line:#e6e8ee;--blue:#2f54eb;--red:#d43d2a;--green:#1f9d7b}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1120px;margin:auto;padding:20px 16px 50px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}
h1{font-size:22px;margin:0}.nav a{color:var(--blue);text-decoration:none;margin-left:14px}.sub,.note{color:var(--mut);font-size:12px}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:18px 0}.kpi,.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.kpi label{display:block;color:var(--mut);font-size:12px}.kpi strong{font-size:21px;font-variant-numeric:tabular-nums}.card{margin:14px 0}
.card h2{font-size:15px;margin:0 0 6px}.controls{display:flex;gap:7px;align-items:center;flex-wrap:wrap;margin:8px 0}
.controls button{border:1px solid var(--line);background:white;border-radius:7px;padding:5px 11px;cursor:pointer}.controls button.sel{background:var(--blue);color:white}
.chart{height:370px;width:100%}.small{height:300px}.note{line-height:1.8;margin-top:18px}.tag{display:inline-block;border-radius:20px;background:#eef1ff;color:#3450c7;padding:2px 9px;font-size:11px}
@media(max-width:650px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.chart{height:310px}.small{height:270px}}
</style></head><body><main class="wrap"><header class="top"><div><h1>沪深融资余额分析</h1><div class="sub">数据截至 <b id="asof">—</b> · 更新于 __UPDATED__</div></div><nav class="nav"><a href="./">返回 ETF 图表</a></nav></header>
<div class="kpis"><div class="kpi"><label>沪深融资余额</label><strong id="total">—</strong><div class="sub">亿元</div></div><div class="kpi"><label>近 5 个交易日余额变化</label><strong id="week">—</strong><div class="sub">亿元 · 余额变动估算</div></div><div class="kpi"><label>近 21 个交易日余额变化</label><strong id="month">—</strong><div class="sub">亿元 · 余额变动估算</div></div><div class="kpi"><label>最新日融资买入额</label><strong id="buy">—</strong><div class="sub">亿元</div></div></div>
<section class="card"><h2>融资余额、沪深300与融资余额 / A股流通市值</h2><div class="controls"><span class="sub">范围</span><button data-range="60">近 3 个月</button><button data-range="120">近 6 个月</button><button data-range="250" class="sel">近 1 年</button><button data-range="0">全部</button><span class="tag">沪深合计</span></div><div id="trend" class="chart"></div><div class="sub">比例仅在流通市值缓存覆盖日期显示；交易所历史市值从 2021-12-27 起按每次最多 20 个交易日逐步补齐。分母为沪市 A 股（主板、科创板）与深市 A 股流通市值合计。</div></section>
<section class="card"><h2>融资余额变化与融资买入额</h2><div class="sub">红柱为融资余额增加，绿柱为减少；折线为沪深融资买入额。</div><div id="flow" class="chart small"></div></section>
<p class="note"><b>数据与口径：</b>沪市、深市融资余额及融资买入额来自上交所、深交所官方公开汇总数据，仅显示两市均有记录的日期。执行全量回补时，程序按批次从最新日期向前获取；回补完成前暂时保留旧报表，完成后整段替换为官方历史。流通市值使用上交所每日股票情况中的主板、科创板“流通市值”和深交所每日证券类别汇总中的 A 股流通市值，合计后计算“融资余额 / A股流通市值 (%)”；B股、基金及其他证券不纳入。交易所历史市值比例自 2021-12-27 起逐步回填。融资余额为融资负债余额；不等同于“融资融券余额”（后者还包含融券余额）。融资偿还额没有在历史汇总接口中稳定提供，因此图中“余额变化估算”=当日沪深合计融资余额−前一交易日合计融资余额，可能受权益调整等因素影响，不能当作精确的融资净买入。指数使用沪深300日收盘价，展示为起点=100的归一化走势。数据每日由 GitHub Actions 更新，非实时行情。</p>
<script>const D=__DATA__;const fmt=x=>Number(x).toLocaleString('zh-CN',{maximumFractionDigits:1});const last=D[D.length-1];document.getElementById('asof').textContent=last.date;document.getElementById('total').textContent=fmt(last.balance);document.getElementById('buy').textContent=fmt(last.buy);document.getElementById('week').textContent=(last.balance-D[Math.max(0,D.length-6)].balance>=0?'+':'')+fmt(last.balance-D[Math.max(0,D.length-6)].balance);document.getElementById('month').textContent=(last.balance-D[Math.max(0,D.length-22)].balance>=0?'+':'')+fmt(last.balance-D[Math.max(0,D.length-22)].balance);
const dates=D.map(x=>x.date),baseIndex=(D.find(x=>x.index)?.index)||1,idx=D.map(x=>x.index?x.index/baseIndex*100:null);const trend=echarts.init(document.getElementById('trend')),flow=echarts.init(document.getElementById('flow'));let range=250;
function optionTrend(){const start=range?Math.max(0,D.length-range):0;return {animation:false,legend:{top:4},tooltip:{trigger:'axis',axisPointer:{type:'cross'}},grid:{left:60,right:105,top:45,bottom:55},xAxis:{type:'category',data:dates,axisLabel:{show:false}},yAxis:[{type:'value',name:'亿元',scale:true},{type:'value',name:'沪深300',scale:true},{type:'value',name:'占比(%)',scale:true,position:'right',offset:48,axisLabel:{formatter:'{value}%'}}],dataZoom:[{type:'inside',startValue:dates[start]},{type:'slider',startValue:dates[start],height:18,bottom:12}],series:[{name:'融资余额',type:'line',showSymbol:false,yAxisIndex:0,data:D.map(x=>x.balance),lineStyle:{width:2,color:'#2f54eb'}},{name:'沪深300（起点=100）',type:'line',showSymbol:false,yAxisIndex:1,data:idx,lineStyle:{width:1.5,color:'#f59e0b'}},{name:'融资余额 / A股流通市值',type:'line',showSymbol:false,yAxisIndex:2,data:D.map(x=>x.margin_cap_pct),connectNulls:false,lineStyle:{width:1.8,color:'#16a085'}}]}}
function optionFlow(){const start=range?Math.max(0,D.length-range):0;return {animation:false,legend:{top:4},tooltip:{trigger:'axis'},grid:{left:60,right:65,top:38,bottom:55},xAxis:{type:'category',data:dates,axisLabel:{show:false}},yAxis:[{type:'value',name:'余额变动(亿元)'},{type:'value',name:'买入额(亿元)'}],dataZoom:[{type:'inside',startValue:dates[start]},{type:'slider',startValue:dates[start],height:16,bottom:10}],series:[{name:'余额变化估算',type:'bar',yAxisIndex:0,data:D.map(x=>({value:x.flow_est,itemStyle:{color:x.flow_est>=0?'#d43d2a':'#1f9d7b'}}))},{name:'融资买入额',type:'line',yAxisIndex:1,showSymbol:false,data:D.map(x=>x.buy),lineStyle:{color:'#7c3aed',width:1.5}}]}}
trend.setOption(optionTrend());flow.setOption(optionFlow());echarts.connect([trend,flow]);document.querySelectorAll('[data-range]').forEach(b=>{b.onclick=()=>{document.querySelectorAll('[data-range]').forEach(x=>x.classList.remove('sel'));b.classList.add('sel');range=Number(b.dataset.range);trend.setOption(optionTrend(),true)}});window.addEventListener('resize',()=>{trend.resize();flow.resize()});</script></main></body></html>'''


def main(force=False, rebuild=False):
    if rebuild:
        # Keep the published margin history untouched until the replacement is
        # complete. The temporary checkpoint is the only state cleared here.
        if os.path.exists(BACKFILL_CACHE):
            os.remove(BACKFILL_CACHE)
        try:
            os.remove(CAP_CACHE)
        except FileNotFoundError:
            pass
        force = True
    previous = load_cache()
    last_refresh = previous.get("updated_at", "")
    try:
        refreshed = datetime.strptime(last_refresh[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CN)
    except ValueError:
        refreshed = datetime.min.replace(tzinfo=TZ_CN)
    backfill_pending = os.path.exists(BACKFILL_CACHE)
    if previous.get("rows") and not force and not backfill_pending and datetime.now(TZ_CN) - refreshed < timedelta(hours=6):
        rows, updated = previous["rows"], last_refresh
    else:
        fetched = fetch_market_data(rebuild=rebuild)
        new_source = "SSE/SZSE official all-history" if rebuild or backfill_pending else "SSE/SZSE official recent; historical baseline retained"
        if fetched == previous.get("rows") and (
            previous.get("source") == new_source or os.path.exists(BACKFILL_CACHE)
        ):
            rows, updated = fetched, last_refresh
        else:
            rows = fetched
            updated = datetime.now(TZ_CN).strftime("%Y-%m-%d %H:%M:%S (北京时间)")
            cache = {"updated_at": updated, "source": new_source, "rows": rows}
            with open(CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, separators=(",", ":"))
    html = build_page(rows, updated)
    with open(PAGE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Generated {PAGE}: {len(rows)} observations through {rows[-1]['date']}")


if __name__ == "__main__":
    main("--force" in sys.argv, "--rebuild" in sys.argv)


