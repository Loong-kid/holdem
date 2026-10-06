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

## 레인지 트레이너

입장 화면의 **레인지 트레이너**, 게임 상단의 **레인지 연습** 버튼으로 `/trainer/`를 새 탭에서 연다. 방 입장이나 암호 없이 사용할 수 있다. 원래 홀덤 탭의 WebSocket은 유지된다.

- 토너먼트 프리플랍 8단계, 174개 차트 출제 + 전체 177개 차트 라이브러리.
- 오픈·푸시/폴드·오픈 대응·리잼/리잼콜·헤즈업·블라인드 배틀·BB 디펜스·공격적 블라인드 상대 BTN 전략.
- 기존 경계 핸드 가중 출제와 오답 복습, 후속 3벳/4벳/림프 대응, 칩 EV 해설을 그대로 사용.
- 학습 기록은 사이트를 연 브라우저의 localStorage에 개인별로 저장된다. 계정·기기 간 동기화는 없다. 로컬 파일(`file://`)에서 쌓은 기록은 웹사이트 저장소와 별개다.

원본 앱은 `../range-trainer`, 배포용 파일은 `trainer/`. 원본 수정 후 `py sync_trainer.py`로 동기화하고 `py sync_trainer.py --check`로 확인한다. 배포 저장소에 실행 파일·차트 데이터·승률표가 포함되어, Render는 형제 프로젝트 없이 이 저장소만으로 실행한다. 원본 PDF·핸드 히스토리·로컬 테스트 스크린샷은 묶지 않는다.

검증: `python -m unittest discover -s tests -p "test_*.py"`; Playwright가 설치된 Python으로 `python tests/trainer_browser.py`(Chrome, 데스크톱/폰). 테스트 서버용 Python은 `SERVER_PYTHON`으로 지정 가능하며 기본은 `.venv/Scripts/python.exe`이다.

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
