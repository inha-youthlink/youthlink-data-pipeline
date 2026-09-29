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
- `policy_eligibility_code`
- `common_code`

### RAG domain
- `policy_chunk`

### Chat domain
- `chat_room`
- `chat_profile`
- `chat_message`
- `message_policy_ref`