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
import time
import pandas as pd
import matplotlib.dates as mdates
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
PAGE_ID = "38104f81-11a0-81c2-9a8c-d3b4e738aa2b"
LEADING_PAGE_ID = "38104f81-11a0-81c2-9a8c-d3b4e738aa2b"   # JHING Portfolio 메인 페이지
RESULTS_DS_ID = "c33ca7b7-17f3-45c2-95fc-07b53752bd2b"      # 주도대장주 결과 DB

WICS_SECTORS = {
    "에너지": "G10", "소재": "G15", "산업재": "G20", "자유소비재": "G25",
    "필수소비재": "G30", "건강관리": "G35", "금융": "G40", "IT": "G45",
    "커뮤니케이션서비스": "G50", "유틸리티": "G55",
}
TOP_N_PER_SECTOR = 5
LEAD_LOOKBACK_DAYS = 400
LEAD_RETURN_WINDOW_DAYS = 183

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

    # 오늘 날짜로 이미 기록된 행이 있으면 새로 추가하지 않고 그 행을 갱신합니다.
    existing_id = None
    for p in query_data_source(TOTAL_ASSETS_DS_ID):
        if get_date(p["properties"]["작성일자"]) == today:
            existing_id = p["id"]
            break

    if existing_id:
        requests.patch(f"{BASE_URL}/pages/{existing_id}", headers=HEADERS,
                        json={"properties": properties}, timeout=30).raise_for_status()
    else:
        requests.post(
            f"{BASE_URL}/pages", headers=HEADERS,
            json={"parent": {"type": "data_source_id", "data_source_id": TOTAL_ASSETS_DS_ID},
                  "properties": properties}, timeout=30).raise_for_status()
def update_summary_banner(total_valuation, total_profit, profit_rate):
    """페이지 맨 위 요약 두 줄(작성일자 / 총평가금액·총수익·총수익률)을 굵게 갱신합니다."""
    resp = requests.get(f"{BASE_URL}/blocks/{PAGE_ID}/children", headers=HEADERS,
                         params={"page_size": 100}, timeout=30)
    resp.raise_for_status()
    today = datetime.now(KST).strftime("%Y-%m-%d")

    line1 = f"📅 작성일자: {today}"
    line2 = (f"💰 총평가금액 {total_valuation:,.0f}원   "
             f"📈 총수익 {total_profit:+,.0f}원   "
             f"📊 총수익률 {profit_rate * 100:+.2f}%")

    def set_bold(block_id, btype, content):
        requests.patch(
            f"{BASE_URL}/blocks/{block_id}", headers=HEADERS,
            json={btype: {"rich_text": [
                {"type": "text", "text": {"content": content},
                 "annotations": {"bold": True}}
            ]}},
            timeout=30,
        ).raise_for_status()

    for block in resp.json()["results"]:
        btype = block.get("type")
        rich = block.get(btype, {}).get("rich_text", [])
        text = "".join(t.get("plain_text", "") for t in rich)
        if text.startswith("📅 작성일자"):
            set_bold(block["id"], btype, line1)
        elif text.startswith("💰 총평가금액"):
            set_bold(block["id"], btype, line2)
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
        if (stock_ret != stock_ret or bench_ret != bench_ret or
                stock_ret in (float("inf"), float("-inf")) or
                bench_ret in (float("inf"), float("-inf"))):
            print(f"[WARN] {item['종목명']} 수익률 계산값이 비정상(NaN/Inf)이라 건너뜁니다.")
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


# ───────────────────────── 7) 국내 주도대장주 분석 ─────────────────────────

def find_recent_wics_date(max_tries=30):
    """오늘부터 거슬러 올라가며 WICS 데이터가 존재하는 가장 최근 영업일을 찾습니다."""
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.wiseindex.com/Index/Index?G10="
    }

    d = datetime.now(KST)

    for _ in range(max_tries):
        date_str = d.strftime("%Y%m%d")
        url = (
            "https://www.wiseindex.com/Index/GetIndexComponets"
            f"?ceil_yn=0&dt={date_str}&sec_cd=G10"
        )

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            print(f"[DEBUG] WICS 날짜 확인: {date_str}, status={resp.status_code}")

            resp.raise_for_status()
            data = resp.json()

            if data.get("list"):
                return date_str

        except Exception as e:
            print(f"[WARN] WICS 날짜 확인 실패: {date_str} / {e}")

        d -= timedelta(days=1)

    raise RuntimeError("최근 WICS 데이터를 찾지 못했습니다. WiseIndex API URL 또는 차단 여부를 확인하세요.")

