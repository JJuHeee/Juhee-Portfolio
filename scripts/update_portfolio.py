"""
SWING Portfolio 자동 업데이트 스크립트

흐름: 매매일지(Trade Log) 조회 -> 보유주식(Holdings) 갱신 -> 총자산(Total Assets)에
오늘자 스냅샷 1행 추가 -> 총자산 추이 차트 생성

원칙: 기존 행/블록은 절대 삭제하지 않습니다. 보유주식은 티커가 같으면 업데이트(patch),
처음 보는 티커면 새 행을 추가(create)합니다. 총자산은 매 실행마다 새 행을 "추가"하여
히스토리를 쌓습니다 (과거 행 수정/삭제 없음).

실행 환경: GitHub Actions (스케줄 + 수동 실행)
필요 환경변수: NOTION_TOKEN (Notion Internal Integration Token, GitHub Secrets에 등록)
"""

import os
import requests
from datetime import datetime, timezone, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

try:
    import FinanceDataReader as fdr
except ImportError:
    fdr = None

# ── 고정 설정값 (데이터소스 ID는 비밀값이 아니므로 코드에 그대로 둬도 안전합니다) ──
TRADE_LOG_DS_ID = "9a0c7b65-17c2-4042-973d-075ed4421ba0"     # 매매일지
HOLDINGS_DS_ID = "89b0cb64-c32a-4646-a336-72af409d81f5"      # 보유주식
TOTAL_ASSETS_DS_ID = "14fc5390-f230-4332-8682-740ab1548b86"  # 총자산

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
    """KRX 종가 기준 현재가를 가져옵니다. 실패 시 None."""
    if fdr is None:
        return None
    try:
        df = fdr.DataReader(ticker)
        return float(df["Close"].iloc[-1])
    except Exception as e:
        print(f"[WARN] {ticker} 시세 조회 실패: {e}")
        return None


def find_holding_page(ticker, cache):
    if cache is None:
        cache = {get_rich_text(p["properties"]["티커"]): p["id"]
                 for p in query_data_source(HOLDINGS_DS_ID)}
    return cache.get(ticker), cache


_holdings_cache = None


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


# ───────────────────────── 4) 차트 생성 (GitHub에 직접 저장) ─────────────────────────

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


# ───────────────────────── 메인 실행 ─────────────────────────

def main():
    trades = fetch_trade_log()
    if not trades:
        print("매매일지가 비어 있습니다. 종료합니다.")
        return

    book = compute_holdings(trades)

    total_valuation = total_profit = total_cost = 0.0
    for holding in book.values():
        result = upsert_holding(holding)  # 수량 0이 되어도 행은 삭제하지 않고 갱신만 합니다
        if holding["보유수량"] > 0:
            total_valuation += result["valuation"]
            total_profit += result["profit"]
            total_cost += result["cost"]

    append_total_assets_snapshot(total_valuation, total_profit, total_cost)
    draw_chart()
    print("업데이트 완료")


if __name__ == "__main__":
    main()
