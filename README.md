# Holdem

웹 멀티플레이 텍사스 홀덤 (No-Limit). Python FastAPI + WebSocket 백엔드, vanilla JS 프론트.
같은 방 이름으로 접속한 사람들이 한 테이블에서 같이 플레이한다.

## 구조

```
poker/
  cards.py       카드 & 덱
  evaluator.py   7장 -> 최강 5장 핸드 평가
  game.py        게임 엔진 (블라인드~쇼다운, 사이드팟). 네트워크와 무관한 순수 로직
server.py        FastAPI + WebSocket (방 관리 + 상태 브로드캐스트)
static/          index.html / style.css / app.js (포커 테이블 UI)
```

서버가 게임의 single source of truth. 각 브라우저는 상태를 받아 그리기만 한다.
본인 홀카드는 본인에게만 전송되어 상대가 볼 수 없다.

## Render 배포

1. 이 코드를 GitHub 저장소에 push.
2. https://render.com 로그인 -> **New +** -> **Web Service** -> GitHub 저장소 선택.
3. 설정:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn server:app --host 0.0.0.0 --port $PORT`
   - **Instance Type**: Free
4. **Create Web Service** -> 빌드 후 `https://<이름>.onrender.com` 주소 발급.
   누구나 이 주소로 접속해 같이 플레이.

(`render.yaml` 이 있어서 Blueprint 방식으로도 배포 가능.)

> 무료 티어는 일정 시간 미사용 시 잠들고(첫 접속 시 30~60초 콜드스타트),
> 게임 상태가 메모리에 있어 재시작되면 진행 중이던 테이블은 초기화된다. 초기 단계에선 충분.
