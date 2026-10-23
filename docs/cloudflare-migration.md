# Cloudflare 이전 설계와 작업 순서

작성: 2026-10-23. 기준 코드: `a2a3e30` / `v37-trainer-wheel`.

목표는 기존 Render + Supabase 서비스를 복구 가능한 기준으로 유지하면서,
Cloudflare 버전을 별도 주소에서 검증하고 장기 운영 기준으로 전환하는 것이다.
**2026-10-23 구현·배포 완료:** https://holdem-cloudflare.loong-kid.workers.dev
게임방 명령/시계 분리, Python Workers/DO, D1, 상태 복원, 전체 332핸드 이관을 완료했다.
기존 운영은 그대로 보존한다. 아래 단계별 설계와 실제 구현 차이는 현재 상태 절을 참고한다.

## 보존한 기준

| 항목 | 위치 / 상태 |
|---|---|
| 기존 운영 작업 폴더 | `../Holdem`, `main`, 운영 변경 없음 |
| 기준 브랜치 | `backup/render-supabase-v37-20261023` → `a2a3e30` |
| 새 작업 폴더 | `../Holdem-cloudflare`, `cloudflare-migration` |
| 코드 독립 백업 | 기존 폴더의 `backups/20261023-render-supabase-v37/holdem-v37.bundle` |
| 백업 검증 | `git bundle verify` 통과, 전체 Git 이력 포함 |
| 기존 데이터베이스 | Supabase 유지. 별도 `holdem-all-hands.json` 전체 백업 및 D1 이관 완료 |

Git worktree는 작업 파일을 분리하지만 같은 Git 저장소를 공유한다.
bundle은 Git 저장소와 별도로 복원 가능한 파일 백업이다.
기존 untracked `sound_preview.html`은 원래 폴더에 그대로 있다.
기존 서버의 `/export?room=`가 전체 방을 내보내는 것을 코드로 확인하고,
2026-10-23 전체 332핸드를 저장·재조회 건수 대조했다.
`database-manifest.json` SHA256: c0a0b548e76ce859731ea1487ca0bbdddb96aef4d290ac45ed37a67764e84450.
전체 JSON은 1,731,029바이트이며 개인 카드를 포함하므로 Git·정적 사이트에서 제외한다.

복구 예: `git clone ../Holdem/backups/20261023-render-supabase-v37/holdem-v37.bundle Holdem-restored`

## 목표 구조

```text
브라우저: 기존 화면·레인지 트레이너·게임 메시지 형식
   │ HTTP / WebSocket
   ▼
Cloudflare Worker: 정적 자산, 요청 검증, 방 ID별 라우팅
   │
   ▼
PokerRoom Durable Object: 방 하나의 상태를 판정하는 단일 주체
   ├─ 기존 Python poker/ 게임 규칙 + 족보 판정
   ├─ 영구 상태: 세션·좌석·칩·현재 핸드·재접속·타이머
   ├─ WebSocket Hibernation + Alarms
   └─ 완료 핸드 outbox ──▶ D1: 복기·통계·기존 Supabase 기록
```

- Python을 우선 검증한다. Cloudflare는 Python Workers/FastAPI/Durable Objects를
  지원하지만 기존 `uvicorn` 서버를 그대로 실행하는 환경은 아니다.
- 게임 규칙은 `poker/game.py`, 족보는 `poker/evaluator.py`, 덱은 `poker/cards.py`를 재사용한다.
- 기존 `static/`, `trainer/`는 Static Assets로 제공한다.
- Supabase 드라이버·스레드 풀은 새 런타임에서 분리한다.
- D1은 완료 핸드의 조회 저장소로 사용한다. 실시간 베팅 판정에 네트워크 DB 조회를 넣지 않는다.
- 전역 Cloudflare KV를 실시간 방 상태의 원본으로 사용하지 않는다.

## 가장 먼저 바꿀 부분

### 1. 진행 중인 게임을 직렬화·복원하기 — 기반 구현 완료

`poker/snapshots.py`: `snapshot_game(game)` / `restore_game(data)`.

- 버전 1 JSON 스키마. 저장된 덱을 다시 섞지 않고 남은 카드 순서 그대로 복구.
- 플레이어의 개인 카드, 스택, 현재 라운드 베팅, 전체 기여액, 행동 상태,
  보드, 차례, 최소 레이즈, 리플레이 기록, 올인 런아웃 상태 보존.
- 객체 참조·소켓·lock·task는 저장하지 않음.
- 손상 데이터와 알 수 없는 버전은 오류 처리. 빈 테이블로 몰래 초기화하지 않음.
- 새 엔진 필드가 추가되면 누락을 감지해 스키마 갱신을 요구.
- 저장본은 다른 사람의 카드와 남은 덱을 포함하므로 서버 전용이다.
  클라이언트에는 기존 `public_state`와 해당 사용자의 `private_state`만 보낸다.

