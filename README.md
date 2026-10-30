# YouthLink 데이터 파이프라인

온통청년 API의 청년 지원 정책을 수집·정제하여 PostgreSQL에 저장하는 YouthLink의 데이터 파이프라인입니다. 정책 검색과 맞춤형 추천에 필요한 데이터를 준비하고, 이후 pgvector 기반 RAG와 챗봇으로 연결하는 것을 목표로 합니다.

정책 ETL은 `policy`, `policy_application_period`, `policy_region`, `policy_eligibility_code` 4개 테이블을 적재합니다. 지역 ETL은 VWorld의 시도·시군구 정보를 `region`에 동기화하며, 지역 View를 통해 과거·상위 코드와 알려진 범위 누락을 현재 코드로 해석합니다. `common_code`는 SQL seed로 채웁니다. 정책 청크 생성, OpenAI 임베딩 적재, pgvector cosine 검색 파이프라인이 구현되어 있습니다.

> 작성 기준: 2026-10-30에 정리한 프로젝트 구현 상태. 아래의 구현 현황은 코드 기준이며, 실제 운영 DB 상태는 각 환경에서 검증해야 합니다.

## 문서

- [데이터베이스 ERD](docs/erd.md)
- [테이블 및 컬럼 데이터 사전](docs/data-dictionary.md)
- [RAG 정책 Chunk 생성 전략](docs/rag-chunk-strategy.md)
- [임베딩 및 pgvector 검색 파이프라인](docs/embedding-pipeline.md)
- [검증형 정책 원문 수집 파이프라인](docs/web-source-pipeline.md)

## 1. 처음 참여한 팀원의 진행 순서

1. 저장소를 내려받고 Python 가상환경과 의존성을 설치합니다.
2. PostgreSQL과 pgvector를 준비하고 DB를 생성합니다.
3. 확장 → 스키마 → 공통 코드 seed 순서로 기본 SQL을 실행합니다.
4. `.env.example`을 `.env`로 복사하여 로컬 설정을 입력합니다.
5. 마이그레이션과 VWorld 적재를 3절의 순서대로 완료합니다.
6. 샘플 JSON으로 적재를 확인한 뒤 실제 API 수집을 실행합니다.
7. 검증 SQL과 실패 로그를 확인합니다.
8. 005 마이그레이션을 적용한 뒤 정책 청크를 dry-run하고 적재합니다.
9. 임베딩 계획의 청크·토큰 수를 확인하고 소량 API 테스트 후 전체를 적재합니다.
10. 질문 벡터를 생성하여 pgvector 검색 결과를 검증합니다.
11. 009~012 마이그레이션 뒤 정책 URL을 dry-run으로 점검하고 `PENDING` 연결을 동기화합니다.
12. 원문 수집은 특정 도메인·소량 파일럿으로 시작하고 검증 View만 소비합니다.

모든 명령은 **저장소 루트**에서 실행합니다. 처음에는 팀 공용 운영 DB 대신 개발용 DB를 사용하세요. 샘플 실행도 DB에 실제로 데이터를 저장합니다.

## 2. 프로젝트 구조

```text
youthlink-data-pipeline/
├── db/
│   ├── extensions.sql          # vector, pgcrypto 확장 활성화
│   ├── schema.sql              # 기본 16개 테이블·웹 검증 View·인덱스
│   ├── migrations/             # 지역·청크·정책 수명주기·웹 원문 마이그레이션
│   └── seed/
│       ├── common_code.sql     # API 코드 → 이름 매핑
│       └── region_code_mapping.sql # 과거·분할·상위 지역 코드 매핑
├── docs/
│   ├── erd.md                  # 전체 테이블 관계도
│   ├── data-dictionary.md      # 테이블·컬럼 데이터 사전
│   ├── rag-chunk-strategy.md   # policy-v1 청크 생성 계약
│   ├── embedding-pipeline.md   # 임베딩 적재·벡터 검색 운영 방법
│   └── web-source-pipeline.md  # URL 발견·수집·관련성 검증 경계
├── etl/
│   ├── load_policies.py        # API/JSON 추출·변환·적재
│   ├── load_regions.py         # VWorld 시도·시군구 동기화 및 검증
│   ├── generate_policy_chunks.py # 결정적 RAG 청크 생성·동기화
│   ├── embedding_utils.py      # 임베딩·벡터 공통 검증 함수
│   ├── embed_policy_chunks.py  # OpenAI 임베딩 배치 생성·적재
│   ├── search_policy_chunks.py # 질문 임베딩·pgvector cosine 검색
│   ├── sync_policy_sources.py  # 정책 URL 정규화·연결 동기화
│   ├── crawl_policy_sources.py # 안전한 소량 HTML 수집·관련성 검증
│   ├── revalidate_policy_sources.py # 저장 문서 로컬 재검증
│   ├── review_policy_sources.py # REVIEW_REQUIRED 원문 수동 판정
│   ├── source_relevance.py     # 결정적 정책-원문 관련성 판정
│   ├── generate_policy_web_chunks.py # 검증 원문 전용 별도 청크
│   ├── embed_policy_web_chunks.py # 검증 원문 청크 전용 임베딩
│   └── logs/                  # 실행 중 생성, Git 제외
│       └── failed_policies.jsonl
├── tests/                       # 네트워크·DB 없이 실행되는 단위 테스트
├── .github/workflows/tests.yml # Python 3.10~3.12 CI
├── sample_response.json       # 로컬 적재 테스트용 응답
├── requirements.txt
├── .env.example               # 비밀값 없는 설정 템플릿
├── .env                       # 개인별 실제 설정, Git 제외
├── .gitignore
├── README_ETL.md              # 기존 ETL 설명
└── README.md
```

스키마의 기준은 [db/schema.sql](db/schema.sql), 상세 관계도는 [docs/erd.md](docs/erd.md), 변환·저장 동작의 기준은 [etl/load_policies.py](etl/load_policies.py)입니다.

## 3. PostgreSQL / pgvector와 주요 테이블

PostgreSQL에는 정책의 정형 조건과 API 원문(`JSONB`)을 함께 저장합니다. pgvector는 향후 정책 설명의 의미를 비교하는 벡터 검색에 사용합니다.

모든 마이그레이션을 적용한 최종 구성은 **18개 테이블 + 6개 View**입니다. `db/schema.sql`이 웹 원문 staging·별도 fallback 청크 계층을 포함한 기본 16개 테이블과 검증 전용 View 3개를 만들고, 지역 마이그레이션이 `region`, `region_code_mapping`과 지역 View 3개를 추가합니다. 중간 단계의 `region_code_alias`는 004 마이그레이션이 데이터를 이관한 뒤 제거하므로 최종 테이블 수에 포함되지 않습니다.

