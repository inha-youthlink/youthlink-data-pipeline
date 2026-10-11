# YouthLink 데이터 사전

작성 기준: 2026-10-11

이 문서는 YouthLink PostgreSQL 스키마의 테이블과 컬럼 의미를 설명합니다. 테이블 간 관계를 그림으로 확인하려면 [erd.md](erd.md)를 참고하세요. 실제 정의의 기준은 `db/schema.sql`과 `db/migrations/`입니다.

## 읽는 방법

- **한 행의 의미**는 해당 테이블의 튜플(row) 하나가 무엇을 나타내는지 설명합니다.
- `PK`는 기본키, `FK`는 외래키, `UNIQUE`는 중복 방지 제약조건입니다.
- 외부 API 코드에는 의도적으로 FK를 강제하지 않은 경우가 있습니다. 신규·과거 코드 때문에 정책 수집 전체가 실패하지 않게 하기 위함입니다.
- 최종 프로젝트 구조는 12개 테이블과 3개 조회용 View입니다.

## 전체 구성

| 영역 | 객체 | 역할 |
| --- | --- | --- |
| 정책 | `policy` | 온통청년 정책 본문과 정형 조건 |
| 정책 | `policy_application_period` | 정책별 정확한 단일·복수 신청 기간 |
| 정책 | `policy_region` | 정책과 온통청년 원본 지역코드 연결 |
| 정책 | `policy_eligibility_code` | 전공·직업·학력·특화 조건 |
| 정책 | `common_code` | 온통청년 코드 이름 사전 |
| 지역 | `region` | VWorld 기준 현재 시군구 마스터 |
| 지역 | `region_code_mapping` | 과거·상위 지역코드를 현재 코드로 변환 |
| RAG | `policy_chunk` | 정책 청크와 임베딩 |
| 채팅 | `chat_room` | 대화방 |
| 채팅 | `chat_profile` | 대화방별 사용자 프로필 |
| 채팅 | `chat_message` | 대화 메시지 |
| 채팅 | `message_policy_ref` | 메시지와 근거 정책 연결 |
| 조회 | `v_policy_region_resolved` | 정책 지역코드를 현재 지역으로 해석하는 View |
| 조회 | `v_policy_region_scope` | 정책의 전국 범위를 공통 판정하는 View |
| 조회 | `v_policy_sido_scope` | 정책별 시도 전체 범위를 판정하는 View |

---

## `policy`

**한 행의 의미:** 온통청년 정책 하나.

`policy_no`는 API의 `plcyNo`를 그대로 사용하는 자연키입니다. 별도의 내부 정책 ID는 사용하지 않습니다.

### 기본 정보

| 컬럼 | 타입 | 용도 | 원본 API 필드 |
| --- | --- | --- | --- |
| `policy_no` | `VARCHAR(30)` | 정책 식별자, PK | `plcyNo` |
| `policy_name` | `VARCHAR(300)` | 정책명 | `plcyNm` |
| `description` | `TEXT` | 정책 설명 | `plcyExplnCn` |
| `support_content` | `TEXT` | 구체적인 지원 내용 | `plcySprtCn` |
| `keywords` | `TEXT[]` | 검색용 정책 키워드 | `plcyKywdNm` |
| `large_categories` | `TEXT[]` | 정책 대분류 이름 | `lclsfNm` |
| `middle_categories` | `TEXT[]` | 정책 중분류 이름 | `mclsfNm` |

### 제공 방식과 기관

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `provider_group_code` | `VARCHAR(30)` | 제공기관 그룹 코드 |
| `provision_method_code` | `VARCHAR(30)` | 프로그램·보조금·대출 등 제공 방식 코드 |
| `approval_status_code` | `VARCHAR(30)` | 정책 승인 상태 코드 |
| `supervising_org_code` | `VARCHAR(30)` | 주관기관 코드 |
| `supervising_org_name` | `VARCHAR(200)` | 주관기관명 |
| `supervising_manager_name` | `VARCHAR(100)` | 주관기관 담당자명 |
| `operating_org_code` | `VARCHAR(30)` | 운영기관 코드 |
| `operating_org_name` | `VARCHAR(200)` | 운영기관명 |
| `operating_manager_name` | `VARCHAR(100)` | 운영기관 담당자명 |