def fetch_wics_data(date_str):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.wiseindex.com/Index/Index?G10="
    }

    rows = []

    for sector_name, seq in WICS_SECTORS.items():
        url = (
            "https://www.wiseindex.com/Index/GetIndexComponets"
            f"?ceil_yn=0&dt={date_str}&sec_cd={seq}"
        )

        try:
            resp = requests.get(url, headers=headers, timeout=15)
            print(f"[DEBUG] WICS 수집: {sector_name} {seq}, status={resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[WARN] WICS 수집 실패: {sector_name}({seq}) / {e}")
            continue

        for item in data.get("list", []):
            rows.append({
                "종목코드": str(item.get("CMP_CD")).zfill(6),
                "종목명": item.get("CMP_KOR"),
                "섹터": sector_name,
            })

        time.sleep(0.3)

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError("WICS 데이터를 수집했지만 결과가 비어 있습니다.")

    print(f"[INFO] WICS 수집 완료: {len(df)}개 종목")
    print(df.groupby("섹터").size())

    return df


def fetch_kospi_listing():
    df = fdr.StockListing("KOSPI")
    df = df.rename(columns={"Code": "종목코드", "Marcap": "시가총액"})
    df["종목코드"] = df["종목코드"].astype(str).str.zfill(6)
    df["시가총액"] = pd.to_numeric(df["시가총액"], errors="coerce")
    df = df.dropna(subset=["시가총액"])
    return df[["종목코드", "시가총액"]]


def build_sector_leaders():
    wics_date = find_recent_wics_date()
    print(f"[INFO] WICS 기준일: {wics_date}")
    wics_df = fetch_wics_data(wics_date)
    kospi_df = fetch_kospi_listing()

    merged = wics_df.merge(kospi_df, on="종목코드", how="inner")
    merged = merged.dropna(subset=["섹터", "시가총액"])

    leaders = (
        merged.sort_values("시가총액", ascending=False)
        .groupby("섹터", group_keys=False)
        .head(TOP_N_PER_SECTOR)
        .reset_index(drop=True)
    )
    print(f"[INFO] 섹터 대장주 {len(leaders)}개 추출 완료")
    return leaders, wics_date


def analyze_leading_stock(ticker, start_date):
    df = fdr.DataReader(ticker, start_date)
    if df is None or len(df) < 130:
        return None
    close = df["Close"]
    current_price = close.iloc[-1]

    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    ma120 = close.rolling(120).mean().iloc[-1]

    one_year = close[close.index >= (close.index[-1] - pd.Timedelta(days=365))]
    high_52w = one_year.max()

    six_month = close[close.index >= (close.index[-1] - pd.Timedelta(days=LEAD_RETURN_WINDOW_DAYS))]
    if len(six_month) < 2:
        return None
    six_month_return = (six_month.iloc[-1] / six_month.iloc[0] - 1) * 100

    return {
        "현재가": current_price, "MA20": ma20, "MA60": ma60, "MA120": ma120,
        "52주최고가": high_52w, "6개월수익률": six_month_return,
        "series": normalized_pct_series(six_month),
    }


def judge_leading_conditions(stock, bench_return):
    failed = []
    if not (stock["6개월수익률"] > bench_return):
        failed.append("1")
    if not (stock["현재가"] > stock["MA120"]):
        failed.append("2")
    if not (stock["MA20"] > stock["MA60"]):
        failed.append("3")
    high_ratio = stock["현재가"] / stock["52주최고가"] * 100
    if not (high_ratio >= 80):
        failed.append("4")
    verdict = "주도주" if not failed else "제외"
    return verdict, failed, high_ratio


_leading_results_cache = None


def upsert_leading_result(row):
    global _leading_results_cache
    if _leading_results_cache is None:
        _leading_results_cache = {get_rich_text(p["properties"]["티커"]): p["id"]
                                   for p in query_data_source(RESULTS_DS_ID)}

    properties = {
        "종목명": {"title": [{"text": {"content": row["종목명"]}}]},
        "티커": {"rich_text": [{"text": {"content": row["종목코드"]}}]},
        "섹터": {"rich_text": [{"text": {"content": row["섹터"]}}]},
        "6개월수익률": {"number": round(row["6개월수익률"] / 100, 4)},
        "상대수익률": {"number": round(row["상대수익률"] / 100, 4)},
        "52주고점비율": {"number": round(row["52주고점비율"] / 100, 4)},
        "판정": {"select": {"name": row["판정"]}},
        "미충족조건": {"rich_text": [{"text": {"content": row["미충족조건"]}}]},
    }

    page_id = _leading_results_cache.get(row["종목코드"])
    if page_id:
        requests.patch(f"{BASE_URL}/pages/{page_id}", headers=HEADERS,
                        json={"properties": properties}, timeout=30).raise_for_status()
    else:
        resp = requests.post(
            f"{BASE_URL}/pages", headers=HEADERS,
            json={"parent": {"type": "data_source_id", "data_source_id": RESULTS_DS_ID},
                  "properties": properties}, timeout=30)
        resp.raise_for_status()
        _leading_results_cache[row["종목코드"]] = resp.json()["id"]


