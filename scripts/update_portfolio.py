"""
SWING Portfolio 자동 업데이트 스크립트

흐름:
1) 매매일지 조회 -> 보유주식 갱신 -> 총자산에 오늘자 스냅샷 추가 -> 총자산/분류별 차트 생성
2) 관심종목(워치리스트)을 기준지수(코스피200/S&P500/나스닥100) 대비 6개월 수익률로 분석하고
   알파값(초과수익률) 기준으로 판정(유지/매수확대/손절검토)을 매겨 갱신 -> 대조 차트 생성

원칙: 기존 행/블록은 절대 삭제하지 않습니다. 보유주식·관심종목은 기존 행을 찾으면 patch,
없으면 새로 생성합니다. 총자산은 매 실행마다 새 행을 "추가"하여 히스토리를 쌓습니다.

실행 환경: GitHub Actions (스케줄 + 수동 실행)
필요 환경변수: NOTION_TOKEN (Notion Internal Integration Token, GitHub Secrets에 등록)
"""

import os
import requests
from datetime import datetime, timezone, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
fm.fontManager.addfont("/usr/share/fonts/truetype/nanum/NanumGothic.ttf")
plt.rcParams['font.family'] = 'NanumGothic'
plt.rcParams['axes.unicode_minus'] = False

try:
    import FinanceDataReader as fdr
except ImportError:
    fdr = None

# ── 고정 설정값 (데이터소스 ID는 비밀값이 아니므로 코드에 그대로 둬도 안전합니다) ──
TRADE_LOG_DS_ID = "9a0c7b65-17c2-4042-973d-075ed4421ba0"     # 매매일지
HOLDINGS_DS_ID = "89b0cb64-c32a-4646-a336-72af409d81f5"      # 보유주식
TOTAL_ASSETS_DS_ID = "14fc5390-f230-4332-8682-740ab1548b86"  # 총자산
WATCHLIST_DS_ID = "fb2cee25-90a8-41e5-9224-50eb53b3d05b"     # 관심종목

# 나스닥100은 FinanceDataReader가 지수 코드를 직접 지원하지 않아 추적 ETF인 QQQ로 대체합니다.
BENCHMARK_TICKERS = {
    "코스피200": "KS200",
    "S&P500": "S&P500",
    "나스닥100": "QQQ",
}
ALPHA_SELL_THRESHOLD = -10   # 알파값이 이 값 이하면 "손절검토"
ALPHA_BUY_THRESHOLD = 40     # 알파값이 이 값 이상이면 "매수확대" (명시 안 된 임의 기준값, 필요시 조정)

NOTION_TOKEN = os.environ["NOTION_TOKEN"]  # GitHub Secrets에서 주입됩니다
NOTION_VERSION = "2025-09-03"
BASE_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

KST = timezone(timedelta(hours=9))


# ───────────────────────── Notion 공통 헬퍼 ─────────────────────────

def query_data_source(data_source_id):
    """데이터소스의 모든 페이지를 가져옵니다 (페이지네이션 처리)."""
    results = []
    payload = {"page_size": 100}
    while True:
        resp = requests.post(
            f"{BASE_URL}/data_sources/{data_source_id}/query",
            headers=HEADERS, json=payload, timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]
    return results


def get_title(prop):
    arr = prop.get("title", [])
    return arr[0]["plain_text"] if arr else ""


def get_rich_text(prop):
    arr = prop.get("rich_text", [])
    return arr[0]["plain_text"] if arr else ""


def get_number(prop):
    return prop.get("number") or 0


def get_select(prop):
    sel = prop.get("select")
    return sel["name"] if sel else None


def get_date(prop):
    d = prop.get("date")
    return d["start"] if d else None


# ───────────────────────── 1) 매매일지 읽기 ─────────────────────────

