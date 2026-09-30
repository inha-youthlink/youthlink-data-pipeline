# YouthLink 데이터 파이프라인

온통청년 API의 청년 지원 정책을 수집·정제하여 PostgreSQL에 저장하는 YouthLink의 데이터 파이프라인입니다. 정책 검색과 맞춤형 추천에 필요한 데이터를 준비하고, 이후 pgvector 기반 RAG와 챗봇으로 연결하는 것을 목표로 합니다.

현재 ETL은 `policy`, `policy_region`, `policy_eligibility_code` 3개 테이블을 적재합니다. `common_code`는 SQL seed로 채우며, 임베딩과 채팅 관련 테이블은 스키마가 준비된 단계입니다.

> 작성 기준: 2026-09-30에 확인한 저장소 코드. 아래의 구현 현황은 코드 기준이며, 실제 API 전체 수집 성공이나 운영 DB 상태를 보증하는 실행 결과는 아닙니다. 샘플 실행 전에는 6절의 CLI 분기 수정 사항을 확인하세요.

## 1. 처음 참여한 팀원의 진행 순서

1. 저장소를 내려받고 Python 가상환경과 의존성을 설치합니다.
2. PostgreSQL과 pgvector를 준비하고 DB를 생성합니다.
3. 확장 → 스키마 → 공통 코드 seed 순서로 SQL을 실행합니다.
4. `.env.example`을 `.env`로 복사하여 로컬 설정을 입력합니다.
5. 샘플 JSON으로 적재를 확인한 뒤 실제 API 수집을 실행합니다.
6. 검증 SQL과 실패 로그를 확인합니다.

모든 명령은 **저장소 루트**에서 실행합니다. 처음에는 팀 공용 운영 DB 대신 개발용 DB를 사용하세요. 샘플 실행도 DB에 실제로 데이터를 저장합니다.

## 2. 프로젝트 구조

```text
youthlink-data-pipeline/
├── db/
│   ├── extensions.sql          # vector, pgcrypto 확장 활성화
│   ├── schema.sql              # 9개 테이블·제약조건·인덱스
│   └── seed/
│       └── common_code.sql     # API 코드 → 이름 매핑
├── docs/
│   └── erd.md                  # 전체 테이블 관계도
├── etl/
│   ├── load_policies.py        # API/JSON 추출·변환·적재
│   └── logs/                  # 실행 중 생성, Git 제외
│       └── failed_policies.jsonl
├── sample_response.json       # 로컬 적재 테스트용 응답
├── requirements.txt
├── .env.example               # 비밀값 없는 설정 템플릿
├── .env                       # 개인별 실제 설정, Git 제외
├── .gitignore
├── README_ETL.md              # 기존 ETL 설명
└── README.md
```

스키마의 기준은 [db/schema.sql](db/schema.sql), 상세 관계도는 [docs/erd.md](docs/erd.md), 변환·저장 동작의 기준은 [etl/load_policies.py](etl/load_policies.py)입니다.

## 3. PostgreSQL / pgvector와 9개 테이블

PostgreSQL에는 정책의 정형 조건과 API 원문(`JSONB`)을 함께 저장합니다. pgvector는 향후 정책 설명의 의미를 비교하는 벡터 검색에 사용합니다.

| 테이블 | 역할 | 현재 데이터 입력 방식 |
| --- | --- | --- |
| `common_code` | API 코드, 코드 그룹, 사람이 읽는 이름 | SQL seed |
| `policy` | 정책 번호·이름·지원 내용·나이·소득·기간·기관·URL·원문 | ETL UPSERT |
| `policy_region` | 정책별 대상 지역 코드. `(policy_no, region_code)`가 기본키 | ETL 재동기화 |
| `policy_eligibility_code` | 전공·취업·학력·특화 대상의 복수 조건 코드 | ETL 재동기화 |
| `policy_chunk` | 정책을 나눈 텍스트와 임베딩 | 스키마 준비, 생성 작업은 다음 단계 |
| `chat_room` | 대화방 | 스키마 준비 |
| `chat_profile` | 대화방별 사용자 나이·지역·소득 등 추천 조건 | 스키마 준비 |
| `chat_message` | 대화방의 사용자·챗봇 메시지 | 스키마 준비 |
| `message_policy_ref` | 메시지가 참조한 정책과 관련도 점수 | 스키마 준비 |

정책 하나에는 여러 지역·자격 조건·청크가 연결됩니다. 대화방에는 프로필 하나와 여러 메시지가 연결되며, 메시지와 정책은 `message_policy_ref`로 연결합니다. `common_code`는 코드 해석용 사전이며, 현재 정책의 코드 컬럼에 공통 코드 외래키를 강제하지는 않습니다.