코드의 표시 이름은 `common_code`에서 확인할 수 있습니다. 외부 신규 코드 수집을 막지 않기 위해 FK는 강제하지 않습니다.

### 지원 규모와 대상 조건

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `support_scale_limit` | `BOOLEAN` | 지원 규모 제한 여부 |
| `support_scale_count` | `INTEGER` | 지원 가능 인원 또는 규모 |
| `first_come_first_served` | `BOOLEAN` | 선착순 여부 |
| `min_age` | `INTEGER` | 최소 지원 나이 |
| `max_age` | `INTEGER` | 최대 지원 나이 |
| `age_limit_yn` | `BOOLEAN` | 나이 제한 여부 |
| `marriage_status_code` | `VARCHAR(30)` | 기혼·미혼·제한 없음 등 혼인 조건 |
| `income_condition_code` | `VARCHAR(30)` | 소득 조건 유형 |
| `min_income` | `BIGINT` | 최소 소득 조건 |
| `max_income` | `BIGINT` | 최대 소득 조건 |
| `income_etc` | `TEXT` | 숫자로 표현하기 어려운 추가 소득 조건 |

나이의 `0` 이하는 미설정으로 보고 `NULL`로 저장합니다. 양수 최소 나이가 최대 나이보다 크면 최소 나이를 보존하고 최대 나이를 `NULL`로 정규화합니다.

### 신청·사업 기간

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `application_period_type_code` | `VARCHAR(30)` | 특정 기간·상시·마감 등 신청 기간 유형 |
| `application_start_date` | `DATE` | 신뢰 가능한 단일 신청기간의 시작일. 복수·오류 기간은 `NULL` |
| `application_end_date` | `DATE` | 신뢰 가능한 단일 신청기간의 종료일. 복수·오류 기간은 `NULL` |
| `application_period_raw` | `TEXT` | API 신청 기간 원문 |
| `application_period_parse_status` | `VARCHAR(20)` | `EMPTY`, `SINGLE`, `MULTIPLE`, `RAW`, `INVALID` 파싱 상태 |
| `business_period_type_code` | `VARCHAR(30)` | 사업 기간 유형 |
| `business_start_date` | `DATE` | 사업 시작일 |
| `business_end_date` | `DATE` | 사업 종료일 |
| `business_period_etc` | `TEXT` | 별도 사업 기간 설명 |

날짜로 파싱할 수 없는 표현과 상시 모집 같은 비정형 정보를 잃지 않도록 원문을 함께 보존합니다. 정확한 단일·복수 기간 조회에는 `policy_application_period`를 사용합니다. 리터럴 `\\N`과 실제 줄바꿈은 복수 기간 구분자로 인식하며, 1900년 미만 더미 날짜와 역전된 범위는 `INVALID`로 표시하고 기간 행과 RAG 기간 텍스트를 만들지 않습니다. 날짜 형식이 아닌 설명형 원문은 `RAW`로 구분하여 보존·표시할 수 있습니다.

### 신청·심사·안내

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `application_method` | `TEXT` | 신청 방법 |
| `screening_method` | `TEXT` | 선정 및 심사 방법 |
| `submission_documents` | `TEXT` | 제출 서류 |
| `additional_notes` | `TEXT` | 기타 안내사항 |
| `additional_qualification` | `TEXT` | 추가 신청 자격 |
| `participation_exclusion` | `TEXT` | 참여 제외 대상 |
| `application_url` | `TEXT` | 신청 페이지 URL |
| `reference_url_1` | `TEXT` | 참고 URL 1 |
| `reference_url_2` | `TEXT` | 참고 URL 2 |
| `registrar_org_code` | `VARCHAR(30)` | 정책 등록기관 코드 |
| `registrar_org_name` | `VARCHAR(200)` | 정책 등록기관명 |

### 원천 추적과 동기화

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `source_created_at` | `TIMESTAMPTZ` | 온통청년 최초 등록 시각 |
| `source_updated_at` | `TIMESTAMPTZ` | 온통청년 마지막 수정 시각 |
| `synced_at` | `TIMESTAMPTZ` | YouthLink DB 마지막 동기화 시각 |
| `raw_data` | `JSONB` | API 정책 객체 전체 원문 |