def fetch_trade_log():
    pages = query_data_source(TRADE_LOG_DS_ID)
    trades = []
    for p in pages:
        props = p["properties"]
        trades.append({
            "종목이름": get_title(props["종목이름"]),
            "날짜": get_date(props["날짜"]),
            "티커": get_rich_text(props["티커"]),
            "매수매도": get_select(props["매수매도"]),
            "수량": get_number(props["수량"]),
            "단가": get_number(props["단가"]),
            "분류": get_select(props["분류"]),
        })
    return trades


# ───────────────────────── 2) 보유주식 계산/갱신 ─────────────────────────

def compute_holdings(trades):
    """이동평균법으로 평균매입단가와 보유수량을 계산합니다."""
    book = {}
    for t in sorted(trades, key=lambda x: x["날짜"] or ""):
        key = t["티커"]
        if key not in book:
            book[key] = {
                "종목이름": t["종목이름"], "티커": t["티커"],
                "분류": t["분류"], "보유수량": 0.0, "총매입금액": 0.0,
            }
        b = book[key]
        b["종목이름"] = t["종목이름"]
        b["분류"] = t["분류"]
        if t["매수매도"] == "매수":
            b["보유수량"] += t["수량"]
            b["총매입금액"] += t["수량"] * t["단가"]
        elif t["매수매도"] == "매도":
            avg_cost = (b["총매입금액"] / b["보유수량"]) if b["보유수량"] else 0
            b["보유수량"] -= t["수량"]
            b["총매입금액"] -= avg_cost * t["수량"]
    return book


def get_price(ticker):
    """KRX/해외 종가 기준 현재가를 가져옵니다. 실패 시 None."""
    if fdr is None:
        return None
    try:
        df = fdr.DataReader(ticker)
        return float(df["Close"].iloc[-1])
    except Exception as e:
        print(f"[WARN] {ticker} 시세 조회 실패: {e}")
        return None


_holdings_cache = None


def find_holding_page(ticker, cache):
    if cache is None:
        cache = {get_rich_text(p["properties"]["티커"]): p["id"]
                 for p in query_data_source(HOLDINGS_DS_ID)}
    return cache.get(ticker), cache


def upsert_holding(holding):
    global _holdings_cache
    avg_cost = (holding["총매입금액"] / holding["보유수량"]) if holding["보유수량"] else 0
    qty = holding["보유수량"]
    price = get_price(holding["티커"]) or avg_cost
    valuation = price * qty
    profit = valuation - avg_cost * qty
    profit_rate = (profit / (avg_cost * qty)) if avg_cost * qty else 0

    properties = {
        "종목이름": {"title": [{"text": {"content": holding["종목이름"]}}]},
        "티커": {"rich_text": [{"text": {"content": holding["티커"]}}]},
        "평가금액": {"number": round(valuation)},
        "수익": {"number": round(profit)},
        "수익률": {"number": round(profit_rate, 4)},
        "보유수량": {"number": qty},
        "매입가": {"number": round(avg_cost)},
        "분류": {"select": {"name": holding["분류"]}},
    }

    page_id, _holdings_cache = find_holding_page(holding["티커"], _holdings_cache)
    if page_id:
        requests.patch(f"{BASE_URL}/pages/{page_id}", headers=HEADERS,
                        json={"properties": properties}, timeout=30).raise_for_status()
    else:
        resp = requests.post(
            f"{BASE_URL}/pages", headers=HEADERS,
            json={"parent": {"type": "data_source_id", "data_source_id": HOLDINGS_DS_ID},
                  "properties": properties}, timeout=30)
        resp.raise_for_status()
        _holdings_cache[holding["티커"]] = resp.json()["id"]

    return {"valuation": valuation, "profit": profit, "cost": avg_cost * qty}


# ───────────────────────── 3) 총자산 스냅샷 추가 ─────────────────────────