| 테이블 | 역할 | 현재 데이터 입력 방식 |
| --- | --- | --- |
| `common_code` | API 코드, 코드 그룹, 사람이 읽는 이름 | SQL seed |
| `policy_sync_job` | 온통청년 전체 동기화의 시작·종료·성공·실패·비활성화 이력 | 정책 API ETL |
| `policy` | 정책 번호·이름·지원 내용·나이·소득·기간·기관·URL·원문 | ETL UPSERT |
| `policy_application_period` | 정책별 정확한 신청 기간. 복수 기간은 여러 행으로 저장 | ETL 재동기화 |
| `policy_region` | 정책별 대상 지역 코드. `(policy_no, region_code)`가 기본키 | ETL 재동기화 |
| `region` | VWorld 기준 현재 시도·시군구 코드와 이름 | 지역 ETL UPSERT |
| `region_code_mapping` | 과거·폐지·분할·상위 지역 코드를 현재 코드에 연결 | SQL seed |
| `policy_eligibility_code` | 전공·취업·학력·특화 대상의 복수 조건 코드 | ETL 재동기화 |
| `policy_chunk` | 정책을 나눈 텍스트, 변경 감지 정보, 임베딩 | `policy-v1` 생성기로 동기화 |
| `web_source` | 정규화한 원문 URL과 HTTP 수집 상태 | URL 동기화·크롤러 |
| `policy_web_source` | 정책과 URL의 관계 및 관련성 검증 결과 | URL 동기화·검증기 |
| `web_document` | 수집·정제한 HTML 본문 버전 | 크롤러 staging |
| `policy_enrichment_fact` | 원문에서 추출한 필드값·근거·검증 상태 | 추후 보강 추출기 |
| `policy_web_chunk` | 검증된 크롤링 원문만 나눈 별도 fallback 청크·임베딩 | 웹 청크·임베딩 CLI |
| `chat_room` | 대화방 | 스키마 준비 |
| `chat_profile` | 대화방별 사용자 나이·지역·소득 등 추천 조건 | 스키마 준비 |
| `chat_message` | 대화방의 사용자·챗봇 메시지 | 스키마 준비 |
| `message_policy_ref` | 메시지가 참조한 정책과 관련도 점수 | 스키마 준비 |

`v_policy_region_resolved`는 원본 코드를 현재 지역으로 해석합니다. `DIRECT`, `MAPPED`, 알려진 범위 누락을 보정한 `INFERRED`, 해석 불가인 `UNRESOLVED`를 구분합니다. `v_policy_region_scope`는 정책의 전국 여부를, `v_policy_sido_scope`는 시도 전체 포함 여부를 공통 판정합니다. `v_verified_policy_web_document`, `v_verified_policy_enrichment`, `v_verified_policy_web_chunk`는 활성 정책에 연결되고 검증을 통과한 현재 원문·보강 사실·별도 웹 청크만 노출합니다.

API 원천 계층과 크롤링 계층은 서로 덮어쓰지 않습니다. `policy`와 `policy_chunk`는 온통청년 API 전용이며, 크롤링 결과는 `web_*`, `policy_enrichment_fact`, `policy_web_chunk`에만 저장합니다. 두 계층은 `policy_no`를 RAG fallback 범위 지정용 연결 키로만 공유합니다.

정책 하나에는 여러 지역·자격 조건·청크가 연결됩니다. 대화방에는 프로필 하나와 여러 메시지가 연결되며, 메시지와 정책은 `message_policy_ref`로 연결합니다. `common_code`는 코드 해석용 사전이며, 현재 정책의 코드 컬럼에 공통 코드 외래키를 강제하지는 않습니다.

`policy_chunk.embedding`은 `VECTOR(1536)`이며 사용할 임베딩 모델은 `text-embedding-3-small`입니다. 코사인 거리용 HNSW 인덱스가 정의되어 있습니다. 아직 임베딩되지 않은 청크는 벡터·모델명·임베딩 시각이 모두 `NULL`입니다. 청크 생성기는 API를 호출하지 않고, 별도의 임베딩 CLI가 명시적인 `--write` 실행에서만 API를 호출합니다. 모델이나 차원을 변경한다면 컬럼·인덱스·기존 임베딩의 재생성 계획을 함께 조정해야 합니다.

### DB 준비

Python은 3.10 이상을 기준으로 준비합니다. PostgreSQL 서버에는 `vector` 확장과 HNSW를 지원하는 pgvector가 설치되어 있어야 하며, `psql` 클라이언트도 필요합니다. `CREATE EXTENSION`은 서버에 없는 pgvector 패키지를 설치해 주지 않습니다.

로컬 서버의 `postgres` 계정을 사용하는 예시입니다. 사용자명·호스트·포트는 자신의 환경에 맞추고 비밀번호는 프롬프트에서 입력하세요.

```bash
createdb -h localhost -p 5432 -U postgres -W youthlink
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/extensions.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/schema.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/seed/common_code.sql
```

이미 `youthlink` DB가 있다면 생성 명령은 생략합니다. 확장 설치 권한이 없으면 DB 관리자에게 `vector`, `pgcrypto` 활성화를 요청합니다. pgAdmin을 사용한다면 같은 DB의 Query Tool에서 위 SQL 파일 내용을 순서대로 실행해도 됩니다.

`schema.sql`의 `CREATE TABLE IF NOT EXISTS`는 기존 테이블 구조를 최신 형태로 변경하는 마이그레이션이 아닙니다. 이후 컬럼·제약조건 변경은 별도 마이그레이션으로 관리해야 합니다.

### 새 DB의 지역 구성 순서

위의 확장·스키마·공통 코드 seed를 적용한 뒤 `.env`에 `VWORLD_API_KEY`, `VWORLD_API_DOMAIN`, `DATABASE_URL`을 설정하고 다음 순서를 그대로 따릅니다.

```bash
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/001_add_region_master.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/002_change_region_code_mapping.sql

python etl/load_regions.py

psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/seed/region_code_mapping.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/003_add_policy_region_resolved_view.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/004_retire_region_code_alias.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/005_prepare_policy_chunk_pipeline.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/006_add_policy_application_period.sql
python etl/load_policies.py backfill-periods
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/007_complete_incheon_scope_and_add_scope_views.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/008_add_policy_sync_tracking.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/009_add_policy_web_sources.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/010_bind_web_validation_to_document.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/011_add_separate_policy_web_chunks.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/012_enforce_validated_document_source.sql
```

`region_code_mapping.canonical_code`는 `region.region_code`를 참조하므로 **VWorld 지역 적재를 매핑 seed보다 먼저** 실행해야 합니다. 004는 레거시 alias를 이관·제거하고, 006은 정확한 복수 신청기간 테이블을 추가합니다. 기존 DB는 006 뒤 `backfill-periods`로 원문을 재파싱합니다. 007은 인천 범위 누락 보정과 공용 지역 범위 View를, 008은 정책 동기화 작업과 소프트 비활성화를, 009는 검증형 웹 원문 staging 계층을 추가합니다. 010은 관련성 판정을 실제로 검사한 `web_document` 버전에 묶습니다. 011은 API 기반 `policy_chunk`와 완전히 분리된 크롤링 fallback 청크·벡터 계층을 추가하며, 012는 판정 문서가 동일 `web_source` 소속임을 복합 FK로 강제합니다.

현재 별도의 마이그레이션 이력 도구는 없습니다. 적용한 파일과 순서를 환경별로 기록하고, 번호 순서에 따라 수동으로 적용하세요. 기존 DB에서는 이미 적용한 마이그레이션을 무조건 반복 실행하지 말고 현재 스키마와 데이터를 먼저 확인합니다.

### common_code seed

`db/seed/common_code.sql`은 `API코드정보.xlsx`의 코드정보를 바탕으로 만든 코드 사전입니다. 제공 기관, 제공 방식, 승인 상태, 신청·사업 기간 구분, 혼인·소득 조건, 전공·취업·학력·특화 대상 코드를 저장합니다. 지역 코드 전체 사전은 이 seed의 범위가 아닙니다.