이 모듈은 Room 스냅샷을 통해 Durable Object의 저장·복구 경로에 연결되어 있다.
`tests/test_snapshots.py`는 3개 게임 종류 × 2개 베팅 방식 × 2개 진행 전략에서
매 행동과 런아웃마다 JSON 저장/새 객체 복원 후 결과를 대조한다.

### 2. `server.py`의 Room을 실행 환경과 분리하기 — 구현 완료

현재 Room은 게임 규칙 호출, 권한, WebSocket, DB, 타이머를 함께 담당한다.
이를 다음 경계로 나눈다.

| 책임 | 호스팅과 무관한 코드 | 실행 환경별 어댑터 |
|---|---|---|
| 방 상태 | 좌석, ledger, 세션 소유권, 채팅, 게임 상태 | 없음 |
| 행동 처리 | `handle(connection, message, now)` | Durable Object가 호출 |
| 응답 | 받는 세션별 메시지와 이벤트 | WebSocket 전송 |
| 시계 | `advance(now)`, `next_deadline()` | Cloudflare alarm |
| 기록 | 완료 핸드와 outbox 생성 | PostgreSQL / D1 |

기존 메시지 `join`, `sit`, `stand`, `start`, `pause`, `action`, `chat`, `leave`,
`ping`, `list_replays`, `get_replay`, `sit_out`, `rebuy`, `set_allow_same_ip`,
`set_timeout`, `set_blinds`, `set_tournament`, `set_default_stack`, `set_variant`,
`adjust_stack`을 기능 목록으로 삼아 빠진 동작 없이 옮긴다.
개인 카드 노출 방지와 방장 권한 확인도 이 계층에서 유지한다.

Room 저장본에는 엔진 외에도 다음 데이터가 필요하다.

- `room_id`, 테이블 생성 때 발급한 `session_id`, 상태 `revision`, 스키마 버전.
- 호스트 ID, 좌석·브라우저 토큰 소유권, ledger, 같은 IP 정책, 최근 채팅.
- 자동 진행 여부, 액션 제한 시간, 일시정지 잔여 시간.
- 액션/다음 핸드/런아웃/재접속/토너먼트의 UTC Unix 마감 시각(초).
- 완료 핸드 저장 대기열과 재시도 정보.

기존 monotonic 시각은 재실행 뒤 사용할 수 없으므로 저장하지 않는다.
현재 0.25초 반복 확인은 가장 이른 마감 시각 하나를 예약하는 방식으로 대체한다.
지연되거나 중복 호출된 alarm도 현재 상태와 마감 시각을 확인해 한 번만 진행시킨다.
재접속은 브라우저 토큰으로 소유권을 확인하고 닉네임만으로 좌석을 넘기지 않는다.

### 3. 방 하나의 Durable Object 검증

- Worker는 방 ID를 결정하고 해당 Object로 WebSocket을 전달한다.
  기존 `/ws`는 첫 `join` 메시지에 방 ID가 있으므로, 새 클라이언트는
  연결 URL에도 방을 넣도록 한다. 메시지의 방과 라우팅된 방이 다르면 거부한다.
- Hibernation API로 연결을 받고, 연결별 최소 식별 정보만 attachment에 둔다.
- 작업 중 재진입이 가능한 await 경계에서도 명령이 순서대로 처리되도록 한다.
- 명령 결과를 저장한 뒤 응답·전파한다. 중복 명령은 command ID로 구분한다.
- 소켓이 계속 살아 있는 절전 복귀와, 끊긴 소켓의 재접속은 별개로 시험한다.
- 기존 화면에서 2인/9인, 관전자, 모바일 재접속, 중복 소켓 정리까지 검증한다.
- 스냅샷이 없을 때의 새 방과, 저장 실패·손상으로 읽지 못하는 방을 구분한다.

### 4. 기록 조회·데이터 이전

`db.py` 직접 호출을 저장 인터페이스로 바꾸고 PostgreSQL/D1 구현을 나눈다.
완료 핸드는 `(session_id, hand_number)` 고유 키로 중복 저장을 막는다.
핸드 번호만 쓰면 방 재생성 후 기존 기록과 충돌한다.

DO 상태 저장과 D1 쓰기는 하나의 원자적 트랜잭션으로 가정하지 않는다.
완료 핸드를 DO에 outbox로 함께 기록하고, D1에 idempotent하게 반영한 뒤
대기열에서 제거한다. D1 장애 시 게임은 계속되고 기록 전송은 재시도한다.