def append_total_assets_snapshot(total_valuation, total_profit, total_cost):
    today = datetime.now(KST).strftime("%Y-%m-%d")
    profit_rate = (total_profit / total_cost) if total_cost else 0
    properties = {
        "기록명": {"title": [{"text": {"content": f"{today} 자산현황"}}]},
        "작성일자": {"date": {"start": today}},
        "총평가금액": {"number": round(total_valuation)},
        "총수익": {"number": round(total_profit)},
        "총수익률": {"number": round(profit_rate, 4)},
    }
    requests.post(
        f"{BASE_URL}/pages", headers=HEADERS,
        json={"parent": {"type": "data_source_id", "data_source_id": TOTAL_ASSETS_DS_ID},
              "properties": properties}, timeout=30).raise_for_status()


# ───────────────────────── 4) 차트 생성 ─────────────────────────

def draw_chart():
    pages = query_data_source(TOTAL_ASSETS_DS_ID)
    rows = []
    for p in pages:
        props = p["properties"]
        date = get_date(props["작성일자"])
        value = get_number(props["총평가금액"])
        if date:
            rows.append((date, value))
    rows.sort(key=lambda r: r[0])
    if not rows:
        return

    dates = [r[0] for r in rows]
    values = [r[1] for r in rows]

    os.makedirs("charts", exist_ok=True)
    plt.figure(figsize=(9, 4))
    plt.plot(dates, values, marker="o", linewidth=2)
    plt.title("SWING Portfolio 총자산 추이")
    plt.xticks(rotation=45, ha="right")
    plt.ylabel("총평가금액 (원)")
    plt.tight_layout()
    plt.savefig("charts/total_assets.png", dpi=150)
    plt.close()


def draw_category_chart():
    pages = query_data_source(HOLDINGS_DS_ID)
    totals = {}
    for p in pages:
        props = p["properties"]
        qty = get_number(props["보유수량"])
        if qty <= 0:
            continue
        category = get_select(props["분류"])
        valuation = get_number(props["평가금액"])
        totals[category] = totals.get(category, 0) + valuation

    if not totals:
        return

    labels = list(totals.keys())
    values = list(totals.values())
    total_sum = sum(values)

    os.makedirs("charts", exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))

    axes[0].pie(values, labels=labels, autopct="%1.1f%%", startangle=90)
    axes[0].set_title("보유주식 분류별 비율")

    axes[1].axis("off")
    table_rows = [[label, f"{value:,.0f}원", f"{value / total_sum * 100:.1f}%"]
                  for label, value in zip(labels, values)]
    table_rows.append(["합계", f"{total_sum:,.0f}원", "100%"])
    table = axes[1].table(cellText=table_rows, colLabels=["분류", "평가금액", "비율"],
                           loc="center", cellLoc="center")
    table.scale(1, 1.8)
    axes[1].set_title("분류별 평가금액")

    plt.tight_layout()
    plt.savefig("charts/category_breakdown.png", dpi=150)
    plt.close()


# ───────────────────────── 5) 관심종목 지수 분석 ─────────────────────────

def fetch_watchlist():
    pages = query_data_source(WATCHLIST_DS_ID)
    items = []
    for p in pages:
        props = p["properties"]
        items.append({
            "page_id": p["id"],
            "종목명": get_title(props["종목명"]),
            "티커": get_rich_text(props["티커"]),
            "기준지수": get_select(props["기준지수"]),
        })
    return items


def total_return_pct(series):
    if series is None or len(series) < 2:
        return None
    return (series.iloc[-1] / series.iloc[0] - 1) * 100


def normalized_pct_series(series):
    return (series / series.iloc[0] - 1) * 100


def verdict_for(alpha):
    if alpha <= ALPHA_SELL_THRESHOLD:
        return "손절검토"
    if alpha >= ALPHA_BUY_THRESHOLD:
        return "매수확대"
    return "유지"


