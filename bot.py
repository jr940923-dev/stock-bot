"""
리포트 추천 미국주식 자동매매봇 (한국투자증권 KIS Open API)

규칙 (2026-11-15 확정)
- 데일리 리포트가 picks.json 에 올린 미국 추천 종목을 다음 미국장 시작 5분 뒤 매수
- 한 번에 1종목만 보유, 보유 중 나온 추천은 건너뜀
- 매도: +20% 익절 / -10% 손절 / 20거래일 경과(마감 15분 전) 중 먼저 오는 것
- 주가 5~200달러 종목만, 남은 달러 안에서 살 수 있는 만큼(정수 주)

실행: python bot.py          (설정은 .env)
비상정지: 같은 폴더에 STOP 파일을 만들면 새 주문을 내지 않음
"""
import json, math, os, sys, time, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
import requests

BASE = Path(__file__).resolve().parent
ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
URL = "https://openapi.koreainvestment.com:9443"


def load_env():
    env = {}
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith(("KIS_", "BOT_"))})
    return env


CFG = load_env()
APPKEY = CFG.get("KIS_APPKEY", "")
APPSECRET = CFG.get("KIS_APPSECRET", "")
CANO = CFG.get("KIS_CANO", "")            # 계좌번호 앞 8자리
PRDT = CFG.get("KIS_PRDT", "01")          # 계좌상품코드 (보통 01)
PICKS_URL = CFG.get("BOT_PICKS_URL", "")
NTFY = CFG.get("BOT_NTFY_TOPIC", "")      # 비우면 알림 없이 로그만
DRY = CFG.get("BOT_DRY_RUN", "1") == "1"  # 1이면 주문을 내지 않고 로그만
MAX_BUDGET = float(CFG.get("BOT_MAX_BUDGET_USD", "1000"))

TP, SL, HOLD_DAYS = 0.20, 0.10, 20
PMIN, PMAX = 5.0, 200.0
PICK_MAX_AGE_DAYS = 4

HOLIDAYS = {  # NYSE 휴장일
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}  # 13:00 마감
PRICE_EX = ["NAS", "NYS", "AMS"]
ORDER_EX = {"NAS": "NASD", "NYS": "NYSE", "AMS": "AMEX"}

STATE_F = BASE / "state.json"
TOKEN_F = BASE / "token.json"
LOG_F = BASE / "bot.log"


# ---------- 기록·알림 ----------
def log(msg, push=False):
    line = f"[{dt.datetime.now(KST):%m-%d %H:%M}] {msg}"
    print(line, flush=True)
    with LOG_F.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    if push and NTFY:
        try:
            requests.post(f"https://ntfy.sh/{NTFY}", data=msg.encode("utf-8"), timeout=10)
        except Exception:
            pass


def load_state():
    if STATE_F.exists():
        return json.loads(STATE_F.read_text(encoding="utf-8"))
    return {"pos": None, "pending": None, "done_ids": [], "trades": []}


def save_state(s):
    tmp = STATE_F.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STATE_F)


# ---------- 시장 시간 ----------
def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def market_times(d):
    o = dt.datetime.combine(d, dt.time(9, 30), ET)
    c = dt.datetime.combine(d, dt.time(13, 0) if d.isoformat() in EARLY_CLOSE else dt.time(16, 0), ET)
    return o, c


def trading_days_between(d0, d1):
    """d0 다음 날부터 d1까지(포함) 거래일 수"""
    n, d = 0, d0
    while d < d1:
        d += dt.timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


# ---------- KIS API ----------
def token():
    if TOKEN_F.exists():
        t = json.loads(TOKEN_F.read_text())
        if time.time() < t["exp"] - 600:
            return t["tok"]
    r = requests.post(URL + "/oauth2/tokenP", json={
        "grant_type": "client_credentials", "appkey": APPKEY, "appsecret": APPSECRET}, timeout=15)
    j = r.json()
    if "access_token" not in j:
        raise RuntimeError(f"토큰 발급 실패: {j}")
    TOKEN_F.write_text(json.dumps({"tok": j["access_token"], "exp": time.time() + int(j.get("expires_in", 86400))}))
    return j["access_token"]


def headers(tr):
    return {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {token()}",
            "appkey": APPKEY, "appsecret": APPSECRET, "tr_id": tr, "custtype": "P"}


def api_get(path, tr, params):
    r = requests.get(URL + path, headers=headers(tr), params=params, timeout=15)
    j = r.json()
    if j.get("rt_cd") not in ("0", None):
        raise RuntimeError(f"{tr} 오류: {j.get('msg1')}")
    return j


