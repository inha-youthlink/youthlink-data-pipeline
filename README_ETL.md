# YouthLink ETL prototype

온통청년 API의 `youthPolicyList`를 PostgreSQL의 다음 테이블로 적재합니다.

- `policy`
- `policy_region`
- `policy_eligibility_code`

## 1. 설치

```bash
python -m venv .venv
```

Git Bash:

```bash
source .venv/Scripts/activate
```

PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

의존성 설치:

```bash
pip install -r requirements.txt
```

## 2. 환경변수

`.env.example`을 `.env`로 복사한 뒤 실제 값을 넣습니다.

```bash
cp .env.example .env
```

예:

```dotenv
YOUTH_API_URL=https://...
YOUTH_API_KEY=...
YOUTH_API_PAGE_SIZE=100

DATABASE_URL=postgresql://postgres:비밀번호@localhost:5432/youthlink
```

`.env`는 Git에 올리지 마세요.

## 3. 먼저 로컬 JSON으로 테스트

실제 API를 호출하기 전에 저장해 둔 응답 JSON으로 ETL을 테스트할 수 있습니다.

```bash
python etl/load_policies.py file sample_response.json
```

## 4. 실제 API 전체 수집

```bash
python etl/load_policies.py api
```

## 5. 적재 확인

PostgreSQL에서:

```sql
SELECT COUNT(*) FROM policy;
SELECT COUNT(*) FROM policy_region;
SELECT COUNT(*) FROM policy_eligibility_code;

SELECT policy_no, policy_name, min_age, max_age
FROM policy
ORDER BY source_updated_at DESC NULLS LAST
LIMIT 10;
```

## 현재 변환 규칙

### policy

- `plcyNo` → `policy_no`
- `plcyNm` → `policy_name`
- `plcyExplnCn` → `description`
- `plcySprtCn` → `support_content`
- `plcyKywdNm` → `keywords TEXT[]`
- `lclsfNm` → `large_categories TEXT[]`
- `mclsfNm` → `middle_categories TEXT[]`
- `sprtTrgtMinAge` / `sprtTrgtMaxAge` → 정수
- `aplyYmd` → 신청 시작일/종료일 + 원문
- `raw_data` → API 정책 객체 전체 보존

### policy_region

`zipCd`를 쉼표로 나누어 한 코드당 한 행으로 적재합니다.

### policy_eligibility_code

- `plcyMajorCd` → `MAJOR`
- `jobCd` → `JOB`
- `schoolCd` → `SCHOOL`
- `sbizCd` → `SPECIAL`

쉼표가 있으면 여러 행으로 분리합니다.

## 재실행

`policy`는 `policy_no` 기준 UPSERT 합니다.

`policy_region`, `policy_eligibility_code`는 해당 정책의 기존 행을 삭제한 뒤
현재 API 값으로 다시 삽입합니다. 따라서 같은 데이터를 반복 실행해도
중복 행이 쌓이지 않습니다.