기존 Supabase 핸드는 별도 원본 ID(`supabase:<id>`)로 가져와 재실행해도
중복되지 않게 한다. 방별 건수·전체 건수·이벤트 내용 해시를 대조한다.
`/export`, `/stats`, `/gto`, `/gto_charts`, 리플레이 조회를 함께 이전한다.
GTO의 동기 파일 읽기/`asyncio.to_thread`는 Worker 배포 방식에 맞춰 점검한다.
과거 복기 기록만으로 당시의 진행 중 테이블이나 사용자 잔고를 복구할 수는 없다.

### 5. 검증 주소 → 주 운영 전환

1. 기존 사이트를 유지한 채 별도 Cloudflare 검증 주소를 배포.
2. 핸드 판정/칩 합계/오마하·더블보드/팟리밋/토너먼트 회귀 검증.
3. 절전·재실행·재접속·중복 명령·지연/중복 alarm·D1 쓰기 실패 검증.
4. 실제 사용량과 응답 시간 측정. 무료 한도 내 동작 검토.
5. 게임이 진행 중이지 않은 전환 시점에 전체 DB 백업과 최종 차이분 이관.
6. 기본 접속 주소를 Cloudflare로 변경. Render + Supabase 복구 경로 보존.

양쪽 서버에서 같은 방을 동시에 플레이하게 연결하지 않는다.
Cloudflare 전환 후 새로 생긴 기록은 자동으로 기존 Supabase에 생기지 않는다.
되돌릴 때는 새 기록의 역이관과 테이블 상태 호환성도 확인해야 한다.
따라서 이번 Git 기준점은 코드 복구본이며 두 운영 환경의 실시간 동기화를 뜻하지 않는다.

레인지 트레이너의 `localStorage`는 도메인별로 분리된다.
새 주소로 전환할 때 학습 기록 내보내기/가져오기를 제공한다.
사용자 계정 기반 기록 동기화는 서버 이전과 별도 기능으로 둔다.

## 현재 구현 상태

- `poker/room.py`: 기존 게임 명령·권한·좌석·ledger·채팅·토너먼트·재접속을 추출.
  소켓에는 응답을 바로 쓰지 않고 큐에 쌓은 뒤 런타임에서 저장 완료 후 전파한다.
- 마감 시각은 UTC Unix **초**로 저장하고, Cloudflare alarm 호출 때 밀리초로 변환한다.
  실행 환경의 monotonic clock이나 반복 ticker에 의존하지 않는다.
- `cloudflare/entry.py`: Worker 라우팅, WebSocket Hibernation, 방별 DO.
  DO storage 트랜잭션에 스냅샷과 다음 alarm을 함께 저장한다.
  게임방과 별도 `analysis:` DO에서 export/GTO를 처리해 무거운 연산을 분리한다.
- `cloudflare/repository.py` + SQL migration: D1 복기 저장·목록·내보내기.
  D1 장애 시 DO outbox를 보존하고, 재시도 때 동일 키/이벤트 해시로 중복을 막는다.
- 기존 Supabase 원본 332개 고유 ID와 이벤트 해시 검증 통과.
  새 사이트의 7개 방 HTTP export에서 전체 메타데이터·이벤트도 원본과 일치.
- `tools/cloudflare_manage.py`: 빌드, 로컬 D1 준비, 개발 서버, 원격 배포.
  `tools/import_legacy.py`: 개인 백업 변환·이관·해시 검증.
- 실제 로컬 Cloudflare 런타임 재시작 후 좌석/카드/팟/일시정지 시계 유지,
  중복 명령, D1 복기, 방 분리, GTO, D1 장애 중 저장→재시작→복구 검증.
- 단위/기존 회귀 16개, 9좌석 6모드, 엔진 매행동 저장/복구 검증.

## 현재 검증 방법

```powershell
uv run --group test python -m unittest discover -s tests -p "test_*.py"
uv run --group test python tests/cloudflare_integration.py
uv run --group test python tests/cloudflare_browser.py http://127.0.0.1:8787
```

브라우저 검증은 지정 주소에 별도 테스트 방과 핸드를 생성한다.

## 확인한 공식 자료

- Python Workers: https://developers.cloudflare.com/workers/languages/python/
- Durable Object API (Python 포함): https://developers.cloudflare.com/durable-objects/api/base/
- Hibernation WebSocket: https://developers.cloudflare.com/durable-objects/best-practices/websockets/
- Alarms: https://developers.cloudflare.com/durable-objects/api/alarms/

Python 엔진·GTO·세션 처리의 실제 Cloudflare 실행 및 배포를 확인했다.