def update_leading_summary(analysis_date, bench_return, n_leading, n_total):
    resp = requests.get(f"{BASE_URL}/blocks/{LEADING_PAGE_ID}/children", headers=HEADERS,
                         params={"page_size": 100}, timeout=30)
    resp.raise_for_status()
    summary_text = (
        f"🏆 분석일: {analysis_date}   |   기준: KOSPI200({bench_return:+.2f}%)   |   "
        f"주도주: {n_leading}개 선별 / 전체 {n_total}개 분석   |   주도주 조건: 4개 조건 모두 충족"
    )
    for block in resp.json()["results"]:
        btype = block.get("type")
        rich = block.get(btype, {}).get("rich_text", [])
        text = "".join(t.get("plain_text", "") for t in rich)
        if text.startswith("🏆 분석일"):
            requests.patch(
                f"{BASE_URL}/blocks/{block['id']}", headers=HEADERS,
                json={btype: {"rich_text": [{"type": "text", "text": {"content": summary_text}}]}},
                timeout=30,
            ).raise_for_status()
            return
    print("[WARN] 주도대장주 요약 블록을 찾지 못해 갱신을 건너뜁니다.")


def draw_leading_chart(leading_stocks, bench_series, analysis_date):
    os.makedirs("charts", exist_ok=True)
    plt.figure(figsize=(11, 6))
    plt.plot(bench_series.index, bench_series.values, linestyle="--", color="gray",
              label=f"KOSPI200 ({bench_series.iloc[-1]:+.1f}%)")
    for name, ticker, series, ret in leading_stocks:
        plt.plot(series.index, series.values, marker="o", markersize=3,
                  label=f"{name}({ticker}) ({ret:+.1f}%)")

    plt.title(f"국내 주도대장주 수익률 분석 ({analysis_date})")
    plt.ylabel("누적수익률 (%)")
    plt.gca().xaxis.set_major_locator(mdates.MonthLocator())
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    plt.xticks(rotation=45, ha="right")
    plt.axhline(0, color="lightgray", linewidth=0.8)
    plt.legend(fontsize=8, loc="upper left")
    plt.tight_layout()
    plt.savefig("charts/leading_stocks.png", dpi=150)
    plt.close()


def run_leading_stocks_analysis():
    if fdr is None:
        return
    leaders, wics_date = build_sector_leaders()
    analysis_date = f"{wics_date[:4]}-{wics_date[4:6]}-{wics_date[6:]}"
    start_date = (datetime.now(KST) - timedelta(days=LEAD_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    bench_data = analyze_leading_stock("KS200", start_date)
    if bench_data is None:
        print("[ERROR] KOSPI200 데이터를 가져오지 못해 주도대장주 분석을 중단합니다.")
        return
    bench_return = bench_data["6개월수익률"]
    bench_series = bench_data["series"]

    results = []
    leading_stocks = []
    for _, row in leaders.iterrows():
        stock_data = analyze_leading_stock(row["종목코드"], start_date)
        if stock_data is None:
            print(f"[WARN] {row['종목명']}({row['종목코드']}) 데이터 부족으로 건너뜁니다.")
            continue

        verdict, failed, high_ratio = judge_leading_conditions(stock_data, bench_return)
        relative_return = stock_data["6개월수익률"] - bench_return

        result_row = {
            "종목코드": row["종목코드"], "종목명": row["종목명"], "섹터": row["섹터"],
            "6개월수익률": stock_data["6개월수익률"], "상대수익률": relative_return,
            "52주고점비율": high_ratio, "판정": verdict,
            "미충족조건": ",".join(failed) if failed else "-",
        }
        results.append(result_row)

        if verdict == "주도주":
            leading_stocks.append((row["종목명"], row["종목코드"],
                                    stock_data["series"], stock_data["6개월수익률"]))

    results.sort(key=lambda r: r["상대수익률"], reverse=True)
    for row in results:
        upsert_leading_result(row)

    leading_stocks.sort(key=lambda x: x[3], reverse=True)
    draw_leading_chart(leading_stocks, bench_series, analysis_date)
    update_leading_summary(analysis_date, bench_return, len(leading_stocks), len(results))
    print(f"주도대장주 분석 완료: 전체 {len(results)}개 분석, 주도주 {len(leading_stocks)}개 선별")


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
        profit_rate = (total_profit / total_cost) if total_cost else 0
        update_summary_banner(total_valuation, total_profit, profit_rate)
        draw_chart()
        draw_category_chart()
    else:
        print("매매일지가 비어 있어 보유주식/총자산 갱신을 건너뜁니다.")

    run_index_analysis()

    try:
        run_leading_stocks_analysis()
    except Exception as e:
        print(f"[WARN] 주도대장주 분석 실패로 건너뜁니다: {e}")

    print("업데이트 완료")


if __name__ == "__main__":
    main()