`raw_data`는 파싱 규칙 변경, 누락 필드 재처리, 오류 조사에 사용합니다.

### 주요 제약조건

- `policy_no` PK
- 최소 나이 ≤ 최대 나이
- 최소 소득 ≤ 최대 소득
- 신청 시작일 ≤ 신청 종료일
- 사업 시작일 ≤ 사업 종료일

---

## `policy_application_period`

**한 행의 의미:** 정책 하나의 신청 가능 기간 하나.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `policy_no` | `VARCHAR(30)` | 대상 정책, `policy` FK이자 복합 PK |
| `period_index` | `INTEGER` | API 원문에 등장한 기간 순서, 0부터 시작하는 복합 PK |
| `start_date` | `DATE` | 해당 신청 기간 시작일 |
| `end_date` | `DATE` | 해당 신청 기간 종료일 |

기간 사이의 공백을 신청 가능으로 잘못 판단하지 않도록 복수 기간을 합치지 않습니다. 현재 날짜가 어느 신청기간에 포함되는지는 다음처럼 조회합니다.

```sql
SELECT DISTINCT policy_no
FROM policy_application_period
WHERE CURRENT_DATE BETWEEN start_date AND end_date;
```

정책 삭제 시 기간 행도 삭제됩니다. `period_index >= 0`, 시작·종료일은 1900-01-01 이상, 시작일은 종료일 이하여야 합니다.

---

## `policy_region`

**한 행의 의미:** 정책 하나가 온통청년 원본 지역코드 하나를 대상으로 한다는 관계.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `policy_no` | `VARCHAR(30)` | 대상 정책, `policy` FK |
| `region_code` | `VARCHAR(20)` | API에서 받은 원본 `zipCd` |

### 키와 사용 주의

- PK: `(policy_no, region_code)`
- 정책 삭제 시 관련 행도 삭제됩니다.
- `region_code`는 원본 보존 컬럼이므로 `region` FK가 아닙니다.
- 백엔드와 RAG의 현재 지역 필터에는 가능하면 `v_policy_region_resolved`를 사용합니다.

예:

```text
P001 | 28260
```

이는 `P001` 정책의 원본 API 지역코드가 `28260`이라는 뜻이며, 현재 지역 해석 결과를 의미하지는 않습니다.

---

## `region`

**한 행의 의미:** VWorld 기준 현재 시군구 하나.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `region_code` | `VARCHAR(5)` | 5자리 현재 시군구 코드, PK |
| `sido_code` | `VARCHAR(2)` | 앞 2자리 시도 코드 |
| `sigungu_code` | `VARCHAR(3)` | 뒤 3자리 시군구 코드 |
| `sido_name` | `VARCHAR(50)` | 시도명 |
| `sigungu_name` | `VARCHAR(50)` | 시군구명 |
| `region_name` | `VARCHAR(100)` | `시도명 + 시군구명` 전체 이름 |
| `is_active` | `BOOLEAN` | 최신 VWorld 조회 기준 활성 여부 |
| `source` | `VARCHAR(50)` | 지역 데이터 출처, 기본 `VWORLD` |
| `created_at` | `TIMESTAMPTZ` | 최초 적재 시각 |
| `updated_at` | `TIMESTAMPTZ` | 마지막 갱신 시각 |

`is_active = false`는 이전에는 존재했지만 최신 VWorld 동기화에서 조회되지 않았다는 의미입니다.

---

## `region_code_mapping`

**한 행의 의미:** 과거·폐지·상위 지역코드 하나를 현재 지역코드 하나로 해석하는 관계.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `source_code` | `VARCHAR(5)` | 원본 과거·상위 지역코드 |
| `canonical_code` | `VARCHAR(5)` | 현재 `region.region_code` FK |
| `mapping_type` | `VARCHAR(20)` | 코드 관계 유형 |
| `valid_from` | `DATE` | 매핑 유효 시작일 |
| `valid_to` | `DATE` | 매핑 유효 종료일 |
| `reason` | `TEXT` | 행정구역 변경 사유 |
| `created_at` | `TIMESTAMPTZ` | 매핑 등록 시각 |

### 키와 매핑 유형