def api_post(path, tr, body):
    r = requests.post(URL + path, headers=headers(tr), data=json.dumps(body), timeout=15)
    j = r.json()
    if j.get("rt_cd") != "0":
        raise RuntimeError(f"{tr} 주문 오류: {j.get('msg1')}")
    return j


def price(ticker, ex=None):
    """현재가와 거래소 코드. 거래소를 모르면 세 곳을 차례로 시도"""
    for e in ([ex] if ex else PRICE_EX):
        j = api_get("/uapi/overseas-price/v1/quotations/price", "HHDFS00000300",
                    {"AUTH": "", "EXCD": e, "SYMB": ticker})
        last = (j.get("output") or {}).get("last") or ""
        if last and float(last) > 0:
            return float(last), e
    raise RuntimeError(f"{ticker} 시세를 찾지 못함")


def buying_power(ex, ticker, px):
    j = api_get("/uapi/overseas-stock/v1/trading/inquire-psamount", "TTTS3007R",
                {"CANO": CANO, "ACNT_PRDT_CD": PRDT, "OVRS_EXCG_CD": ORDER_EX[ex],
                 "OVRS_ORD_UNPR": f"{px:.2f}", "ITEM_CD": ticker})
    o = j.get("output") or {}
    return float(o.get("ovrs_ord_psbl_amt") or o.get("ord_psbl_frcr_amt") or 0)


def holding(ticker):
    """보유 수량과 평균 매입가 (미국 3개 거래소 모두 조회)"""
    for ex in ("NASD", "NYSE", "AMEX"):
        j = api_get("/uapi/overseas-stock/v1/trading/inquire-balance", "TTTS3012R",
                    {"CANO": CANO, "ACNT_PRDT_CD": PRDT, "OVRS_EXCG_CD": ex, "TR_CRCY_CD": "USD",
                     "CTX_AREA_FK200": "", "CTX_AREA_NK200": ""})
        for row in j.get("output1") or []:
            if row.get("ovrs_pdno") == ticker and float(row.get("ovrs_cblc_qty") or 0) > 0:
                return int(float(row["ovrs_cblc_qty"])), float(row.get("pchs_avg_pric") or 0)
    return 0, 0.0


def order(side, ticker, ex, qty, px):
    if DRY:
        log(f"[모의] {side} {ticker} {qty}주 @ {px:.2f}")
        return "DRY"
    tr = "TTTT1002U" if side == "buy" else "TTTT1006U"
    j = api_post("/uapi/overseas-stock/v1/trading/order", tr, {
        "CANO": CANO, "ACNT_PRDT_CD": PRDT, "OVRS_EXCG_CD": ORDER_EX[ex], "PDNO": ticker,
        "ORD_QTY": str(qty), "OVRS_ORD_UNPR": f"{px:.2f}", "CTAC_TLNO": "", "MGCO_APTM_ODNO": "",
        "SLL_TYPE": "" if side == "buy" else "00", "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00"})
    return (j.get("output") or {}).get("ODNO", "")


def cancel(ticker, ex, odno, qty):
    if DRY or not odno:
        return
    api_post("/uapi/overseas-stock/v1/trading/order-rvsecncl", "TTTT1004U", {
        "CANO": CANO, "ACNT_PRDT_CD": PRDT, "OVRS_EXCG_CD": ORDER_EX[ex], "PDNO": ticker,
        "ORGN_ODNO": odno, "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(qty), "OVRS_ORD_UNPR": "0",
        "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0"})


# ---------- 추천 종목 ----------
def fetch_picks():
    r = requests.get(PICKS_URL, params={"t": int(time.time())}, timeout=15)
    return r.json().get("picks", [])


def next_pick(state, today):
    picks = fetch_picks()
    fresh = []
    for p in picks:
        pid = p.get("id") or f"{p['date']}-{p['ticker']}"
        if pid in state["done_ids"]:
            continue
        age = (today - dt.date.fromisoformat(p["date"])).days
        if age > PICK_MAX_AGE_DAYS:
            state["done_ids"].append(pid)   # 너무 오래된 추천은 버림
            continue
        fresh.append((p["date"], pid, p["ticker"].upper().strip()))
    fresh.sort(reverse=True)
    return fresh[0] if fresh else None


