# YouthLink 임베딩 및 pgvector 검색 파이프라인

작성 기준: 2026-10-31

## 1. 목적

활성 정책의 API 기반 `policy_chunk.content`를 캡스톤 LiteLLM 게이트웨이의 `bedrock-titan-embed-v2` 1024차원 벡터로 변환해 같은 행의 `embedding`에 저장하고, 사용자 질문 벡터와 cosine similarity를 비교합니다. 이 문서의 기본 파이프라인은 온통청년 API 원천 전용입니다.

크롤링 원문은 `policy_chunk`에 합치지 않습니다. 검증된 원문은 별도 `policy_web_chunk`에 생성하고 `etl/embed_policy_web_chunks.py`로 별도 임베딩합니다. RAG는 API 검색을 먼저 수행하고 부족할 때만 같은 `policy_no` 범위의 `v_verified_policy_web_chunk`를 fallback으로 조회합니다.

`policy.is_active = false`인 정책은 새 임베딩 후보와 검색 결과에서 제외합니다. 이력과 재활성화를 위해 기존 청크·벡터는 삭제하지 않으며, 활성 여부만 바뀐 경우 전체 임베딩을 다시 생성할 필요가 없습니다.

게이트웨이는 OpenAI 호환 `/v1/embeddings` API를 제공하므로 Python `openai` SDK를 사용합니다. AWS 자격증명이나 `boto3`로 Bedrock을 직접 호출하지 않습니다.

## 2. 환경변수

개인 `.env`에 다음 값을 설정합니다. API 키는 Git에 커밋하지 않습니다.

```dotenv
LITELLM_API_KEY=
LITELLM_BASE_URL=https://3.226.2.148/v1
LITELLM_EMBEDDING_MODEL=bedrock-titan-embed-v2
```

API 키는 Git에 커밋하거나 화면·로그에 출력하지 않습니다. 기존 `OPENAI_API_KEY`를 게이트웨이 키로 자동 사용하지 않으므로, 반드시 별도의 `LITELLM_API_KEY`를 설정해야 합니다.

DB의 `VECTOR(1024)` 및 HNSW 인덱스와 맞추기 위해 출력 차원은 코드에서 1024로 검증합니다. 모델과 질문 임베딩도 반드시 같은 별칭을 사용해야 합니다.

임베딩 요청에는 `encoding_format="float"`를 반드시 넣습니다. OpenAI SDK는 이 값을 생략하면 `"base64"`를 보내고, 게이트웨이는 이를 Titan의 이진 출력으로 바꿔 모든 칸이 0 또는 1인 벡터를 돌려줍니다. 이진 벡터는 칸마다 크기 정보가 사라져 검색 점수가 좁은 범위에 몰리므로 검색 순위가 망가집니다. 코드는 0/1로만 된 벡터를 받으면 저장 전에 오류로 멈춥니다. 질문 임베딩(백엔드)도 같은 옵션을 써야 합니다.

2026-11-01 이전에 이 옵션 없이 임베딩한 DB는 벡터가 이진입니다. 모델 이름이 같아 기본 실행은 건너뛰므로 `--force`로 전체를 다시 임베딩합니다. 저장된 벡터가 이진인지는 `SELECT embedding::text FROM policy_chunk WHERE embedding IS NOT NULL LIMIT 1`로 값이 0과 1뿐인지 보면 알 수 있습니다.

DB 마이그레이션 전에 인증과 모델 권한을 한 문장으로 확인합니다. 이 명령은 DB를 읽거나 수정하지 않고 키와 벡터 원문도 출력하지 않습니다.

```bash
python etl/test_embedding_gateway.py
```

출력에 `model=bedrock-titan-embed-v2, dimensions=1024`가 보여야 다음 단계로 진행합니다. 게이트웨이가 이진(0/1) 벡터를 주면 이 점검에서 오류로 멈춥니다.

## 3. 비용 없는 실행 계획 확인

다음 명령은 게이트웨이를 호출하거나 DB를 수정하지 않습니다. 처리할 청크 수와 문자 기반 참고용 예상 토큰 수만 출력합니다. 이 값은 외부 토크나이저 다운로드 없이 계산하며 Titan 과금 토큰과 정확히 일치하지 않습니다. 실제 배치 사용량은 게이트웨이 응답의 `usage`를 사용합니다.

```bash
python etl/embed_policy_chunks.py
```

최초 전체 임베딩 전 로컬 데이터 기준 결과는 다음과 같습니다. 정책이나 청크가 바뀌면 수치도 바뀝니다.

```text
chunks=14471
estimated_tokens=...
model=bedrock-titan-embed-v2
dimensions=1024
```

요금과 잔여 예산은 캡스톤 게이트웨이 운영자에게 확인합니다. 임베딩 호출은 발급받은 키의 공용 예산에 포함됩니다.

전체 임베딩이 완료된 상태에서 같은 계획 명령을 다시 실행하면 다음처럼 대상이 0개로 표시됩니다.

```text
chunks=0
estimated_tokens=0
```

## 4. 임베딩 실행

처음에는 소량으로 실제 API·DB 연결을 검증합니다.

```bash
python etl/embed_policy_chunks.py --write --limit 10
```

정상 적재를 확인한 뒤 나머지를 실행합니다.

```bash
python etl/embed_policy_chunks.py --write
```