- 코드의 앞자리 `0`을 보존하기 위해 문자열로 다룹니다.
- `ON CONFLICT (code) DO UPDATE`로 재실행 시 코드 그룹과 이름을 갱신합니다.
- 원본 자료의 `bizPrdSecd`는 API 필드명과 맞추어 `bizPrdSeCd`로 통일했습니다.
- 새 API 코드가 발견되면 seed를 보완합니다. seed에서 빠진 행이 재실행만으로 DB에서 삭제되지는 않습니다.

## 4. 가상환경과 의존성 설치

```bash
git clone https://github.com/inha-youthlink/youthlink-data-pipeline.git
cd youthlink-data-pipeline
python -m venv .venv
```

사용 중인 터미널에 맞는 활성화 명령 하나를 실행합니다.

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# Windows Git Bash
source .venv/Scripts/activate

# macOS / Linux
source .venv/bin/activate
```

```bash
python -m pip install -r requirements.txt
```

주요 의존성은 PostgreSQL 연결용 `psycopg[binary]`, HTTP 요청용 `requests`, `.env` 로딩용 `python-dotenv`, 임베딩용 공식 `openai` SDK, 토큰 계산용 `tiktoken`입니다. PowerShell에서 활성화가 제한되면 `.\.venv\Scripts\python.exe`를 `python` 대신 사용해 설치·실행할 수 있습니다.

### 자동 테스트

정책 변환·CLI·API 응답 검증, VWorld prefix 필터·중복 처리, 청크 생성, 임베딩 응답 순서·차원 검증과 벡터 검색 SQL은 외부 API나 DB에 연결하지 않는 단위 테스트로 확인할 수 있습니다.

```bash
python -m unittest discover -s tests -v
```

같은 명령은 GitHub Actions에서 Python 3.10, 3.11, 3.12로 실행됩니다.

## 5. 환경변수 설정

```powershell
# PowerShell
Copy-Item .env.example .env
```

```bash
# Git Bash / macOS / Linux
cp .env.example .env
```

이미 `.env`가 있다면 덮어쓰지 말고 필요한 항목만 보완하세요. 아래는 **실제 비밀값이 없는 `.env.example` 형식**입니다.

```dotenv
# 온통청년 API
YOUTH_API_URL=
YOUTH_API_KEY=
YOUTH_API_PAGE_SIZE=100
YOUTH_API_REQUEST_DELAY_SECONDS=1
YOUTH_API_MAX_ATTEMPTS=3
YOUTH_API_RETRY_BASE_DELAY_SECONDS=1

# VWorld API
VWORLD_API_KEY=
VWORLD_API_DOMAIN=http://localhost

# PostgreSQL
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/youthlink

# OpenAI Embeddings
OPENAI_API_KEY=
OPENAI_EMBEDDING_MODEL=text-embedding-3-small

# Policy web-source crawler
YOUTHLINK_CRAWLER_USER_AGENT=YouthLinkResearchBot/0.1
YOUTHLINK_CRAWLER_REQUEST_DELAY_SECONDS=2
YOUTHLINK_CRAWLER_TIMEOUT_SECONDS=15
YOUTHLINK_CRAWLER_MAX_RESPONSE_BYTES=2097152
```

| 변수 | 설명 |
| --- | --- |
| `YOUTH_API_URL` | 온통청년 정책 조회 API의 기본 요청 주소. 키가 포함된 전체 요청 URL을 넣지 않음 |
| `YOUTH_API_KEY` | 발급받은 인증키. 개인 `.env`에만 입력 |
| `YOUTH_API_PAGE_SIZE` | 페이지당 요청 건수. 기본 100, API 허용 범위 내의 양수 사용 |
| `YOUTH_API_REQUEST_DELAY_SECONDS` | 연속 페이지 요청 사이 대기시간. 기본 1초 |
| `YOUTH_API_MAX_ATTEMPTS` | 네트워크·429·5xx 오류의 최대 요청 횟수. 기본 3 |
| `YOUTH_API_RETRY_BASE_DELAY_SECONDS` | 재시도 지수 대기의 시작값. 기본 1초, 개별 대기는 최대 30초 |
| `VWORLD_API_KEY` | VWorld에서 발급받은 인증키. 개인 `.env`에만 입력 |
| `VWORLD_API_DOMAIN` | VWorld 인증키에 등록한 요청 도메인. 로컬 개발은 보통 `http://localhost` 사용 |
| `DATABASE_URL` | 적재 대상 PostgreSQL 연결 문자열 |
| `OPENAI_API_KEY` | 임베딩을 실제 생성할 때만 필요한 개인 API 키. Git에 커밋하지 않음 |
| `OPENAI_EMBEDDING_MODEL` | 임베딩 모델. 기본 `text-embedding-3-small` |
| `YOUTHLINK_CRAWLER_USER_AGENT` | 웹 원문 요청에 표시할 식별자. 운영 전 팀 연락처를 포함한 값으로 합의 권장 |
| `YOUTHLINK_CRAWLER_REQUEST_DELAY_SECONDS` | 서로 다른 원문 요청 사이 대기시간. 기본 2초 |
| `YOUTHLINK_CRAWLER_TIMEOUT_SECONDS` | 원문·robots.txt 요청 제한시간. 기본 15초 |
| `YOUTHLINK_CRAWLER_MAX_RESPONSE_BYTES` | 원문 한 건의 최대 응답 크기. 제한형 iframe 수집은 바깥 shell과 내부 본문 응답을 합산하며 기본 2 MiB |

`YOUR_PASSWORD`는 자리표시자입니다. 실제 DB 사용자와 비밀번호로 변경하고, 연결 문자열의 비밀번호에 `@`, `:`, `/` 등 특수문자가 있으면 URL 인코딩해야 합니다. Python 실행 시 `load_dotenv()`가 설정을 읽습니다. 일반 `psql` 명령은 이 `.env`를 자동으로 읽지 않습니다.

정책 파일 모드는 DB 설정만 필요하고, 정책 API 모드는 `YOUTH_API_*`와 DB 설정을 확인해야 합니다. 지역 동기화에는 `VWORLD_API_KEY`, `VWORLD_API_DOMAIN`, `DATABASE_URL`이 필요합니다. VWorld 인증키의 허용 도메인과 `VWORLD_API_DOMAIN` 값이 다르면 요청이 거절될 수 있습니다. `OPENAI_API_KEY`는 정책·지역·청크 생성에는 필요하지 않으며 임베딩 적재와 질문 벡터 검색에만 사용합니다.

## 6. 샘플 JSON 테스트

정책 ETL의 `file`과 `api` 명령 분기는 정상 동작하도록 정리되어 있습니다. 다음 명령으로 로컬 JSON을 적재합니다.

```bash
python etl/load_policies.py file sample_response.json
```

파일은 전체 API 응답(`result.youthPolicyList`) 또는 정책 객체 배열 형식을 받을 수 있습니다. API 응답의 페이징 키는 코드상 `pagging`입니다. 샘플에 있는 정책만 저장하며 전체 API를 호출하지 않습니다. 정상 종료 시 `loaded N policies`가 출력됩니다.

**파일 모드는 현재 한 트랜잭션으로 전체를 적재합니다.** 한 건이 실패하면 해당 실행 전체를 롤백하며, API 모드의 정책별 SAVEPOINT와 실패 JSONL 로깅은 적용되지 않습니다. 동일 샘플을 두 번 실행하고 정책·연결 테이블에 중복이 늘지 않는지 검증하세요.

## 7. 실제 API 실행과 ETL 흐름

```bash
python etl/load_policies.py api

# 환경변수의 페이지 크기를 이번 실행에서만 덮어쓰는 예
python etl/load_policies.py api --page-size 100
```