`policy_chunk.embedding`은 `VECTOR(1536)`, 모델 기본값은 `text-embedding-3-small`입니다. 코사인 거리용 HNSW 인덱스가 정의되어 있습니다. **현재 ETL은 임베딩을 생성하지 않습니다.** 향후 모델이나 차원을 변경한다면 컬럼·인덱스·기존 임베딩의 재생성 계획을 함께 조정해야 합니다.

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

주요 의존성은 PostgreSQL 연결용 `psycopg[binary]`, HTTP 요청용 `requests`, `.env` 로딩용 `python-dotenv`입니다. PowerShell에서 활성화가 제한되면 `.\.venv\Scripts\python.exe`를 `python` 대신 사용해 설치·실행할 수 있습니다.

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

# PostgreSQL
DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/youthlink
```

| 변수 | 설명 |
| --- | --- |
| `YOUTH_API_URL` | 온통청년 정책 조회 API의 기본 요청 주소. 키가 포함된 전체 요청 URL을 넣지 않음 |
| `YOUTH_API_KEY` | 발급받은 인증키. 개인 `.env`에만 입력 |
| `YOUTH_API_PAGE_SIZE` | 페이지당 요청 건수. 기본 100, API 허용 범위 내의 양수 사용 |
| `DATABASE_URL` | 적재 대상 PostgreSQL 연결 문자열 |

`YOUR_PASSWORD`는 자리표시자입니다. 실제 DB 사용자와 비밀번호로 변경하고, 연결 문자열의 비밀번호에 `@`, `:`, `/` 등 특수문자가 있으면 URL 인코딩해야 합니다. Python 실행 시 `load_dotenv()`가 설정을 읽습니다. 일반 `psql` 명령은 이 `.env`를 자동으로 읽지 않습니다.

파일 모드는 DB 설정만 필요하고, API 모드는 네 항목을 모두 확인해야 합니다. 임베딩 API 키는 현재 정책 수집 ETL에 필요하지 않습니다.

## 6. 샘플 JSON 테스트

### 먼저 확인할 현재 코드의 실행 분기

작성 시점 `load_policies.py` 끝부분에서는 `elif args.command == "file"`이 API 분기의 실패 건수 조건 아래에 들어가 있어 파일 모드가 실행되지 않습니다. 아래처럼 `api`와 `file`을 같은 수준으로 두고, 기존 `if __name__ == "__main__":` 안에서 `args = parser.parse_args()` 다음에 배치해야 합니다.

```python
    if args.command == "api":
        success_count, failed_count = run_api_etl(args.page_size)
        print("========== ETL RESULT ==========")
        print(f"success : {success_count}")
        print(f"failed  : {failed_count}")
        print(f"total   : {success_count + failed_count}")
        print("================================")
        if failed_count > 0:
            print(f"failed log: {FAILED_LOG_PATH}")
    elif args.command == "file":
        run_json_file(args.path)