def run_index_analysis():
    if fdr is None:
        return
    watchlist = fetch_watchlist()
    if not watchlist:
        return

    start_date = (datetime.now(KST) - timedelta(days=183)).strftime("%Y-%m-%d")
    benchmark_series_cache = {}
    chart_groups = {}  # 기준지수 -> {"benchmark_series", "benchmark_ret", "stocks": [(name, series, ret)]}

    for item in watchlist:
        bench_name = item["기준지수"]
        if bench_name not in benchmark_series_cache:
            bench_ticker = BENCHMARK_TICKERS.get(bench_name)
            try:
                b_df = fdr.DataReader(bench_ticker, start_date)
                benchmark_series_cache[bench_name] = b_df["Close"]
            except Exception as e:
                print(f"[WARN] 기준지수 {bench_name}({bench_ticker}) 조회 실패: {e}")
                benchmark_series_cache[bench_name] = None

        b_series = benchmark_series_cache[bench_name]
        if b_series is None:
            continue

        try:
            s_df = fdr.DataReader(item["티커"], start_date)
            s_series = s_df["Close"]
        except Exception as e:
            print(f"[WARN] {item['종목명']}({item['티커']}) 시세 조회 실패: {e}")
            continue

        stock_ret = total_return_pct(s_series)
        bench_ret = total_return_pct(b_series)
        if stock_ret is None or bench_ret is None:
            continue

        alpha = stock_ret - bench_ret
        verdict_label = verdict_for(alpha)

        properties = {
            "6개월수익률": {"number": round(stock_ret / 100, 4)},
            "기준지수수익률": {"number": round(bench_ret / 100, 4)},
            "알파값": {"number": round(alpha / 100, 4)},
            "판정": {"select": {"name": verdict_label}},
        }
        requests.patch(f"{BASE_URL}/pages/{item['page_id']}", headers=HEADERS,
                        json={"properties": properties}, timeout=30).raise_for_status()

        group = chart_groups.setdefault(bench_name, {
            "benchmark_series": normalized_pct_series(b_series),
            "benchmark_ret": bench_ret,
            "stocks": [],
        })
        group["stocks"].append((item["종목명"], normalized_pct_series(s_series), stock_ret))

    draw_index_chart(chart_groups)


def draw_index_chart(chart_groups):
    order = ["코스피200", "S&P500", "나스닥100"]
    groups = [name for name in order if name in chart_groups]
    if not groups:
        return

    os.makedirs("charts", exist_ok=True)
    fig, axes = plt.subplots(len(groups), 1, figsize=(9, 4 * len(groups)))
    if len(groups) == 1:
        axes = [axes]

    for ax, bench_name in zip(axes, groups):
        group = chart_groups[bench_name]
        ax.plot(group["benchmark_series"].index, group["benchmark_series"].values,
                linestyle="--", color="gray",
                label=f"{bench_name} ({group['benchmark_ret']:+.1f}%)")
        for name, series, ret in group["stocks"]:
            ax.plot(series.index, series.values, label=f"{name} ({ret:+.1f}%)")
        ax.set_title(f"vs {bench_name}")
        ax.set_ylabel("수익률 (%)")
        ax.legend(fontsize=8, loc="upper left")
        ax.axhline(0, color="lightgray", linewidth=0.8)

    fig.suptitle("관심종목 6개월 수익률 대조")
    plt.tight_layout()
    plt.savefig("charts/index_comparison.png", dpi=150)
    plt.close()


# ───────────────────────── 메인 실행 ─────────────────────────

def main():
    trades = fetch_trade_log()
    if trades:
        book = compute_holdings(trades)

        total_valuation = total_profit = total_cost = 0.0
        for holding in book.values():
            result = upsert_holding(holding)
            if holding["보유수량"] > 0:
                total_valuation += result["valuation"]
                total_profit += result["profit"]
                total_cost += result["cost"]

        append_total_assets_snapshot(total_valuation, total_profit, total_cost)
        draw_chart()
        draw_category_chart()
    else:
        print("매매일지가 비어 있어 보유주식/총자산 갱신을 건너뜁니다.")

    run_index_analysis()
    print("업데이트 완료")


if __name__ == "__main__":
    main()