페이지 크기는 전체 수집량 제한이 아닙니다. **2,990건 같은 고정 상한은 없습니다.** 현재 코드는 페이지가 비거나 반환 건수가 요청 페이지 크기보다 작으면 종료합니다. `totCount`는 진행 상황 표시용이며 전체 건수가 늘어나도 페이지 순회를 수행합니다. 이 종료 방식은 API가 마지막 페이지 전에는 요청 크기만큼 반환한다는 전제이므로, API의 페이지 크기 제한이나 응답 규칙이 바뀌면 재점검해야 합니다.

```mermaid
flowchart TD
    A[온통청년 API 페이지 요청] --> B[정책 목록 추출]
    B --> C[정책 한 건 정제]
    C --> D[policy UPSERT]
    D --> E[지역·자격 조건 재동기화]
    E --> F[페이지 처리 후 커밋]
    C -. 실패 .-> G[해당 정책 SAVEPOINT 롤백]
    D -. 실패 .-> G
    E -. 실패 .-> G
    G --> H[실패 JSONL 기록 후 다음 정책]
    F --> I[다음 페이지 또는 종료]
```

1. **Extract:** `fetch_page()`가 `apiKeyNm`, `pageNum`, `pageSize`, `pageType=1`, `rtnType=json`으로 요청합니다. 요청 타임아웃은 30초입니다.
2. **Transform:** `transform_policy()`가 API 필드를 DB 컬럼으로 변환하고 지역·자격 조건을 분리합니다.
3. **Load:** `load_policy()`가 정책과 신청기간·지역·자격조건 연결 테이블을 같은 정책 단위 트랜잭션에서 갱신합니다.

| API 필드 | 저장 위치·변환 |
| --- | --- |
| `plcyNo`, `plcyNm` | `policy_no`, `policy_name`. 정책 번호 누락은 오류, 이름 누락은 `(제목 없음)` |
| `plcyExplnCn`, `plcySprtCn` | 정책 설명, 지원 내용 |
| `plcyKywdNm`, `lclsfNm`, `mclsfNm` | 쉼표를 분리한 `TEXT[]` |
| `sprtTrgtMinAge`, `sprtTrgtMaxAge` | 정수, 0 이하·변환 불가 값은 `NULL` |
| `aplyYmd` | 단일·복수 신청 기간을 파싱하여 `policy_application_period`에 저장하고 원문도 보존 |
| `zipCd` | 지역 코드별 `policy_region` 행 |
| `plcyMajorCd`, `jobCd`, `schoolCd`, `sbizCd` | 각각 `MAJOR`, `JOB`, `SCHOOL`, `SPECIAL` 조건 행 |
| 정책 객체 전체 | `policy.raw_data`에 `JSONB`로 보존 |

빈 문자열은 기본적으로 `NULL`, `Y/N`은 불리언으로 변환합니다. 신청기간의 리터럴 `\\N`과 실제 줄바꿈은 복수 기간 구분자로 처리합니다. 파싱 상태는 `EMPTY`, `SINGLE`, `MULTIPLE`, `RAW`, `INVALID`로 저장합니다. 1900년 미만 더미 날짜와 역전된 기간은 `INVALID`로 표시하여 정책 적재를 실패시키지 않고, 원문은 유지하되 RAG 기간 텍스트에서는 제외합니다. `policy.application_start_date/end_date`는 신뢰 가능한 단일 기간에만 채우고, 정확한 조회는 `policy_application_period`를 사용합니다. 나이는 0 이하를 미설정(`NULL`)으로 처리합니다.

## 8. 지역 코드 처리

온통청년 API의 `zipCd`는 5자리 시군구 코드입니다. 정책 수집 시 이 값은 임의로 현재 코드로 치환하지 않고 `policy_region.region_code`에 원본 그대로 보존합니다. 이렇게 하면 원천 데이터의 추적 가능성을 유지하면서도, 별도의 지역 마스터와 매핑을 통해 현재 행정구역 기준으로 조회할 수 있습니다.

현재 지역 코드와 이름은 VWorld의 시도·시군구 조회 API를 통해 `region` 테이블에 주기적으로 동기화합니다. 관련 객체의 역할은 다음과 같습니다.

| 객체 | 역할 |
| --- | --- |
| `policy_region` | 온통청년 정책에 포함된 원본 `zipCd` 보존 |
| `region` | VWorld 기준 현재 시도·시군구 코드, 이름, 활성 상태 관리 |
| `region_code_mapping` | 폐지 코드, 분할 지역, 상위 지역 코드를 하나 이상의 현재 코드로 연결 |
| `v_policy_region_resolved` | 원본 코드를 현재 코드와 지역명으로 해석하여 RAG·백엔드에 제공 |
| `v_policy_region_scope` | 정책을 `NATIONWIDE`, `PARTIAL`, `UNKNOWN`으로 공통 판정 |
| `v_policy_sido_scope` | 정책별 시도 범위를 `SIDO_WIDE`, `PARTIAL`로 판정 |

`region_code_mapping`은 `(source_code, canonical_code)`를 기준으로 1:N 매핑을 지원합니다. 하나의 과거 코드가 여러 현재 행정구역으로 분할된 경우 View 결과도 여러 행으로 확장됩니다. 인천의 알려진 9개 코드 범위 누락은 007 View에서만 보정하고 `policy_region` 원본은 변경하지 않습니다.

### 지역 마스터 동기화

먼저 `.env.example`을 참고하여 개인 `.env`에 `VWORLD_API_KEY`와 `VWORLD_API_DOMAIN`을 설정한 뒤 저장소 루트에서 실행합니다.

```bash
python etl/load_regions.py
```

지역 ETL은 VWorld에서 시도·시군구 목록을 조회하고 응답의 중복, 코드 형식, 최소 지역 수 등을 검증한 뒤 `region`을 UPSERT합니다. 동기화가 끝나면 `policy_region`의 원본 코드 중 `region`에도 없고 `region_code_mapping`에도 없는 코드를 자동으로 검사합니다. 미해결 코드 0건이 운영 목표이며, 2026-10-30 기준 새로 발견된 대전 상위 코드 `30000` 1건은 후속 매핑 대상으로 남아 있습니다.

```text
=== policy region validation ===
unresolved region codes: 0
all policy regions resolved: OK
```

`[WARN] unresolved region codes`가 출력되면 해당 코드를 임의로 버리거나 원본 값을 수정하지 말고, VWorld의 신규 코드인지 행정구역 변경 이력인지 확인한 후 `region_code_mapping`을 보완합니다.

### RAG·백엔드 조회

RAG와 백엔드에서는 원본 `policy_region`을 직접 필터링하기보다 `v_policy_region_resolved.resolved_region_code`를 사용하는 것을 권장합니다. View의 `resolution_type`은 다음 의미를 가집니다.

- `DIRECT`: 원본 코드가 현재 `region` 코드와 직접 일치
- `MAPPED`: 과거·상위 코드가 `region_code_mapping`을 통해 현재 코드로 변환
- `INFERRED`: 알려진 원천 범위 누락을 코드 집합으로 안전하게 보정. 이 행의 `source_region_code`는 `NULL`
- `UNRESOLVED`: 현재 지역과 매핑 어디에서도 해석되지 않음

특정 현재 지역에 적용되는 정책은 다음과 같이 조회합니다. 이 쿼리는 해당 현재 코드로 저장된 정책뿐 아니라 그 코드로 매핑되는 과거 코드의 정책도 함께 찾습니다.