```

수정 후 다음 명령으로 로컬 JSON을 적재합니다.

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
3. **Load:** `load_policy()`가 정책과 두 연결 테이블을 같은 정책 단위 트랜잭션에서 갱신합니다.

| API 필드 | 저장 위치·변환 |
| --- | --- |
| `plcyNo`, `plcyNm` | `policy_no`, `policy_name`. 정책 번호 누락은 오류, 이름 누락은 `(제목 없음)` |
| `plcyExplnCn`, `plcySprtCn` | 정책 설명, 지원 내용 |
| `plcyKywdNm`, `lclsfNm`, `mclsfNm` | 쉼표를 분리한 `TEXT[]` |
| `sprtTrgtMinAge`, `sprtTrgtMaxAge` | 정수, 0 이하·변환 불가 값은 `NULL` |
| `aplyYmd` | `YYYYMMDD ~ YYYYMMDD` 형태의 신청 기간을 날짜로 변환하고 원문도 보존 |
| `zipCd` | 지역 코드별 `policy_region` 행 |
| `plcyMajorCd`, `jobCd`, `schoolCd`, `sbizCd` | 각각 `MAJOR`, `JOB`, `SCHOOL`, `SPECIAL` 조건 행 |
| 정책 객체 전체 | `policy.raw_data`에 `JSONB`로 보존 |

빈 문자열은 기본적으로 `NULL`, `Y/N`은 불리언으로 변환합니다. 파싱하지 못한 날짜는 `NULL`이 될 수 있으므로 적재 성공만으로 정제 품질이 보장되지는 않습니다. 현재 `normalize_age_range()`는 정의만 되어 있고 변환에서 호출하지 않아, 양수 나이가 역전된 경우에는 DB 제약조건으로 실패합니다.

## 8. 실패 레코드 로깅과 계속 진행

API 모드는 **페이지 단위 트랜잭션 안에 정책별 SAVEPOINT**를 둡니다. 정책 저장 중 오류가 나면 그 정책의 변경만 롤백하고, 실패 내용을 기록한 뒤 다음 정책을 처리합니다. 지역·자격 조건을 갱신하다 실패해도 해당 정책의 기존 데이터까지 부분적으로 바뀐 상태로 남지 않도록 같은 SAVEPOINT로 묶습니다.

단순 `try/except`만으로는 PostgreSQL의 실패한 트랜잭션 상태를 복구할 수 없습니다. 현재 코드는 중첩된 `conn.transaction()`을 이용해 SAVEPOINT를 만들고 복구합니다. 성공한 정책들은 바깥 페이지 트랜잭션이 커밋되어야 최종 반영됩니다.

실패 로그는 `etl/logs/failed_policies.jsonl`에 UTF-8로 추가됩니다. **한 줄에 JSON 객체 하나**이며 실행할 때마다 누적됩니다. 다음은 가상 정책으로 만든 형식 예시입니다.

```json
{"timestamp":"2026-09-30T10:00:00","page_num":1,"policy_no":"EXAMPLE_POLICY","policy_name":"예시 정책","error_type":"CheckViolation","error_message":"나이 범위 제약조건 위반","raw_data":{"plcyNo":"EXAMPLE_POLICY","sprtTrgtMinAge":"30","sprtTrgtMaxAge":"20"}}
```

로그의 정책 번호·오류 종류·원문을 확인하고 변환 규칙을 수정한 뒤 재실행합니다. 실패 로그 자체는 일반 JSON 배열이 아니며, 현재 `file` 명령에 JSONL 파일을 직접 전달하는 재처리 기능은 없습니다.

주의할 현재 동작:

- HTTP 오류·타임아웃·JSON 응답 파싱 오류는 정책 처리 루프 밖에서 발생하여 실행을 중단할 수 있습니다. 자동 재시도는 아직 없습니다.
- DB 연결 장애, 페이지 커밋 실패, 로그 파일 쓰기 실패도 계속 진행을 보장하지 않습니다.
- API 오류 응답에 정책 목록이 없으면 빈 목록으로 인식할 수 있습니다. 업무 오류 코드와 응답 구조 검증은 보완해야 합니다.
- 종료 요약의 `success`는 신규 삽입과 기존 갱신을 합한 처리 건수이며 DB 전체 행 수와 다를 수 있습니다. 처리 실패 건이 있어도 현재 CLI는 별도의 실패 종료 코드를 설정하지 않습니다.

## 9. UPSERT와 주기 동기화 전략

`policy_no`를 기준으로 신규 정책은 INSERT, 기존 정책은 UPDATE합니다. 기존 정책의 수집 대상 컬럼과 `raw_data`를 최신 응답으로 교체하고 `synced_at`을 갱신합니다. `source_updated_at`은 원천 API의 수정 시각이고, `synced_at`은 DB 동기화 시각입니다.

`policy_region`과 `policy_eligibility_code`는 해당 정책의 기존 연결을 삭제한 뒤 현재 응답으로 다시 삽입합니다. 복합 기본키·UNIQUE 제약과 충돌 처리를 통해 반복 실행 시 중복을 방지합니다. 다만 실행마다 동기화 시각과 연결 행의 내부 ID는 바뀔 수 있습니다.

현재는 CLI를 수동 재실행하는 구조이며, 스케줄러는 구현되지 않았습니다. 다음 운영 단계는 다음과 같습니다.

1. API 호출 한도와 필요한 최신성을 기준으로 동기화 주기를 정합니다.
2. 작업 스케줄러 또는 CI에서 실행하고 동시 중복 실행을 막습니다.
3. 실행 ID, 시작·종료 시각, 처리·실패 건수, 완료 상태를 기록하고 실패를 알립니다.
4. 네트워크 오류 재시도, 대기 시간 증가, 비정상 반복 페이지 탐지를 추가합니다.
5. 정책 변경 시 관련 청크·임베딩을 갱신하는 흐름을 연결합니다.

**API에서 사라진 정책은 현재 DB에서 자동 삭제되지 않습니다.** 향후 `is_active`, 실행별 마지막 확인 정보 등을 도입할 수 있습니다. 누락 정책 비활성화는 전체 페이지를 정상 수집한 실행에만 적용해야 하며, 중간 실패나 일시적인 API 누락으로 대량 비활성화하지 않도록 설계해야 합니다. 이 컬럼과 처리는 아직 구현되지 않았습니다.

## 10. 검증 SQL

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

전용 새 DB라면 위 9개 테이블이 보여야 합니다.

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

-- 정제 검토 대상: 반드시 오류라는 뜻은 아님
SELECT policy_no, policy_name, application_period_raw
FROM policy
WHERE application_period_raw IS NOT NULL
  AND (application_start_date IS NULL OR application_end_date IS NULL)
LIMIT 30;

SELECT policy_no, policy_name
FROM policy
WHERE raw_data = '{}'::jsonb OR policy_name = '(제목 없음)'
LIMIT 30;
```

