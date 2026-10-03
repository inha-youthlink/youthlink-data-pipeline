# YouthLink Database ERD

PostgreSQL + pgvector 기반 프로토타입 데이터베이스 구조입니다.

```mermaid
erDiagram

    POLICY {
        varchar policy_no PK
        varchar policy_name
        text description
        text support_content
        text_array keywords
        text_array large_categories
        text_array middle_categories

        varchar provider_group_code
        varchar provision_method_code
        varchar approval_status_code

        varchar supervising_org_code
        varchar supervising_org_name
        varchar supervising_manager_name

        varchar operating_org_code
        varchar operating_org_name
        varchar operating_manager_name

        boolean support_scale_limit
        integer support_scale_count
        boolean first_come_first_served

        integer min_age
        integer max_age
        boolean age_limit_yn

        varchar marriage_status_code
        varchar income_condition_code
        bigint min_income
        bigint max_income
        text income_etc

        varchar application_period_type_code
        date application_start_date
        date application_end_date
        text application_period_raw

        varchar business_period_type_code
        date business_start_date
        date business_end_date
        text business_period_etc

        text application_method
        text screening_method
        text submission_documents
        text additional_notes
        text additional_qualification
        text participation_exclusion

        text application_url
        text reference_url_1
        text reference_url_2

        varchar registrar_org_code
        varchar registrar_org_name

        timestamp source_created_at
        timestamp source_updated_at
        timestamp synced_at

        jsonb raw_data
    }

    POLICY_REGION {
        varchar policy_no PK,FK
        varchar region_code PK
    }

    REGION {
        varchar region_code PK
        varchar sido_code
        varchar sigungu_code
        varchar sido_name
        varchar sigungu_name
        varchar region_name
        boolean is_active
    }

    REGION_CODE_MAPPING {
        varchar source_code PK
        varchar canonical_code PK,FK
        varchar mapping_type
        date valid_from
        date valid_to
        text reason
    }

    POLICY_ELIGIBILITY_CODE {
        bigint id PK
        varchar policy_no FK
        varchar condition_type
        varchar condition_code
    }

    COMMON_CODE {
        varchar code PK
        varchar code_group
        varchar code_name
    }

    POLICY_CHUNK {
        uuid chunk_id PK
        varchar policy_no FK
        integer chunk_index
        varchar chunk_type
        text content
        vector embedding
        varchar embedding_model
        timestamp created_at
    }

    CHAT_ROOM {
        uuid room_id PK
        varchar title
        timestamp created_at
        timestamp updated_at
    }

    CHAT_PROFILE {
        uuid room_id PK,FK
        integer age
        varchar gender
        varchar region_code
        varchar marriage_status_code
        bigint annual_income
        varchar major_code
        varchar job_code
        varchar school_code
        varchar special_target_code
        jsonb extra_profile
        timestamp updated_at
    }

    CHAT_MESSAGE {
        bigint message_id PK
        uuid room_id FK
        varchar role
        text content
        timestamp created_at
    }

    MESSAGE_POLICY_REF {
        bigint message_id PK,FK
        varchar policy_no PK,FK
        float relevance_score
    }

    POLICY ||--o{ POLICY_REGION : "applies to"
    REGION ||--o{ REGION_CODE_MAPPING : "canonical region"
    POLICY ||--o{ POLICY_ELIGIBILITY_CODE : "has condition"
    POLICY ||--o{ POLICY_CHUNK : "chunked into"

    CHAT_ROOM ||--o| CHAT_PROFILE : "has"
    CHAT_ROOM ||--o{ CHAT_MESSAGE : "contains"

    CHAT_MESSAGE ||--o{ MESSAGE_POLICY_REF : "references"
    POLICY ||--o{ MESSAGE_POLICY_REF : "referenced by"
```

## Table groups

### Policy domain
- `policy`
- `policy_region`
- `region`
- `region_code_mapping`
- `policy_eligibility_code`
- `common_code`

### RAG domain
- `policy_chunk`

### Chat domain
- `chat_room`
- `chat_profile`
- `chat_message`
- `message_policy_ref`

## 지역 코드 관계

`policy_region.region_code`에는 온통청년 API의 원본 `zipCd`를 그대로 저장합니다. 원본에는 현재 VWorld 코드뿐 아니라 과거·폐지·상위 코드가 포함될 수 있으므로 `policy_region.region_code`와 `region.region_code` 사이에 외래키를 강제하지 않습니다.

`region`은 VWorld에서 동기화한 현재 시도·시군구 마스터입니다. `region_code_mapping`은 원본 코드인 `source_code`를 현재 코드인 `canonical_code`에 연결하며, `(source_code, canonical_code)` 복합 기본키로 지역 분할에 따른 1:N 매핑을 지원합니다. ERD의 `REGION` → `REGION_CODE_MAPPING` 관계는 `canonical_code`가 참조하는 현재 지역을 나타냅니다.

## `v_policy_region_resolved` View

`v_policy_region_resolved`는 RAG와 백엔드가 지역 코드의 변경 이력을 매번 직접 처리하지 않도록 다음 세 객체를 결합한 조회용 View입니다.

```mermaid
flowchart LR

    PR["policy_region<br/><br/>policy_no<br/>region_code"]

    MAP["region_code_mapping<br/><br/>source_code<br/>canonical_code<br/>mapping_type"]

    REGION["region<br/><br/>region_code<br/>sido_name<br/>sigungu_name<br/>region_name"]

    VIEW["v_policy_region_resolved<br/><br/>policy_no<br/>source_region_code<br/>resolved_region_code<br/>region_name<br/>resolution_type<br/>mapping_type"]

    PR -->|"현재 코드 직접 매칭"| REGION
    PR -->|"과거 / 상위 코드"| MAP
    MAP -->|"canonical_code"| REGION

    REGION --> VIEW
    PR --> VIEW
    MAP --> VIEW
```

View는 정책별 원본 코드와 해석된 현재 코드·지역명을 함께 제공하며, 해석 방식은 `resolution_type`으로 구분합니다.

| `resolution_type` | 의미 |
| --- | --- |
| `DIRECT` | `policy_region.region_code`가 현재 `region.region_code`와 직접 일치 |
| `MAPPED` | 원본 코드가 `region_code_mapping`을 거쳐 하나 이상의 현재 코드로 해석됨 |
| `UNRESOLVED` | 현재 지역과 매핑 어디에서도 원본 코드를 해석할 수 없음 |

주요 컬럼은 `policy_no`, `source_region_code`, `resolved_region_code`, `sido_code`, `sigungu_code`, `sido_name`, `sigungu_name`, `region_name`, `is_active`, `resolution_type`, `mapping_type`, `valid_from`, `valid_to`입니다. RAG·백엔드의 지역 필터는 원본 코드 대신 `resolved_region_code`를 사용하고, 운영 점검에서는 `resolution_type = 'UNRESOLVED'`인 행을 확인합니다.