```sql
SELECT DISTINCT
    p.policy_no,
    p.policy_name
FROM policy p
JOIN v_policy_region_resolved r
  ON r.policy_no = p.policy_no
WHERE r.resolved_region_code = '28125';
```

정책별 지역명을 RAG context에 포함하려면 다음과 같이 조회할 수 있습니다.

```sql
SELECT DISTINCT
    p.policy_no,
    p.policy_name,
    r.region_name
FROM policy p
JOIN v_policy_region_resolved r
  ON r.policy_no = p.policy_no
WHERE p.policy_no = '정책번호';
```

운영 중에는 지역 동기화 후 아래 쿼리로 미해결 코드와 영향을 받는 정책 수를 별도로 확인할 수 있습니다. 결과가 0행이어야 모든 원본 지역 코드를 해석할 수 있는 상태입니다.

```sql
SELECT
    source_region_code,
    COUNT(DISTINCT policy_no) AS policy_count
FROM v_policy_region_resolved
WHERE resolution_type = 'UNRESOLVED'
GROUP BY source_region_code
ORDER BY source_region_code;
```

전국 및 시도 전체 여부는 애플리케이션마다 다시 계산하지 않고 공용 View를 사용합니다.

```sql
SELECT policy_no, region_scope,
       resolved_active_region_count, total_active_region_count
FROM v_policy_region_scope
WHERE policy_no = '정책번호';

SELECT policy_no, sido_code, sido_name, sido_scope
FROM v_policy_sido_scope
WHERE policy_no = '정책번호'
ORDER BY sido_code;
```

## 9. 실패 레코드 로깅과 계속 진행

API 모드는 **페이지 단위 트랜잭션 안에 정책별 SAVEPOINT**를 둡니다. 정책 저장 중 오류가 나면 그 정책의 변경만 롤백하고, 실패 내용을 기록한 뒤 다음 정책을 처리합니다. 지역·자격 조건을 갱신하다 실패해도 해당 정책의 기존 데이터까지 부분적으로 바뀐 상태로 남지 않도록 같은 SAVEPOINT로 묶습니다.

단순 `try/except`만으로는 PostgreSQL의 실패한 트랜잭션 상태를 복구할 수 없습니다. 현재 코드는 중첩된 `conn.transaction()`을 이용해 SAVEPOINT를 만들고 복구합니다. 성공한 정책들은 바깥 페이지 트랜잭션이 커밋되어야 최종 반영됩니다.

실패 로그는 `etl/logs/failed_policies.jsonl`에 UTF-8로 추가됩니다. **한 줄에 JSON 객체 하나**이며 실행할 때마다 누적됩니다. 다음은 가상 정책으로 만든 형식 예시입니다.

```json
{"timestamp":"2026-09-30T10:00:00","page_num":1,"policy_no":"EXAMPLE_POLICY","policy_name":"예시 정책","error_type":"CheckViolation","error_message":"나이 범위 제약조건 위반","raw_data":{"plcyNo":"EXAMPLE_POLICY","sprtTrgtMinAge":"30","sprtTrgtMaxAge":"20"}}
```

로그의 정책 번호·오류 종류·원문을 확인하고 변환 규칙을 수정한 뒤 재실행합니다. 실패 로그 자체는 일반 JSON 배열이 아니며, 현재 `file` 명령에 JSONL 파일을 직접 전달하는 재처리 기능은 없습니다.

주의할 현재 동작:

- 연결 오류·타임아웃·HTTP 429·5xx는 제한적으로 재시도합니다. 일반 4xx는 즉시 중단하며, 요청 URL과 API 키가 예외·작업 기록에 노출되지 않도록 페이지 번호와 상태 코드만 기록합니다.
- 페이지 사이에는 기본 1초를 기다립니다. 온통청년은 공개 문서에 정확한 호출 제한을 명시하지 않으므로 403을 임의로 무한 재시도하지 말고, 작업을 `FAILED`로 보존한 뒤 원인을 확인합니다.
- DB 연결 장애, 페이지 커밋 실패, 로그 파일 쓰기 실패도 계속 진행을 보장하지 않습니다.
- API 응답에 `result.youthPolicyList`가 없거나 타입이 잘못되면 빈 마지막 페이지로 오인하지 않고 실행을 중단합니다. 다만 공급자가 별도 업무 오류 코드를 추가하면 그 코드의 성공·실패 규칙은 추가 검토해야 합니다.
- 종료 요약의 `success`는 신규 삽입과 기존 갱신을 합한 처리 건수이며 DB 전체 행 수와 다를 수 있습니다. 처리 실패 건이 있어도 현재 CLI는 별도의 실패 종료 코드를 설정하지 않습니다.

## 10. UPSERT와 주기 동기화 전략

`policy_no`를 기준으로 신규 정책은 INSERT, 기존 정책은 UPDATE합니다. 기존 정책의 수집 대상 컬럼과 `raw_data`를 최신 응답으로 교체하고 `synced_at`을 갱신합니다. `source_updated_at`은 원천 API의 수정 시각이고, `synced_at`은 DB 동기화 시각입니다.

`policy_application_period`, `policy_region`, `policy_eligibility_code`는 해당 정책의 기존 연결을 삭제한 뒤 현재 응답으로 다시 삽입합니다. 복합 기본키·UNIQUE 제약과 충돌 처리를 통해 반복 실행 시 중복을 방지합니다. 다만 실행마다 동기화 시각과 연결 행의 내부 ID는 바뀔 수 있습니다.

각 API 실행은 `policy_sync_job`에 `RUNNING`, `SUCCEEDED`, `PARTIAL`, `FAILED` 상태와 처리 건수를 기록합니다. API에서 확인된 정책은 `is_active = true`와 현재 `last_seen_sync_job_id`로 갱신됩니다. 다음 조건을 모두 만족한 전체 동기화에서만 이번 작업에 나타나지 않은 정책을 삭제하지 않고 `is_active = false`로 바꿉니다.

- 페이지 순회가 정상적으로 끝남
- API 총건수가 양수이고 모든 페이지에서 일관됨
- 처리·성공·고유 정책 수가 API 총건수와 일치함
- 정책 단위 실패가 0건임

빈 응답, 일부 정책 실패, 총건수 변화, 중복 정책 번호, 네트워크 중단이 있으면 작업은 `PARTIAL` 또는 `FAILED`가 되고 비활성화는 수행하지 않습니다. 이후 API에 다시 나타난 정책은 자동으로 활성화됩니다.

현재는 CLI를 수동 재실행하는 구조이며, 스케줄러는 구현되지 않았습니다. 다음 운영 단계는 다음과 같습니다.

1. API 호출 한도와 필요한 최신성을 기준으로 동기화 주기를 정합니다.
2. 작업 스케줄러 또는 CI에서 실행하고 동시 중복 실행을 막습니다.
3. `policy_sync_job`의 실패·부분 성공을 모니터링하고 알립니다.
4. 반복 페이지 감지와 공급자별 호출 한도 설정을 보완합니다.
5. 정책 변경 시 관련 청크·임베딩을 갱신하는 흐름을 연결합니다.

API에서 사라진 정책과 기존 청크·임베딩은 이력 확인을 위해 보존합니다. 청크 생성, 임베딩 후보 선택, Vector Search는 기본적으로 `is_active = true` 정책만 사용합니다.

## 11. 검증형 정책 원문 수집