# ---------- 한 번의 점검 ----------
def step(state, now):
    today = now.date()
    if not is_trading_day(today):
        return
    o, c = market_times(today)
    if not (o <= now < c):
        return

    # 1) 주문 체결 확인
    pend = state["pending"]
    if pend:
        qty, avg = (pend["qty"], pend["px"]) if DRY else holding(pend["ticker"])
        if pend["side"] == "buy" and qty > 0:
            state["pos"] = {"ticker": pend["ticker"], "ex": pend["ex"], "qty": qty, "entry": avg,
                            "entry_date": today.isoformat()}
            state["pending"] = None
            log(f"✅ 매수 체결: {pend['ticker']} {qty}주, 평균 ${avg:.2f}. "
                f"익절 ${avg*(1+TP):.2f} / 손절 ${avg*(1-SL):.2f} / {HOLD_DAYS}거래일 뒤 매도", push=True)
        elif pend["side"] == "sell" and (DRY or qty == 0):
            pos = state["pos"]
            ret = pend["px"] / pos["entry"] - 1
            pnl = (pend["px"] - pos["entry"]) * pos["qty"]
            state["trades"].append({**pos, "exit": pend["px"], "exit_date": today.isoformat(),
                                    "reason": pend["reason"], "ret": round(ret, 4)})
            state["pos"], state["pending"] = None, None
            log(f"💰 매도 체결({pend['reason']}): {pos['ticker']} {ret*100:+.1f}%, "
                f"약 {pnl:+.2f}달러 (수수료 전)", push=True)
        elif now.timestamp() - pend["t"] > 30 * 60:
            cancel(pend["ticker"], pend["ex"], pend.get("odno"), pend["qty"])
            log(f"⚠️ 30분 미체결로 주문 취소: {pend['side']} {pend['ticker']}", push=True)
            state["pending"] = None
        return

    stop = (BASE / "STOP").exists()

    # 2) 보유 중이면 매도 조건 확인
    pos = state["pos"]
    if pos:
        px, _ = price(pos["ticker"], pos["ex"])
        held = trading_days_between(dt.date.fromisoformat(pos["entry_date"]), today)
        reason = None
        if px >= pos["entry"] * (1 + TP):
            reason = "익절 +20%"
        elif px <= pos["entry"] * (1 - SL):
            reason = "손절 -10%"
        elif held >= HOLD_DAYS and now >= c - dt.timedelta(minutes=15):
            reason = f"{HOLD_DAYS}거래일 경과"
        if reason:
            lim = round(px * 0.995, 2)
            odno = order("sell", pos["ticker"], pos["ex"], pos["qty"], lim)
            state["pending"] = {"side": "sell", "ticker": pos["ticker"], "ex": pos["ex"], "qty": pos["qty"],
                                "px": lim, "odno": odno, "t": now.timestamp(), "reason": reason}
            log(f"매도 주문({reason}): {pos['ticker']} {pos['qty']}주 @ ${lim}")
        return

    # 3) 빈손이면 장 시작 5분 뒤부터 새 추천 매수
    if stop or now < o + dt.timedelta(minutes=5):
        return
    pk = next_pick(state, now.astimezone(KST).date())
    if not pk:
        return
    _, pid, tk = pk
    state["done_ids"].append(pid)
    px, ex = price(tk)
    if not (PMIN <= px <= PMAX):
        log(f"건너뜀: {tk} 가격 ${px} (5~200달러 범위 밖)", push=True)
        return
    lim = round(px * 1.005, 2)
    cash = MAX_BUDGET if DRY else min(MAX_BUDGET, buying_power(ex, tk, lim))
    qty = math.floor(cash * 0.995 / lim)
    if qty < 1:
        log(f"건너뜀: {tk} ${lim}, 주문 가능 금액 ${cash:.2f} 부족", push=True)
        return
    odno = order("buy", tk, ex, qty, lim)
    state["pending"] = {"side": "buy", "ticker": tk, "ex": ex, "qty": qty, "px": lim, "odno": odno,
                        "t": now.timestamp()}
    log(f"매수 주문: {tk} {qty}주 @ ${lim} (추천 {pid})", push=True)


def main():
    missing = [k for k, v in {"KIS_APPKEY": APPKEY, "KIS_APPSECRET": APPSECRET, "KIS_CANO": CANO,
                               "BOT_PICKS_URL": PICKS_URL}.items() if not v]
    if missing:
        sys.exit(f".env에 빠진 값: {', '.join(missing)}")
    log(f"봇 시작 ({'모의 실행: 주문 안 냄' if DRY else '실전'})", push=True)
    while True:
        state = load_state()
        now = dt.datetime.now(ET)
        try:
            step(state, now)
        except Exception as e:
            msg = f"❌ 오류: {e}"
            log(msg, push=(msg != state.get("last_err")))   # 같은 오류는 한 번만 알림
            state["last_err"] = msg
        else:
            state["last_err"] = None
        save_state(state)
        o, c = market_times(now.date())
        time.sleep(60 if is_trading_day(now.date()) and o - dt.timedelta(minutes=10) <= now < c else 300)


if __name__ == "__main__":
    main()