- PK: `(source_code, canonical_code)`
- 한 원본 코드에 여러 행을 저장하여 1:N 분할을 표현합니다.

| `mapping_type` | 의미 |
| --- | --- |
| `REPLACED` | 옛 코드가 새 코드 하나로 대체 |
| `SPLIT` | 옛 지역 하나가 여러 지역으로 분리 |
| `MERGED` | 여러 옛 지역이 현재 지역 하나로 통합 |
| `PARENT` | 원본 코드가 여러 하위 지역을 포함하는 상위 범위 |

예:

```text
28260 | 28275 | SPLIT
28260 | 28290 | SPLIT
```

---

## `policy_eligibility_code`

**한 행의 의미:** 정책 하나가 자격 조건 코드 하나를 갖는 관계.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `id` | `BIGSERIAL` | 내부 행 식별용 PK |
| `policy_no` | `VARCHAR(30)` | 대상 정책, `policy` FK |
| `condition_type` | `VARCHAR(20)` | 조건 종류 |
| `condition_code` | `VARCHAR(30)` | 온통청년 조건 코드 |

### 조건 종류

| 값 | 의미 | 원본 API 필드 |
| --- | --- | --- |
| `MAJOR` | 전공 조건 | `plcyMajorCd` |
| `JOB` | 취업·직업 상태 | `jobCd` |
| `SCHOOL` | 학력·재학 상태 | `schoolCd` |
| `SPECIAL` | 특화 대상 | `sbizCd` |

`(policy_no, condition_type, condition_code)`는 UNIQUE입니다. 코드의 표시 이름은 `common_code`에서 찾습니다.

---

## `common_code`

**한 행의 의미:** 온통청년 API 코드 하나와 사람이 읽을 수 있는 이름.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `code` | `VARCHAR(30)` | API 코드, PK |
| `code_group` | `VARCHAR(50)` | 코드가 속한 API 필드·그룹 |
| `code_name` | `VARCHAR(100)` | 사람이 읽을 수 있는 코드명 |

예:

```text
0049005 | schoolCd | 대학 재학
```

RAG 문맥이나 UI에는 숫자 코드보다 `code_name`을 사용합니다. 외부 신규 코드가 아직 seed에 없어도 정책 ETL은 계속 수행됩니다.

---

## `policy_chunk`

**한 행의 의미:** 정책 하나에서 생성된 RAG용 의미 단위 텍스트 조각 하나.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `chunk_id` | `UUID` | 청크 식별자, PK |
| `policy_no` | `VARCHAR(30)` | 원본 정책, `policy` FK |
| `chunk_index` | `INTEGER` | 정책 내부 청크 순서 |
| `chunk_type` | `VARCHAR(30)` | 청크 의미 유형 |
| `content` | `TEXT` | 임베딩과 검색에 사용할 정제 텍스트 |
| `content_hash` | `VARCHAR(64)` | UTF-8 `content`의 SHA-256 소문자 hex, 변경 감지 기준 |
| `chunker_version` | `VARCHAR(50)` | 텍스트 생성 규칙 버전. 현재 `policy-v1` |
| `token_count` | `INTEGER` | 실제 임베딩 입력 토큰 수. 임베딩 전에는 `NULL` |
| `embedding` | `VECTOR(1536)` | 의미 검색용 임베딩 벡터 |
| `embedding_model` | `VARCHAR(100)` | 실제 임베딩 생성 모델명. 임베딩 전에는 `NULL` |
| `embedded_at` | `TIMESTAMPTZ` | 임베딩 생성 완료 시각 |
| `embedding_error` | `TEXT` | 마지막 임베딩 실패 내용. 성공 전까지 재처리 판단에 사용 |
| `created_at` | `TIMESTAMPTZ` | 청크 생성 시각 |
| `updated_at` | `TIMESTAMPTZ` | 청크 내용이나 생성 규칙이 마지막으로 바뀐 시각 |

### 현재 상태와 제약조건

