# Holdem

현재 운영 기준은 **Cloudflare Workers + Durable Objects + D1**이다.

- 사이트: https://holdem-cloudflare.loong-kid.workers.dev
- 레인지 트레이너: https://holdem-cloudflare.loong-kid.workers.dev/trainer/
- 서버 버전: `v38-cloudflare`, `/health`에서 확인.
- 기존 Render + Supabase 버전은 `../Holdem`의 `main`과
  `backup/render-supabase-v37-20261023`에 보존한다.
- Cloudflare 개발 브랜치: `cloudflare-migration`. `main`은 기존 Render 배포와
  연결되어 있으므로 Cloudflare 작업을 무심코 main에 합쳐 기존 서버를 바꾸지 않는다.
- 2026-10-23 승인한 디자인 시안을 입장·테이블·설정/복기 화면에 적용했다.
  정적 화면 리비전은 `39`; 게임 서버 버전은 `v38-cloudflare`를 유지한다.
  원본 시안은 `design-preview/`에 보존하며 운영 빌드에는 포함하지 않는다.
- 브랜치·커밋·배포의 차이는 [Git과 배포 안내](docs/git-and-deployments.md)를 참고한다.

## Cloudflare 개발·배포

필요 도구: Node.js, uv. 저장소에서 다음을 실행한다.

```powershell
npm.cmd install
uv sync --group test
py tools/cloudflare_manage.py dev
```

`dev`는 정적 자산·Python 패키지를 준비하고 로컬 D1 스키마를 적용한 뒤
http://127.0.0.1:8787 에서 실제 Cloudflare 런타임을 실행한다.
프로젝트에 고정한 uv/Workers 도구를 사용하므로 전역 uv를 바꾸지 않는다.

```powershell
npm.cmd exec wrangler -- login
py tools/cloudflare_manage.py deploy
```

`deploy`는 D1 마이그레이션 확인 후 Worker와 정적 파일을 함께 배포한다.
다른 계정에서는 먼저 D1을 생성하고 `wrangler.jsonc`의 DB ID를 바꿔야 한다.
인증 비밀값은 저장소에 넣지 않는다. Cloudflare 빌드는 원본 PDF·백업·개발 도구를 포함하지 않는다.

```powershell
uv run --group test python -m unittest discover -s tests -p "test_*.py"
uv run --group test python tests/cloudflare_integration.py
uv run --group test python tests/cloudflare_browser.py http://127.0.0.1:8787
uv run --group test python tests/theme_browser.py http://127.0.0.1:8787
```

브라우저 검증은 설치된 Chrome을 사용한다. 통합 검증은 별도 로컬 저장 경로에서
실제 workerd 재시작·D1 장애·영구 대기열 복구를 확인한다.
브라우저 검증은 지정한 주소의 고유한 테스트 방에서 6핸드를 만든다.
`theme_browser.py`는 실제 엔진이 만든 2·6·9인 상태를 브라우저에서만 렌더링한다.
8개 화면 폭과 종목/스트리트 144개 조합의 가로 넘침·좌석/보드 겹침을 검사하며,
게임방 연결이나 운영 DB 쓰기는 하지 않는다.

## 데이터와 복구

Supabase의 332핸드·7개 방을 전체 백업하고 D1에 이관했다(2026-10-23).
원본 식별 키와 이벤트 SHA256 및 운영 HTTP 조회 결과의 모든 메타데이터·이벤트를 대조했다.
원본 파일은 `../Holdem/backups/20261023-render-supabase-v37/holdem-all-hands.json`,
해시·건수는 같은 폴더의 `database-manifest.json`에 있다. 백업에는 개인 카드가 있으므로 공개하지 않는다.

```powershell
py tools/import_legacy.py <전체백업.json> --remote
```

가져오기는 원본 ID로 중복을 막고 이벤트 해시를 검증한다.
진행 중인 게임은 Durable Object에 저장하고, 완료 핸드는 영구 outbox를 거쳐 D1에 저장한다.
D1 장애 중에도 outbox를 유지해 재시도한다. 새 핸드 번호는 테이블 세션 ID와 함께 식별한다.
대용량 복기/GTO 연산은 게임방과 다른 Durable Object에서 처리한다.

기존 Render와 새 Cloudflare의 진행 중인 방·새 기록은 실시간 동기화하지 않는다.
전환 이후에는 새 Cloudflare 주소로 함께 접속한다. 기존 버전으로 돌아갈 때는 새 기록도 별도로 백업한다.
트레이너 점수는 기존과 같이 브라우저 localStorage이며 도메인이 달라지면 별도 기록으로 시작한다.

세부 설계는 [Cloudflare 이전 문서](docs/cloudflare-migration.md)를 참고한다.

---

아래는 기존 Render 버전의 구조·실행 참고다. 해당 서버의 원래 의존성은
`legacy/requirements-render.txt`에 남겨두었다.

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
