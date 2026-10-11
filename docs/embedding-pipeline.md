# YouthLink 임베딩 및 pgvector 검색 파이프라인

작성 기준: 2026-10-11

## 1. 목적

`policy_chunk.content`를 OpenAI `text-embedding-3-small`의 1536차원 벡터로 변환해 같은 행의 `embedding`에 저장하고, 사용자 질문 벡터와 cosine similarity를 비교합니다.

공식 참고 문서:

- [OpenAI Vector embeddings 가이드](https://developers.openai.com/api/docs/guides/embeddings)
- [OpenAI Create embeddings API](https://developers.openai.com/api/reference/resources/embeddings/methods/create)

## 2. 환경변수

개인 `.env`에 다음 값을 설정합니다. API 키는 Git에 커밋하지 않습니다.

```dotenv
OPENAI_API_KEY=실제키
OPENAI_EMBEDDING_MODEL=text-embedding-3-small
```

DB의 `VECTOR(1536)` 및 HNSW 인덱스와 맞추기 위해 출력 차원은 코드에서 1536으로 검증합니다.

## 3. 비용 없는 실행 계획 확인

다음 명령은 OpenAI API를 호출하거나 DB를 수정하지 않습니다. 처리할 청크 수와 `cl100k_base` 기준 예상 토큰 수만 출력합니다.

```bash
python etl/embed_policy_chunks.py
```

최초 전체 임베딩 전 로컬 데이터 기준 결과는 다음과 같습니다. 정책이나 청크가 바뀌면 수치도 바뀝니다.

```text
chunks=11773
estimated_tokens=2716008
model=text-embedding-3-small
dimensions=1536
```

요금은 API 호출 시점의 공식 가격과 실제 응답의 `usage.prompt_tokens`를 기준으로 확인합니다.

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

기본 배치 크기는 100개입니다. 일시적인 연결 오류, 시간 초과, rate limit, 서버 오류는 최대 8회까지 재시도합니다. API가 응답한 재시도 대기 시간이 있으면 이를 우선하고, 없으면 지수 백오프를 적용합니다. API 잔액 부족처럼 대기해도 해결되지 않는 결제 오류는 재시도하지 않습니다. 최종 실패한 배치는 `embedding_error`에 오류를 기록하고 다음 배치로 진행합니다.

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
embedding       = 1536차원 벡터
embedding_model = text-embedding-3-small
token_count     = 청크별 입력 토큰 수
embedded_at     = 성공 시각
embedding_error = NULL
```

실패한 청크는 `embedding_error`에 오류가 남습니다. 기존 정상 벡터를 다른 모델로 갱신하다 실패한 경우에는 서비스 가능한 기존 벡터를 보존하고 오류만 기록합니다.

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

검색 SQL은 pgvector cosine distance 연산자 `<=>`를 사용하고 다음과 같이 유사도로 변환합니다.

```sql
1 - (embedding <=> query_embedding)
```

같은 정책에서 여러 청크가 검색될 수 있으므로 백엔드는 최종 결과를 `policy_no` 기준으로 묶어 사용합니다.

## 7. 상태 검증

```sql
SELECT
    COUNT(*) FILTER (WHERE embedding IS NOT NULL) AS embedded,
    COUNT(*) FILTER (WHERE embedding IS NULL AND embedding_error IS NULL) AS pending,
    COUNT(*) FILTER (WHERE embedding_error IS NOT NULL) AS failed
FROM policy_chunk;

SELECT embedding_model, vector_dims(embedding), COUNT(*)
FROM policy_chunk
WHERE embedding IS NOT NULL
GROUP BY embedding_model, vector_dims(embedding);
```

모든 청크가 성공했다면 `embedded`가 전체 청크 수와 같고 `pending`, `failed`는 0이어야 합니다.