- 정책 삭제 시 관련 청크도 삭제됩니다.
- `chunk_index >= 0`
- `(policy_no, chunk_index)` UNIQUE
- `(policy_no, chunk_type)` UNIQUE
- `chunk_type`은 `SUMMARY`, `ELIGIBILITY`, `SUPPORT`, `APPLICATION` 중 하나입니다.
- `content_hash`는 64자리 소문자 SHA-256 형식이고 빈 `content`는 허용하지 않습니다.
- 임베딩 전에는 `embedding`, `embedding_model`, `embedded_at`이 모두 `NULL`이고, 성공 후에는 셋 모두 값이 있어야 합니다.
- `embedding IS NOT NULL`인 행에 cosine HNSW 인덱스가 적용됩니다.
- 사용할 모델은 `text-embedding-3-small`, 벡터 차원은 1536입니다.
- `etl/generate_policy_chunks.py`가 `policy-v1` 텍스트를 생성하고, `etl/embed_policy_chunks.py`가 명시적인 `--write` 실행에서 임베딩을 적재합니다.

청크별 포함 필드, 지역 압축, 정제 및 변경 감지 규칙은 [RAG 정책 Chunk 생성 전략](rag-chunk-strategy.md)을 기준으로 합니다.

---

## `chat_room`

**한 행의 의미:** 사용자와 챗봇의 대화 세션 하나.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `room_id` | `UUID` | 대화방 식별자, PK |
| `title` | `VARCHAR(200)` | 대화방 제목 |
| `created_at` | `TIMESTAMPTZ` | 생성 시각 |
| `updated_at` | `TIMESTAMPTZ` | 마지막 갱신 시각 |

---

## `chat_profile`

**한 행의 의미:** 대화방 하나에서 사용하는 사용자 맞춤 추천 프로필.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `room_id` | `UUID` | 대상 대화방, PK이자 `chat_room` FK |
| `age` | `INTEGER` | 사용자 나이 |
| `gender` | `VARCHAR(20)` | 성별 |
| `region_code` | `VARCHAR(20)` | 사용자 지역코드 |
| `marriage_status_code` | `VARCHAR(30)` | 혼인 상태 코드 |
| `annual_income` | `BIGINT` | 연소득 |
| `major_code` | `VARCHAR(30)` | 전공 코드 |
| `job_code` | `VARCHAR(30)` | 직업 상태 코드 |
| `school_code` | `VARCHAR(30)` | 학력 코드 |
| `special_target_code` | `VARCHAR(30)` | 특화 대상 코드 |
| `extra_profile` | `JSONB` | 정형 컬럼에 없는 추가 프로필 |
| `updated_at` | `TIMESTAMPTZ` | 마지막 갱신 시각 |

`room_id`가 PK이므로 대화방 하나에는 프로필이 최대 하나 존재합니다. 이 정보는 Vector Search 전에 RDB 후보 정책을 좁히는 데 사용합니다.

---

## `chat_message`

**한 행의 의미:** 대화방 안의 메시지 하나.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `message_id` | `BIGSERIAL` | 메시지 식별자, PK |
| `room_id` | `UUID` | 메시지가 속한 대화방, `chat_room` FK |
| `role` | `VARCHAR(20)` | 메시지 작성 주체 |
| `content` | `TEXT` | 메시지 본문 |
| `created_at` | `TIMESTAMPTZ` | 메시지 생성 시각 |

`role`은 `user`, `assistant`, `system`, `tool`만 허용합니다.

---

## `message_policy_ref`

**한 행의 의미:** 메시지 하나가 답변 근거로 정책 하나를 참조했다는 관계.

| 컬럼 | 타입 | 용도 |
| --- | --- | --- |
| `message_id` | `BIGINT` | 정책을 참조한 메시지, `chat_message` FK |
| `policy_no` | `VARCHAR(30)` | 참조된 정책, `policy` FK |
| `relevance_score` | `DOUBLE PRECISION` | 검색 또는 추천 관련도 점수 |

### 키와 활용

- PK: `(message_id, policy_no)`
- `relevance_score`는 `-1.0`에서 `1.0` 사이 또는 `NULL`
- 답변 근거 표시, 정책 링크 제공, 검색 품질 분석에 사용합니다.

---

## `v_policy_region_resolved` View

**한 행의 의미:** 정책의 원본 지역코드 하나가 현재 지역코드 하나로 해석된 결과이거나, 검증된 코드 집합 규칙으로 보정된 현재 지역 하나.

이 객체는 실제 데이터를 별도로 저장하는 테이블이 아니라 `policy_region`, `region_code_mapping`, `region`을 결합한 조회용 View입니다.