상세 안전 규칙과 상태 전이는 [검증형 정책 원문 수집 파이프라인](docs/web-source-pipeline.md)을 참고합니다. 원문 URL을 발견하는 단계, HTML을 staging에 저장하는 단계, 정책과의 관련성을 승인하는 단계를 분리합니다. 따라서 URL이 있거나 페이지 수집이 성공했다는 이유만으로 그 내용이 정책 데이터나 RAG에 들어가지 않습니다.

009~012 마이그레이션 적용 후 먼저 네트워크 요청 없이 URL 현황을 확인하고 저장합니다.

```bash
python etl/sync_policy_sources.py
python etl/sync_policy_sources.py --write
```

새 연결은 모두 `PENDING`입니다. 기본 크롤러 실행도 후보 계획만 표시하고 네트워크를 사용하지 않습니다.

```bash
python etl/crawl_policy_sources.py --limit 10

# 검토한 도메인에서 소량만 실제 수집
python etl/crawl_policy_sources.py --host example.go.kr --limit 3 --write
```

크롤러는 누락된 신청방법·심사방법·제출서류·참여 제외·추가 자격이 많은 활성 정책을 먼저 선택합니다. 실제 요청에서는 DNS와 모든 리다이렉트를 검사하고, robots.txt를 확인할 수 없거나 허용되지 않으면 차단하며, HTML·응답 크기·시간을 제한합니다.

정확히 확인한 소스만 파일럿하거나 이미 수집한 문서를 새 추출 규칙으로 다시 가져올 때에는 반복 가능한 `--source-id`와 `--refetch`를 사용합니다.

```bash
python etl/crawl_policy_sources.py \
  --source-id <SOURCE_UUID_1> \
  --source-id <SOURCE_UUID_2> \
  --retry-failed \
  --limit 2

# 위 계획이 정확할 때만 실행. FETCHED 문서의 재추출은 --refetch 사용
python etl/crawl_policy_sources.py \
  --source-id <SOURCE_UUID> \
  --refetch \
  --limit 1 \
  --write
```

수집 실패 시 응답 원문은 `web_document`에 저장하지 않습니다. 대신 재시도 판단에 필요한 비민감 정보만 `web_source.metadata.last_failure_diagnostics`에 기록합니다. 여기에는 HTTP 상태·Content-Type·응답 바이트 수, 최종 host/path, 추출 문자 수, script/iframe 개수 등이 포함되며 최종 URL의 query와 응답 본문은 남기지 않습니다. 성공 또는 `304 Not Modified`가 확인되면 이전 실패 진단은 제거됩니다.

`UNSUPPORTED`는 HTML이 아니거나, 너무 크거나, 정적 HTML에서 정책 검증에 충분한 본문을 얻지 못한 상태입니다. 기본 실행은 이를 자동 재시도하거나 검증하지 않습니다. 진단 정보를 확인하고 정적 요청으로 회복 가능하다고 판단한 경우에만 `--retry-failed`를 명시적으로 사용합니다. JavaScript 렌더링 수집기는 아직 구현하지 않았으므로 일반 JS shell을 억지로 `VERIFIED` 처리해서는 안 됩니다.

예외적으로 Naver 블로그는 `https://blog.naver.com/{blogId}/{숫자 logNo}` 형태의 **정확한 게시물 URL**만 제한 지원합니다. 블로그 루트는 임의의 최신 글로 바뀔 수 있어 따라가지 않습니다. 바깥 문서의 `mainFrame`이 정확히 하나이고 내부 대상이 HTTPS 기본 포트의 `blog.naver.com/PostView.naver`이며 `blogId`와 `logNo`가 바깥 URL과 일치할 때만 깊이 1로 수집합니다. iframe과 리다이렉트 각 hop에서 robots·DNS·대상 URL을 다시 검사하고, 바깥 shell과 내부 본문의 응답 크기를 합산합니다. 내부에서는 `se-main-container` 본문만 추출해 블로그 메뉴·공지·댓글 UI를 배제합니다. URL query는 저장하지 않고 `web_document.metadata.retrieval`에 host/path·게시물 식별자·상태·크기 등 query 없는 provenance만 남깁니다. 이 경로의 자동 `VERIFIED` 결과도 항상 `REVIEW_REQUIRED`로 낮춰 사람이 확인합니다.

본문 hash가 달라져 새 `web_document` 버전이 생기면 이전 자동·수동 판정은 초기화됩니다. 새 현재 문서는 다시 자동 검증과 필요한 수동 검토를 통과해야 하며 과거 승인이 승계되지 않습니다.

재수집이 `BLOCKED`·`UNSUPPORTED` 또는 HTTP 404/410으로 끝나면 이전 문서는 더 이상 현재 버전으로 간주하지 않고 기존 판정도 `PENDING`으로 되돌립니다. 따라서 삭제·변조·수집 금지된 페이지의 과거 웹 청크가 검증 View에 계속 노출되지 않습니다. 일시적인 네트워크 실패처럼 `FAILED`인 경우에는 마지막 검증 문서를 유지해 다음 재시도를 허용합니다.

검증 규칙이 바뀌었거나 이미 수집한 URL에 새 정책 연결이 생기면 네트워크 요청 없이 현재 문서를 다시 판정합니다.

```bash
python etl/revalidate_policy_sources.py --limit 100
python etl/revalidate_policy_sources.py --limit 100 --write
```

`REVIEW_REQUIRED`는 자동 승인하지 않고 사람이 정책명·기관·대상·신청 내용을 직접 비교합니다. 먼저 URL을 포함한 후보를 조회합니다.

```bash
python etl/review_policy_sources.py --limit 20 --show-url
```

판정은 화면에 표시된 `policy_no`, `source_id`, `document_id`를 정확히 모두 지정합니다. 기본 실행은 DB를 바꾸지 않고 현재 최신 문서와 검토 대상이 일치하는지만 확인합니다.

```bash
python etl/review_policy_sources.py \
  --policy-no <POLICY_NO> \
  --source-id <SOURCE_UUID> \
  --expected-document-id <DOCUMENT_UUID> \
  --decision VERIFIED \
  --reviewed-by <REVIEWER> \
  --reason "정책명·운영기관·신청 내용이 일치"

# 위 dry-run의 대상과 판정이 맞을 때만 저장
python etl/review_policy_sources.py \
  --policy-no <POLICY_NO> \
  --source-id <SOURCE_UUID> \
  --expected-document-id <DOCUMENT_UUID> \
  --decision VERIFIED \
  --reviewed-by <REVIEWER> \
  --reason "정책명·운영기관·신청 내용이 일치" \
  --write
```

`VERIFIED`는 해당 정책의 사실 근거로 쓸 수 있을 때만 선택합니다. 다른 정책 페이지나 오류 페이지는 `REJECTED`, 공통 포털·목록·로그인 페이지처럼 이동 경로로만 유용하면 `LINK_ONLY`로 판정합니다. 수동 판정은 지정한 문서 버전이 아직 최신이고 미검토 상태일 때만 저장되며, 동일 정책-URL의 모든 관계 유형에 한 번에 적용됩니다.

URL의 정책 번호만 일치하는 것으로는 승인하지 않습니다. 정책 번호와 페이지 내부의 정확한 정책명이 함께 맞거나, 정확한 정책명과 충분한 독립 근거가 일치하고 강한 충돌이 없어야 `VERIFIED`가 됩니다. 각 판정은 `validated_document_id`로 검사한 문서 버전에 고정됩니다. `PENDING`, `REVIEW_REQUIRED`, `REJECTED`, `LINK_ONLY` 데이터는 staging에만 남습니다. RAG·백엔드는 다음 View만 사용합니다.

