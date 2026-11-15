# 리포트 추천 미국주식 봇

데일리 리포트(아침·점심)가 추천한 미국 종목을 한국투자증권 API로 자동 매매합니다.

- 매수: 추천이 나오면 다음 미국장 시작 5분 뒤, 남은 달러로 살 수 있는 만큼(정수 주)
- 매도: +20% 익절 / -10% 손절 / 20거래일 경과 중 먼저 오는 것
- 한 번에 1종목, 주가 5~200달러만

## A9 (Termux) 설치

```bash
pkg install -y python git
pip install requests tzdata
git clone https://github.com/jr940923-dev/stock-bot.git
cd stock-bot
cp .env.example .env
nano .env        # 앱키·시크릿·계좌번호 입력
termux-wake-lock # 화면 꺼져도 계속 돌게
python bot.py
```

처음에는 `.env`의 `BOT_DRY_RUN=1`(주문 안 냄)로 하루 돌려 로그를 확인한 뒤 `0`으로 바꿉니다.

## 자주 쓰는 것

- 비상정지(새 매수 중단): `touch STOP` / 해제: `rm STOP`
- 기록 보기: `tail -n 30 bot.log`
- 보유·거래 내역: `cat state.json`