기본 배치 크기는 100개입니다. 게이트웨이는 입력 배열을 지원합니다. 일시적인 연결 오류, 시간 초과, rate limit, 서버 오류는 최대 8회까지 재시도합니다. API가 응답한 재시도 대기 시간이 있으면 이를 우선하고, 없으면 지수 백오프를 적용합니다. 예산 초과처럼 대기해도 해결되지 않는 오류는 재시도하지 않습니다. 최종 실패한 배치는 `embedding_error`에 오류를 기록하고 다음 배치로 진행합니다.

```bash
# 이전 실패 청크 재시도
python etl/embed_policy_chunks.py --write --retry-errors

# 배치 크기 조정
python etl/embed_policy_chunks.py --write --batch-size 50
```

이미 같은 모델로 임베딩된 청크는 건너뜁니다. `--force`는 정상 벡터까지 다시 생성하여 API 비용이 다시 발생하므로 모델 변경이나 명시적인 전체 재생성에만 사용합니다.

## 5. 저장 상태

성공한 청크:

```text
embedding       = 1024차원 벡터
embedding_model = bedrock-titan-embed-v2
token_count     = 문자 기반 참고용 청크 토큰 추정치
embedded_at     = 성공 시각
embedding_error = NULL
```

실패한 청크는 `embedding_error`에 오류가 남습니다. 기존 정상 벡터를 다른 모델로 갱신하다 실패한 경우에는 서비스 가능한 기존 벡터를 보존하고 오류만 기록합니다.

이전 파이프라인에서 만든 `token_count`를 현재 문자 기반 추정 방식으로 맞출 때는 외부 API를 호출하지 않는 다음 명령을 사용합니다.

```bash
python etl/backfill_embedding_token_estimates.py
python etl/backfill_embedding_token_estimates.py --write
```

## 6. 벡터 검색

질문도 정책 청크와 같은 모델·차원으로 임베딩한 뒤 검색합니다.

```bash
python etl/search_policy_chunks.py "인천에서 받을 수 있는 청년 월세 지원"
```

RDB 조건으로 먼저 선정한 정책 번호가 있다면 반복 가능한 `--policy-no`로 후보 범위를 제한합니다.

```bash
python etl/search_policy_chunks.py \
  "취업 준비 비용 지원" \
  --policy-no 정책번호1 \
  --policy-no 정책번호2 \
  --limit 5
```

특정 의미 청크만 검색할 수도 있습니다.

```bash
python etl/search_policy_chunks.py \
  "제출해야 하는 서류" \
  --chunk-type APPLICATION
```

검색 SQL은 활성 정책만 대상으로 pgvector cosine distance 연산자 `<=>`를 사용하고 다음과 같이 유사도로 변환합니다.

```sql
1 - (embedding <=> query_embedding)
```

같은 정책에서 여러 청크가 검색될 수 있으므로 백엔드는 최종 결과를 `policy_no` 기준으로 묶어 사용합니다.

## 7. 상태 검증

```sql
SELECT
    COUNT(*) FILTER (WHERE p.is_active AND pc.embedding IS NOT NULL) AS active_embedded,
    COUNT(*) FILTER (
        WHERE p.is_active
          AND pc.embedding IS NULL
          AND pc.embedding_error IS NULL
    ) AS active_pending,
    COUNT(*) FILTER (WHERE pc.embedding_error IS NOT NULL) AS failed,
    COUNT(*) FILTER (
        WHERE NOT p.is_active AND pc.embedding IS NULL
    ) AS inactive_preserved_without_embedding
FROM policy_chunk pc
JOIN policy p ON p.policy_no = pc.policy_no;

SELECT embedding_model, vector_dims(embedding), COUNT(*)
FROM policy_chunk
WHERE embedding IS NOT NULL
GROUP BY embedding_model, vector_dims(embedding);

SELECT
    COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS web_embedded,
    COUNT(*) FILTER (WHERE embedding IS NULL AND embedding_error IS NULL) AS web_pending,
    COUNT(*) FILTER (WHERE embedding_error IS NOT NULL) AS web_failed
FROM policy_web_chunk;
```

모든 대상이 성공했다면 `active_pending`, `failed`, `web_pending`, `web_failed`가 0이어야 합니다. 비활성 정책 청크는 감사·재활성화를 위해 본문을 보존하되 비용 절감을 위해 벡터가 없어도 정상입니다.

## 8. 기존 1536차원 DB 전환

기존 OpenAI 벡터는 Titan 벡터와 차원과 의미 공간이 모두 달라 재사용할 수 없습니다. 정책·청크 본문은 유지하고 임베딩 파생 값만 초기화합니다. DB 백업과 게이트웨이 소량 호출 성공을 확인한 다음 적용합니다.

```bash
psql -h localhost -p 5432 -U postgres -W -d youthlink \
  -v ON_ERROR_STOP=1 \
  -f db/migrations/013_change_embedding_dimension_to_1024.sql
```

마이그레이션은 두 청크 테이블의 임베딩 상태와 HNSW 인덱스를 1024차원으로 전환합니다. 이후 정책 청크와 검증 웹 청크를 다시 임베딩합니다.

```bash
python etl/embed_policy_chunks.py --write --limit 10
python etl/embed_policy_chunks.py --write
python etl/embed_policy_web_chunks.py --write
```

2026-10-31 로컬 검증에서는 활성 정책 청크 14,471건과 검증 웹 청크 6건이 모두 `bedrock-titan-embed-v2` 1024차원으로 저장됐고, 실패·처리 대기 대상은 0건이었습니다.