```sql
SELECT * FROM v_verified_policy_web_document;
SELECT * FROM v_verified_policy_enrichment;
```

크롤링 결과는 앞으로도 API 기반 `policy`와 `policy_chunk`에 합치지 않습니다. 검증된 원문만 별도 `policy_web_chunk`로 생성하고 별도로 임베딩합니다.

```bash
# 검증된 현재 원문만 별도 웹 청크로 생성
python etl/generate_policy_web_chunks.py
python etl/generate_policy_web_chunks.py --write

# 별도 웹 청크만 임베딩. 기본 실행은 비용 없는 계획 모드
python etl/embed_policy_web_chunks.py
python etl/embed_policy_web_chunks.py --write
```

RAG의 권장 순서는 다음과 같습니다.

1. `policy` 조건 필터와 `policy_chunk` 검색으로 API 기반 답변을 먼저 구성합니다.
2. 선택된 정책에서 필요한 필드나 설명이 비어 있을 때만 fallback을 실행합니다.
3. 같은 `policy_no` 범위에서 `v_verified_policy_web_chunk`를 검색합니다.
4. 결과에는 `source_type = CRAWLED_WEB`, 원문 URL, `document_id`를 함께 남깁니다.

웹 청크를 전체 정책 추천의 1차 검색 대상으로 사용하거나 웹 사실을 `policy` 컬럼에 `UPDATE`하면 안 됩니다. URL 동기화·수집·웹 청크 생성만으로 기존 API 청크나 임베딩을 다시 만들 필요도 없습니다.

## 12. RAG 정책 청크 생성

청크 규칙의 기준 문서는 [RAG 정책 Chunk 생성 전략](docs/rag-chunk-strategy.md)입니다. 정책마다 `SUMMARY(0)`, `ELIGIBILITY(1)`, `SUPPORT(2)`, `APPLICATION(3)`을 생성하며, 신청 상세와 신청·참고 URL이 모두 없으면 3번 청크는 생략합니다. URL은 청크 생성 여부의 신호로만 사용하고 임베딩 텍스트에는 넣지 않습니다. 인덱스는 생략된 청크가 있어도 당기지 않습니다.

기존 DB는 먼저 005 마이그레이션을 적용합니다. 기본 실행은 DB를 변경하지 않고 정책 5건 중 2건의 결과를 보여주는 안전한 dry-run입니다.

```bash
python etl/generate_policy_chunks.py

# 정책 수나 출력 수를 지정한 dry-run
python etl/generate_policy_chunks.py --limit 20 --sample-size 3

# 특정 정책만 확인
python etl/generate_policy_chunks.py --policy-no 정책번호
```

텍스트를 확인한 뒤 전체 정책을 적재합니다.

```bash
python etl/generate_policy_chunks.py --write
```

같은 명령을 다시 실행하면 내용과 생성기 버전이 같은 행은 건드리지 않습니다. 내용이 바뀐 청크만 갱신하고 기존 임베딩을 `NULL`로 만들어 다음 임베딩 대상에 포함합니다. 새 생성 결과에서 사라진 청크는 선택한 정책 범위 안에서 삭제합니다.

비활성 정책은 새 청크 생성 대상에서 제외하지만 기존 청크와 벡터는 감사·재활성화를 위해 보존합니다. 활성 여부만 바뀐 경우 청크 내용과 임베딩은 변경되지 않으므로 전체 임베딩 재생성이 필요하지 않습니다.

지역은 `v_policy_region_resolved`를 통해 현행 이름으로 해석하고, 모든 현행 지역이면 `전국`, 한 시도의 모든 지역이면 `{시도명} 전체`로 압축합니다. URL은 임베딩 텍스트에 넣지 않습니다.

## 13. 임베딩 생성과 pgvector 검색

상세 실행·재시도·상태 규칙은 [임베딩 및 pgvector 검색 파이프라인](docs/embedding-pipeline.md)을 참고합니다.

먼저 비용이 발생하지 않는 계획 모드로 대상 수와 예상 토큰을 확인합니다.

```bash
python etl/embed_policy_chunks.py
```

정책 데이터가 바뀌면 계획 모드로 대상과 토큰 수를 다시 확인해야 하며 실제 사용량과 요금은 API 응답 및 실행 시점 공식 가격을 기준으로 합니다. 비활성 정책의 기존 벡터는 보존되지만 새 임베딩 후보와 검색 결과에서는 제외됩니다.

개인 `.env`에 `OPENAI_API_KEY`를 설정한 뒤 소량부터 실행합니다.

```bash
python etl/embed_policy_chunks.py --write --limit 10
python etl/embed_policy_chunks.py --write
```

성공한 행은 같은 모델로 재실행해도 건너뜁니다. 실패 행은 오류 내용을 보존하며 다음 명령으로 명시적으로 재시도합니다.

```bash
python etl/embed_policy_chunks.py --write --retry-errors
```

임베딩이 적재된 후 자연어 질문으로 검색합니다.

```bash
python etl/search_policy_chunks.py "인천에서 받을 수 있는 청년 월세 지원" --limit 5
```

`--policy-no`를 반복 지정하면 사용자 프로필의 RDB 필터를 통과한 정책 안에서만 벡터 검색할 수 있습니다.

## 14. 검증 SQL

다음 SQL은 초기화한 `youthlink` DB에서 실행합니다.

### 확장과 테이블 확인

```sql
SELECT extname, extversion
FROM pg_extension
WHERE extname IN ('vector', 'pgcrypto');

SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_type = 'BASE TABLE'
ORDER BY table_name;
```

현재 `schema.sql`만 적용한 새 DB라면 웹 staging·fallback 계층을 포함한 16개 테이블과 검증 View 3개가 보입니다. 001~012 마이그레이션과 지역 적재를 모두 완료한 최종 상태에서는 **18개 테이블 + 6개 View**가 보여야 하고, `region_code_alias`는 없어야 합니다. View는 `v_policy_region_resolved`, `v_policy_region_scope`, `v_policy_sido_scope`, `v_verified_policy_web_document`, `v_verified_policy_enrichment`, `v_verified_policy_web_chunk`를 확인합니다.

### 공통 코드와 적재 건수

```sql
SELECT code_group, COUNT(*) AS code_count
FROM common_code
GROUP BY code_group
ORDER BY code_group;

SELECT 'policy' AS table_name, COUNT(*) AS row_count FROM policy
UNION ALL
SELECT 'policy_region', COUNT(*) FROM policy_region
UNION ALL
SELECT 'policy_eligibility_code', COUNT(*) FROM policy_eligibility_code;

SELECT is_active, COUNT(*) AS policy_count
FROM policy
GROUP BY is_active
ORDER BY is_active DESC;

SELECT status, api_total_count, processed_count, success_count, failed_count,
       deactivated_count, full_scan_completed, deactivation_applied,
       started_at, finished_at
FROM policy_sync_job
ORDER BY started_at DESC
LIMIT 10;

SELECT chunk_type, COUNT(*) AS chunk_count
FROM policy_chunk
GROUP BY chunk_type
ORDER BY MIN(chunk_index);

-- 모두 0을 기대
SELECT COUNT(*) AS invalid_hash_count
FROM policy_chunk
WHERE content_hash <> ENCODE(DIGEST(content, 'sha256'), 'hex');

SELECT COUNT(*) AS duplicate_chunk_count
FROM (
    SELECT policy_no, chunk_type
    FROM policy_chunk
    GROUP BY policy_no, chunk_type
    HAVING COUNT(*) > 1
) duplicated;

SELECT
    COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS embedded,
    COUNT(*) FILTER (
        WHERE embedding IS NULL AND embedding_error IS NULL
    ) AS pending,
    COUNT(*) FILTER (WHERE embedding_error IS NOT NULL) AS failed
FROM policy_chunk;

SELECT embedding_model, vector_dims(embedding), COUNT(*)
FROM policy_chunk
WHERE embedding IS NOT NULL
GROUP BY embedding_model, vector_dims(embedding);

SELECT policy_no, policy_name, min_age, max_age,
       source_updated_at, synced_at
FROM policy
ORDER BY synced_at DESC, policy_no
LIMIT 10;
```

