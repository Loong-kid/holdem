# 홀덤의 Git 브랜치와 배포

현재 GitHub 저장소는 `Loong-kid/holdem` 하나이며, 운영 방식이 다른 두 버전을 브랜치로 분리했다.

| 항목 | 기존 버전 | Cloudflare 버전 |
|---|---|---|
| 로컬 작업 폴더 | `Holdem` | `Holdem-cloudflare` |
| 브랜치 | `main` | `cloudflare-migration` |
| 서버/프론트 제공 | Render | Cloudflare Workers / Static Assets |
| 진행 중인 방 | 기존 Python 서버 메모리 | Durable Objects |
| 완료 핸드 DB | Supabase PostgreSQL | D1 |
| 기준 | `a2a3e30` / v37 | 이관 코드와 승인한 디자인 변경 |

`backup/render-supabase-v37-20261023`도 `a2a3e30`을 가리키도록 보존했다.
두 작업 폴더는 Git worktree여서 Git 이력/객체는 공유하지만 현재 체크아웃한 파일은 별도다.

## 수정이 기록되고 사이트에 나타나는 순서

1. **파일 수정:** 내 컴퓨터에서만 바뀐다. 이 상태로는 GitHub 이력에 없다.
2. **커밋:** 선택한 파일 상태를 로컬 Git에 하나의 저장 지점으로 남긴다. 커밋 ID와 변경 내용을 비교할 수 있다.
3. **푸시:** 커밋을 GitHub의 해당 브랜치에 올린다. 다른 브랜치에는 자동으로 합쳐지지 않는다.
4. **배포:** 정한 코드 상태를 서버에 올린다. GitHub에 푸시한 것과 실제 사이트에 적용된 것은 다른 사건이다.

브랜치는 고정된 옛 버전/새 버전 보관함이 아니라, 커밋 이력의 어느 지점을 현재 가리키는 이름이다.
Cloudflare 브랜치에 새 커밋을 만들면 그 브랜치의 위치가 앞으로 움직인다. `main`은 기존 위치에 남는다.
`main`도 나중에 직접 수정하거나 다른 브랜치를 병합하면 바뀔 수 있다.

## 이 프로젝트의 배포 방식

기존 Render는 `main`을 사용하는 기존 배포용으로 보존한다. Cloudflare 작업을 `main`에 병합하지 않는다.

현재 Cloudflare는 GitHub 푸시만으로 자동 배포하는 설정을 추가한 것이 아니다.
`Holdem-cloudflare`에서 검증한 뒤 `tools/cloudflare_manage.py deploy`로 배포한다.
이 명령은 **현재 로컬 폴더의 파일**을 묶어 배포하므로, 브랜치 이름만으로 배포 대상을 자동 판단하지 않는다.
Cloudflare 배포 전 현재 브랜치, 변경 파일, 검증 결과를 확인하고 커밋/푸시한 상태와 맞춘다.

`wrangler.jsonc`가 Worker 이름과 Durable Object/D1 연결을 정하며,
`tools/build_cloudflare.py`가 운영에 필요한 `static/`, `trainer/`, Python 파일만 포함한다.
시안과 백업 파일은 배포하지 않는다.

## 과거 버전 복구와 DB의 차이

Git에 커밋하고 GitHub에 올린 코드는 과거 버전과 비교하고 다시 배포할 수 있다.
한 번 커밋한 코드도 이후 강제 이력 변경이나 저장소 삭제로 보존이 훼손될 수 있으므로 별도 Git bundle 백업도 유지한다.

게임 기록, 진행 중인 방, 비밀키, 브라우저의 트레이너 점수는 Git 커밋에 들어 있지 않다.
코드를 되돌려도 DB 내용이 당시 시점으로 되돌아가지는 않는다. 데이터 복구는 별도 DB 백업으로 처리한다.
기존 Supabase와 D1의 새 기록도 자동 동기화하지 않는다.

Supabase에 프론트엔드 코드를 배포하는 구조는 아니다. 기존 화면/게임 코드는 Render에서 실행하며 Supabase는 DB를 담당한다.
Cloudflare는 서버와 정적 화면을 제공하고 D1이 완료 핸드를 저장한다.