DB에 들어간 행만 검사하면 롤백된 실패 정책은 보이지 않습니다. 종료 요약과 JSONL 로그도 함께 확인하세요. 재실행 후 건수 비교는 같은 샘플을 사용하는 것이 좋고, 실제 API는 실행 사이에 원천 데이터가 바뀔 수 있습니다.

## 11. 현재까지 완료된 작업과 다음 단계

### 코드·설정으로 준비된 항목

- PostgreSQL 확장 설정 및 9개 테이블·관계·제약조건·인덱스 정의
- pgvector 1536차원 컬럼과 코사인 HNSW 인덱스 정의
- 공통 코드 seed와 재실행 가능한 코드 갱신
- 온통청년 API 페이지 요청, 데이터 정제, 원문 JSONB 보존
- 정책 UPSERT 및 지역·자격 조건 재동기화
- API 모드의 정책별 SAVEPOINT, 실패 JSONL 기록, 처리 결과 요약
- 고정 총건수 상한 없이 페이지를 순회하는 수집
- 샘플 파일 처리 함수, 환경변수 템플릿, 의존성 목록 및 Git 제외 규칙

### 다음 단계

- **우선:** 6절의 CLI 분기를 수정하고 샘플 → 재실행 → 실제 API 순서로 DB 검증
- 파일 모드에도 정책별 실패 처리 적용 여부 결정
- 나이 역전·소득·비정형 기간·미등록 코드의 정제 기준 보완
- API 업무 오류 검사, 재시도, 실행 이력과 종료 상태 관리
- 주기 실행과 동시 실행 방지, 누락 정책 상태 관리
- 정책 청크 생성·임베딩 적재·벡터 검색 및 갱신 연결
- 사용자 조건 필터링, RAG 응답, 정책 출처 표시, 채팅 저장 구현
- 스키마 마이그레이션과 핵심 변환·재실행·롤백 동작의 자동 검증

## 12. Git 보안 및 협업 주의사항

`.env.example`은 팀원이 필요한 변수 이름을 알 수 있도록 커밋하되, 실제 API 키·DB 비밀번호는 `.env`에만 넣습니다. 현재 `.gitignore`의 주요 제외 항목은 다음과 같습니다.

```gitignore
.venv/
.env
__pycache__/
*.pyc
etl/logs/
```

커밋 전 변경 내용을 확인합니다.

```bash
git status
git diff --cached
git check-ignore .env etl/logs/failed_policies.jsonl
```

API 키가 들어간 요청 URL, DB 접속 문자열, 개인 설정 파일, DB 덤프, 실패 로그를 올리지 마세요. 오류 메시지·원문 데이터·스크린샷에도 민감한 값이 포함될 수 있으므로 공유 전 확인합니다. `.env.local` 등 추가 설정 파일을 만든다면 해당 파일도 제외 규칙에 추가합니다.

이미 추적 중인 파일은 `.gitignore`를 추가해도 추적이 해제되지 않습니다. 비밀값이 커밋되었다면 파일 삭제만으로 해결되지 않으므로 먼저 키·비밀번호를 폐기하거나 교체하고 팀과 기록 정리를 진행합니다.

원격 변경 때문에 push가 거절되면 강제 push로 덮어쓰지 않습니다. 진행 중인 rebase와 미커밋 변경이 없는지 확인한 후 원격 변경을 통합합니다.

```bash
git status
git pull --rebase origin main
```

충돌이 있으면 내용을 확인해 해결하고 `git add <파일>` → `git rebase --continue` 순서로 진행합니다. `.env.example`이 실수로 삭제된 충돌이라면 **비밀값 없는 템플릿**을 복원해 추가합니다. 변경 검토와 검증을 마친 뒤 팀의 브랜치·PR 규칙에 따라 공유하세요.