연결 테이블은 한 정책에 여러 행이 들어가므로 정책 수와 같을 필요가 없습니다.

### 조건 코드 이름 확인 및 미등록 코드 탐지

```sql
WITH mapped AS (
    SELECT e.*,
           CASE e.condition_type
               WHEN 'MAJOR' THEN 'plcyMajorCd'
               WHEN 'JOB' THEN 'jobCd'
               WHEN 'SCHOOL' THEN 'schoolCd'
               WHEN 'SPECIAL' THEN 'sbizCd'
           END AS expected_group
    FROM policy_eligibility_code e
)
SELECT m.policy_no, m.condition_type, m.condition_code,
       c.code_name, (c.code IS NULL) AS missing_seed
FROM mapped m
LEFT JOIN common_code c
  ON c.code = m.condition_code
 AND c.code_group = m.expected_group
ORDER BY missing_seed DESC, m.policy_no
LIMIT 100;
```

`missing_seed=true`는 seed 보완 또는 원천 코드 확인 대상입니다.

### 중복과 데이터 품질 확인

```sql
-- 모두 0행을 기대: 현재 PK/UNIQUE 제약으로도 방지됨
SELECT policy_no, COUNT(*) FROM policy
GROUP BY policy_no HAVING COUNT(*) > 1;

SELECT policy_no, region_code, COUNT(*) FROM policy_region
GROUP BY policy_no, region_code HAVING COUNT(*) > 1;

SELECT policy_no, condition_type, condition_code, COUNT(*)
FROM policy_eligibility_code
GROUP BY policy_no, condition_type, condition_code
HAVING COUNT(*) > 1;

-- 원문은 있지만 유효 기간 행이 없는 정제 검토 대상
SELECT policy_no, policy_name, application_period_raw
FROM policy
WHERE application_period_raw IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM policy_application_period ap
      WHERE ap.policy_no = policy.policy_no
  )
LIMIT 30;

SELECT policy_no, policy_name
FROM policy
WHERE raw_data = '{}'::jsonb OR policy_name = '(제목 없음)'
LIMIT 30;
```

DB에 들어간 행만 검사하면 롤백된 실패 정책은 보이지 않습니다. 종료 요약과 JSONL 로그도 함께 확인하세요. 재실행 후 건수 비교는 같은 샘플을 사용하는 것이 좋고, 실제 API는 실행 사이에 원천 데이터가 바뀔 수 있습니다.

## 15. 현재까지 완료된 작업과 다음 단계

### 코드·설정으로 준비된 항목

- PostgreSQL 확장 설정, 기본 16개 테이블 정의, 지역 구성을 포함한 최종 18개 테이블 + 6개 View
- VWorld 시도·시군구 동기화와 `region` 지역 마스터 관리
- 과거·분할·상위 지역 코드의 1:N 매핑과 `v_policy_region_resolved` 제공
- 복수 신청기간의 정확한 행 저장과 더미·역전 날짜 안전 처리
- 인천 9개 코드 범위 누락 보정 및 전국·시도 전체 공용 판정 View
- 지역 동기화 후 미해결 온통청년 코드 자동 검증
- pgvector 1536차원 컬럼과 코사인 HNSW 인덱스 정의
- 공통 코드 seed와 재실행 가능한 코드 갱신
- 온통청년 API 페이지 요청, 데이터 정제, 원문 JSONB 보존
- 정책 UPSERT 및 지역·자격 조건 재동기화
- API 모드의 정책별 SAVEPOINT, 실패 JSONL 기록, 처리 결과 요약
- 전체 동기화 이력, 완전성 검증, 미수신 정책 소프트 비활성화와 자동 재활성화
- 비활성 정책을 제외하는 청크 생성·임베딩 후보·Vector Search
- API 키가 포함된 요청 URL을 숨기는 오류 정제와 제한적 Retry/Backoff
- 고정 총건수 상한 없이 페이지를 순회하는 수집
- 정책 변환·CLI·API 응답과 VWorld 필터/중복 처리 단위 테스트 및 CI
- `policy-v1` 청크 전략, 변경 감지 스키마, 결정적 청크 생성·동기화 CLI
- OpenAI 임베딩 계획·배치 적재·재시도·오류 기록 CLI
- 질문 임베딩과 pgvector cosine 검색, 정책 번호·청크 유형 후보 필터
- 활성 정책 URL 정규화·동기화와 `PENDING` staging 연결
- DNS·리다이렉트·robots.txt·응답 제한을 적용한 소량 HTML 수집기
- 정확한 Naver 게시물만 허용하고 본문 iframe 한 단계만 안전하게 수집하는 제한형 어댑터
- 결정적 정책-페이지 관련성 판정과 `VERIFIED` 전용 원문·보강 View
- exact document 버전에 고정되는 보수적 수동 검토 CLI와 출처-문서 복합 FK
- 검증 원문 전용 `policy_web_chunk` 생성·임베딩 및 RAG fallback View
- 샘플 파일 처리 함수, 환경변수 템플릿, 의존성 목록 및 Git 제외 규칙

2026-10-30 검증 기준 로컬 DB에는 정책 3,708건이 보존되어 있으며 3,664건은 활성, 원천에서 사라진 44건은 비활성 상태입니다. API 기반 임베딩 청크 14,647건은 그대로 유지됩니다. URL inventory는 활성 정책-URL 연결 4,497개와 고유 원문 2,970개입니다. 현재 원문 13건이 있고, 정책-URL 관계는 `VERIFIED` 4행, `LINK_ONLY` 14행, `REJECTED` 1행, `PENDING` 4,478행이며 미검토 `REVIEW_REQUIRED`는 0행입니다. 소스 상태는 `FETCHED` 13건, `UNSUPPORTED` 3건, `BLOCKED` 5건, `DISCOVERED` 2,949건입니다. 검증 원문 4건에서 API corpus와 분리된 웹 청크 6건을 만들었고 모두 `text-embedding-3-small` 1536차원 벡터로 저장했습니다.

같은 날 Naver 6건 파일럿에서는 숫자 게시물 URL 4건만 iframe 본문 수집에 성공했고, 게시물 ID가 없는 블로그 루트 2건은 `UNSUPPORTED`로 유지했습니다. 새 본문 4건은 모두 자동 승인 없이 검토했으며 광양 드림카페 1건만 `VERIFIED`, 완주 관광성 청년몰 글과 2024년 익산 모집 글은 `LINK_ONLY`, 대학리그와 다른 내용의 부산 이스포츠 글은 `REJECTED`로 판정했습니다. 승인된 광양 문서에서 웹 청크 2개를 만들고 총 3,462토큰을 임베딩했습니다. 이 과정에서 API `policy`와 `policy_chunk`는 수정하지 않았습니다.