| 컬럼 | 용도 |
| --- | --- |
| `policy_no` | 정책 번호 |
| `source_region_code` | 온통청년 원본 지역코드. `INFERRED` 행은 대응하는 단일 원본 코드가 없어 `NULL` |
| `resolved_region_code` | 해석된 현재 지역코드 |
| `sido_code` | 현재 시도 코드 |
| `sigungu_code` | 현재 시군구 코드 |
| `sido_name` | 현재 시도명 |
| `sigungu_name` | 현재 시군구명 |
| `region_name` | 사람이 읽을 수 있는 현재 전체 지역명 |
| `is_active` | VWorld 기준 현재 활성 여부 |
| `resolution_type` | 직접·매핑·범위 보정·미해결 구분 |
| `mapping_type` | 지역코드 매핑 유형 |
| `valid_from` | 매핑 유효 시작일 |
| `valid_to` | 매핑 유효 종료일 |

### 해석 유형

| `resolution_type` | 의미 |
| --- | --- |
| `DIRECT` | 원본 코드가 현재 지역코드와 직접 일치 |
| `MAPPED` | `region_code_mapping`을 거쳐 현재 코드로 해석 |
| `INFERRED` | 검증된 원본 코드 집합 규칙으로 누락된 현재 지역을 보정 |
| `UNRESOLVED` | 현재 지역이나 매핑에서 해석할 수 없음 |

분할 지역은 여러 행으로 확장됩니다.

```text
P001 | 28260 | 28275 | 인천광역시 서해구 | MAPPED
P001 | 28260 | 28290 | 인천광역시 검단구 | MAPPED
```

### 사용 원칙

- 사용자 지역 필터에는 `resolved_region_code`를 사용합니다.
- RAG 문맥에는 숫자 코드보다 `region_name`을 사용합니다.
- 정책별 지역명을 합칠 때는 1:N 매핑과 중복을 고려하여 `DISTINCT`와 안정적인 정렬을 사용합니다.
- 운영 점검에서는 `resolution_type = 'UNRESOLVED'`인 행을 확인합니다.

인천 코드가 정확히 알려진 9개 구성일 때 추가되는 영종구·서해구 행은 `resolution_type = 'INFERRED'`, `mapping_type = 'SCOPE_COMPLETION'`입니다. 원본 `policy_region`은 변경하지 않습니다.

---

## `v_policy_region_scope` View

**한 행의 의미:** 정책 하나의 전국 범위 판정.

| 컬럼 | 용도 |
| --- | --- |
| `policy_no` | 정책 번호 |
| `region_scope` | `NATIONWIDE`, `PARTIAL`, `UNKNOWN` |
| `source_region_count` | 원본 지역코드 수 |
| `resolved_active_region_count` | 해석된 고유 활성 지역 수 |
| `total_active_region_count` | `region`의 전체 활성 지역 수 |
| `unresolved_region_count` | 해석 불가 행 수 |
| `inactive_region_count` | 비활성 지역 행 수 |

원본 지역이 없거나 미해결·비활성 지역이 있으면 `UNKNOWN`, 모든 활성 지역을 포함하면 `NATIONWIDE`, 그 외는 `PARTIAL`입니다.

---

## `v_policy_sido_scope` View

**한 행의 의미:** 정책 하나가 포함한 시도 하나의 범위 판정.

| 컬럼 | 용도 |
| --- | --- |
| `policy_no` | 정책 번호 |
| `sido_code`, `sido_name` | 시도 식별·표시값 |
| `sido_scope` | 해당 시도 전체이면 `SIDO_WIDE`, 일부이면 `PARTIAL` |
| `resolved_active_region_count` | 정책이 포함한 해당 시도의 활성 지역 수 |
| `total_active_region_count` | 해당 시도의 전체 활성 지역 수 |

RAG의 `{시도명} 전체` 표시와 백엔드 필터가 같은 판정을 공유할 때 사용합니다.

---

## 프로젝트 외 테이블

로컬 DB에 존재할 수 있는 `vector_test`는 pgvector 설치 확인 과정에서 만들어진 테스트 테이블이며 YouthLink 정식 스키마에 포함되지 않습니다.

