# 검증형 정책 원문 수집 파이프라인

작성 기준: 2026-11-06

온통청년 API의 신청·참고 URL에서 부족한 정보를 보강하되, 관련 없는 페이지의 내용이 정책 데이터나 RAG에 들어가지 않도록 수집과 사용을 분리합니다. 이 파이프라인의 최우선 원칙은 **수집 성공이 곧 데이터 승인이라는 뜻이 아니며, `VERIFIED`를 통과한 데이터만 소비하는 것**입니다.

## 현재 구현 범위

```text
활성 policy의 URL
  → URL 정규화·위험 URL 차단
  → web_source / policy_web_source(PENDING)
  → robots.txt·DNS·응답 제한을 적용한 HTML 수집
  → web_document(staging)
  → 정책-페이지 관련성 판정
  → REVIEW_REQUIRED는 선택적으로 AI 근거 추출·코드 재검증
  → VERIFIED만 검증 전용 View에 노출
```

현재 구현은 URL 발견·동기화, 소량 HTML 수집, 정책-페이지 관련성 판정, `REVIEW_REQUIRED`의 AI 보조 근거 검증, 검증 원문의 별도 웹 청크·임베딩 파이프라인까지입니다. `policy_enrichment_fact`는 선택적인 필드 추출·검수용 스키마이며, 현재 크롤러가 자동으로 사실을 생성하지는 않습니다. 크롤링 결과는 기존 `policy`와 `policy_chunk`에 합치지 않습니다.

## 잘못된 원문 유입을 막는 경계

1. `sync_policy_sources.py`는 활성 정책의 URL만 읽고 HTTP(S) URL을 정규화합니다. localhost, 비공개 IP 리터럴, 자격증명이 포함된 URL, 비공개 단일 호스트명은 저장하지 않습니다.
2. 새 정책-URL 관계는 항상 `PENDING`으로 시작합니다. URL이 존재한다는 사실만으로 관련성이 승인되지 않습니다.
3. 실제 요청 직전에 DNS를 다시 확인하고, 응답 주소 중 하나라도 공인 IP가 아니면 차단합니다. 리다이렉트된 URL도 매번 같은 검사를 받습니다.
4. `robots.txt`를 확인할 수 없거나 수집이 허용되지 않으면 보수적으로 차단합니다. 응답 크기·타임아웃·리다이렉트 수를 제한하고 HTML 문서만 받습니다. 제한형 iframe에서는 바깥 shell과 내부 본문 응답 크기를 합산합니다.
5. HTML은 `main`, `article`, `role=main`, 내용 컨테이너를 우선 추출하고 메뉴·스크립트·폼·footer 등을 제거합니다.
6. 정책 번호·정확한 정책명·기관·지역·신청기간 같은 결정적 신호로 정책과 페이지의 관계를 판정합니다. URL의 정책 번호 하나만으로 승인하지 않으며, 임베딩이나 LLM 점수만으로도 자동 승인하지 않습니다.
7. 판정은 `validated_document_id`로 검사한 문서 버전에 고정합니다. 같은 URL의 본문이 바뀌면 과거 승인이 새 문서로 자동 승계되지 않습니다.
8. 정확한 Naver 게시물 URL의 제한형 iframe 수집도 자동 승인하지 않고 항상 `REVIEW_REQUIRED`로 보냅니다.
9. AI는 승인 상태를 직접 결정하지 않고 구조화된 직접 인용만 추출합니다. 코드는 인용이 저장 문서에 실제로 존재하는지와 API 소유 정책값이 일치하는지 다시 검사하며, shadow 실행만으로는 관계 상태가 바뀌지 않습니다.
10. RAG와 백엔드는 staging 테이블을 직접 사용하지 않고 `v_verified_policy_web_document` 또는 `v_verified_policy_enrichment`만 사용합니다.

DNS 검사는 요청 직전의 목적지 검증이지만, 향후 운영 환경에서는 네트워크 egress 제한과 허용 도메인 정책도 함께 적용하는 것이 좋습니다.

## 상태값

### URL 수집 상태 (`web_source.fetch_status`)

| 상태 | 의미 |
| --- | --- |
| `DISCOVERED` | URL만 발견했고 아직 요청하지 않음 |
| `FETCHED` | HTML을 정상 수집·정제함 |
| `NOT_MODIFIED` | 조건부 요청 결과가 304임 |
| `BLOCKED` | 주소·robots.txt·접근 제한 등으로 자동 수집하지 않음 |
| `UNSUPPORTED` | HTML이 아니거나 너무 크거나 유효 본문이 부족함 |
| `FAILED` | 네트워크·HTTP 등 수집 실패 |

### 정책-URL 관련성 (`policy_web_source.validation_status`)

| 상태 | 사용 여부 | 의미 |
| --- | --- | --- |
| `PENDING` | 사용 금지 | 아직 검증하지 않음 |
| `VERIFIED` | 검증 View에 노출 | 정책 번호와 페이지 내부 정책 식별 근거, 또는 충분한 독립 근거로 관련성을 확인함 |
| `REVIEW_REQUIRED` | 사용 금지 | 일부 신호는 있으나 자동 승인 근거가 부족함 |
| `REJECTED` | 사용 금지 | 다른 정책·기관·지역 등 강한 충돌 또는 오류 페이지 |
| `LINK_ONLY` | 사실 추출 금지 | 로그인·검색·공통 신청 포털처럼 링크로만 유용함 |

URL 또는 메타데이터의 정책 번호가 정확히 일치하더라도 페이지 내부의 정확한 정책명 또는 별도의 정책 근거가 추가로 필요합니다. 정책명이 제목·H1·heading과 정확히 같을 때에는 기관·지역·신청기간·정책 고유 정보 중 독립 근거가 둘 이상 일치해야 자동 승인합니다. 본문에 정책 번호나 이름이 한 번 등장하는 것만으로는 승인하지 않습니다.

## 테이블과 View

| 객체 | 한 행의 의미 |
| --- | --- |
| `web_source` | 정규화한 고유 URL 하나와 마지막 HTTP 수집 상태 |
| `policy_web_source` | 정책 하나와 URL 하나의 관계 유형·활성 여부·검증 결과 및 검증한 문서 버전 |
| `web_document` | URL에서 특정 시점에 추출한 본문 버전 하나 |
| `policy_web_ai_review` | 정확한 정책·문서 버전에 고정된 AI 근거 추출 및 코드 재검증 관찰 하나 |
| `policy_enrichment_fact` | 원문에서 추출한 정책 필드값 하나와 직접 근거·검증 결과 |
| `policy_web_chunk` | 검증된 원문만 나눈 별도 RAG fallback 청크·임베딩 |
| `rag_retrieval_chunk` | 활성 API 청크와 현재 검증 웹 청크를 발행한 단일 검색 projection |
| `v_verified_policy_web_document` | 활성 정책과 `VERIFIED` 관계로 연결된 최신 원문 |
| `v_verified_policy_enrichment` | 검증된 관계의 최신 문서에서 나온 `VERIFIED` 사실 |
| `v_verified_policy_web_chunk` | 현재 검증 문서에 연결된 별도 웹 청크와 출처 |

`web_document`는 `content_hash`로 같은 내용을 중복 저장하지 않고 `is_current`로 최신 버전을 표시합니다. 원문 URL이 API에서 사라지거나 정책이 비활성화되면 연결을 소프트 비활성화하며 과거 문서·검증 기록은 보존합니다.

## 실행 순서

먼저 웹 원문 009~012를 정확한 파일명 순서로 적용하고, Titan 1024차원 전환 013, 통합 검색 016, AI 검토 관찰 017 순서로 적용합니다. 저장소에는 과거 chat migration과 번호가 중복된 009 파일이 있으므로 번호만 보고 선택하지 않습니다.

```bash
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/009_add_policy_web_sources.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/010_bind_web_validation_to_document.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/011_add_separate_policy_web_chunks.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/012_enforce_validated_document_source.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/013_change_embedding_dimension_to_1024.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/016_add_rag_retrieval_chunks.sql
psql -h localhost -p 5432 -U postgres -W -d youthlink -v ON_ERROR_STOP=1 -f db/migrations/017_add_policy_web_ai_review.sql
```

012는 `validated_document_id`가 존재하는지만 보지 않고 그 문서가 해당 `source_id` 소속인지까지 복합 FK로 강제합니다. 잘못된 출처의 문서 버전을 판정에 연결하려 하면 DB가 저장을 거부합니다. 017은 AI 관찰을 동일 출처의 정확한 문서에 복합 FK로 고정하고, 성공한 동일 입력만 캐시합니다.

### 1. URL 목록 확인

기본 실행은 네트워크 요청도 DB 변경도 하지 않습니다.

```bash
python etl/sync_policy_sources.py
```

유효 링크 수, 고유 URL 수, 상위 도메인, 차단된 URL 사유를 확인한 뒤 저장합니다.

```bash
python etl/sync_policy_sources.py --write
```

새 링크는 모두 `PENDING`입니다. 반복 실행하면 현재 활성 정책의 URL을 UPSERT하고 사라진 연결은 삭제하지 않고 `is_active = false`로 전환합니다.

### 2. 소량 수집 계획 확인

기본 실행은 DB에서 후보만 읽으며 네트워크 요청을 하지 않습니다. 신청방법·심사방법·제출서류·참여 제외·추가 자격 필드가 많이 비어 있는 정책의 URL을 우선합니다.

```bash
python etl/crawl_policy_sources.py --limit 10
```

처음에는 특정 도메인과 매우 작은 건수로 파일럿 실행합니다.

```bash
python etl/crawl_policy_sources.py --host example.go.kr --limit 3 --write
```

실패한 소스를 원인 확인 후 다시 시도할 때만 `--retry-failed`를 사용합니다. `BLOCKED`를 반복 요청하는 용도로 사용하지 않습니다.

```bash
python etl/crawl_policy_sources.py --host example.go.kr --limit 3 --retry-failed --write
```

검토한 소스 UUID만 정확히 선택하려면 `--source-id`를 반복합니다. 이미 `FETCHED`인 문서를 새 추출 규칙으로 다시 가져올 때에만 `--refetch`를 사용합니다. 두 경우 모두 먼저 `--write` 없는 계획을 확인합니다.

```bash
python etl/crawl_policy_sources.py \
  --source-id <SOURCE_UUID_1> \
  --source-id <SOURCE_UUID_2> \
  --retry-failed \
  --limit 2

python etl/crawl_policy_sources.py \
  --source-id <SOURCE_UUID> \
  --refetch \
  --limit 1 \
  --write
```

실패 응답의 HTML·본문은 `web_document`에 저장하지 않습니다. 크롤러는 운영자가 원인을 구분할 수 있도록 다음과 같은 비민감 진단만 `web_source.metadata.last_failure_diagnostics`에 남깁니다.

- HTTP 상태, Content-Type, 응답 바이트 수
- 리다이렉트 후 최종 host와 path(query 제외)
- title·후보 컨테이너·body·최종 선택 텍스트의 문자 수
- semantic candidate, script, iframe 개수

최종 URL의 query, 응답 원문, 추출 실패 본문은 진단에 넣지 않습니다. 성공 또는 `304 Not Modified`가 확인되면 오래된 실패 진단은 제거합니다. 이 정보는 `INSUFFICIENT_TEXT`가 짧은 안내 페이지인지, JavaScript shell/iframe 가능성이 있는지 판단하기 위한 관측 자료일 뿐 관련성 증거가 아닙니다.

```sql
SELECT host, fetch_status, error_type, http_status,
       metadata -> 'last_failure_diagnostics' AS failure_diagnostics
FROM web_source
WHERE fetch_status IN ('FAILED', 'UNSUPPORTED', 'BLOCKED')
ORDER BY last_attempt_at DESC;
```

`UNSUPPORTED`와 `BLOCKED`는 기본 후보에서 제외되므로 자동 재시도·자동 검증되지 않습니다. `--retry-failed`는 진단을 검토하고 정적 요청으로 회복 가능하다고 판단한 소스에만 제한적으로 사용합니다. 별도 JavaScript 렌더링 수집기는 구현하지 않았으며, 정확한 문서 버전을 확보하지 못한 JS shell을 수동으로 `VERIFIED` 처리하면 안 됩니다.

### Naver 게시물 iframe 제한 지원

Naver 전체 iframe을 일반적으로 허용하지 않습니다. 다음 조건을 모두 만족하는 정확한 게시물만 깊이 1로 수집합니다.

- 바깥 URL이 `https://blog.naver.com/{blogId}/{숫자 logNo}`이며 기본 포트를 사용함
- 블로그 루트나 임의 목록 URL이 아님
- `id` 또는 `name`이 `mainFrame`인 iframe이 정확히 하나임
- 내부 URL이 HTTPS 기본 포트의 정확한 `blog.naver.com/PostView.naver`임
- 내부 query의 `blogId`, `logNo`가 바깥 게시물과 일치함
- iframe 최초 요청과 각 리다이렉트 hop이 같은 대상 규칙을 통과하고 robots·DNS 검사를 다시 받음
- 바깥 shell과 iframe 응답의 합산 크기가 설정 한도 이내임

내부 HTML에서는 SmartEditor의 `se-main-container` 하나만 본문으로 사용합니다. 블로그 메뉴·공지·이웃 글·댓글 UI는 정책 근거에서 제외합니다. URL query나 응답 본문은 provenance에 저장하지 않으며, `web_document.metadata.retrieval`에는 수집 모드·깊이, container/content host와 path, `blog_id`, `log_no`, 내부 HTTP 상태·Content-Type·응답 바이트 수만 남깁니다. 바깥 shell의 ETag나 Last-Modified는 iframe 본문 변경을 보장하지 않으므로 재사용하지 않습니다.

이 어댑터의 결정적 판정이 `VERIFIED`여도 자동으로 `REVIEW_REQUIRED`로 낮춥니다. 본문 hash가 바뀌어 새 현재 문서가 만들어지면 이전 수동 판정도 초기화하고 새 문서 버전을 다시 검토합니다.

재수집이 `BLOCKED`·`UNSUPPORTED` 또는 HTTP 404/410으로 끝나면 이전 문서의 `is_current`를 해제하고 관계 판정을 `PENDING`으로 초기화합니다. 삭제·변조·수집 금지된 원문의 과거 웹 청크는 검증 View에서 즉시 제외됩니다. 일시적인 네트워크·서버 실패(`FAILED`)는 마지막 검증 문서를 유지하고 재시도합니다.

2026-10-30 Naver 6건 파일럿 결과는 다음과 같습니다.

- 숫자 게시물 URL 4건: `FETCHED`
- 게시물 ID가 없는 블로그 루트 2건: `UNSUPPORTED`
- 수동 판정: 광양 드림카페 `VERIFIED` 1건, 완주 관광성 청년몰 글 `LINK_ONLY` 1건, 2024년 익산 모집 글 `LINK_ONLY` 1건, 대학리그와 다른 부산 이스포츠 글 `REJECTED` 1건
- 승인된 광양 문서: 별도 웹 청크 2개 생성. 2026-10-31 전체 검증 웹 청크 6건과 함께 1024차원 `bedrock-titan-embed-v2`로 재임베딩 완료

### 3. 저장된 문서 재검증

검증 규칙 버전이 바뀌거나 이미 수집한 URL에 새 정책 연결이 추가되면 네트워크 요청 없이 현재 문서를 다시 판정합니다. 기본 실행은 계획만 표시합니다.

```bash
python etl/revalidate_policy_sources.py --limit 100
python etl/revalidate_policy_sources.py --limit 100 --write
```

이 명령은 적용된 AI 관찰의 정책 근거 hash도 현재 정책명·기관·지역·신청기간·고유어로 다시 계산합니다. 정책 API나 지역 해석 변경으로 hash가 달라진 판정은 `REVIEW_REQUIRED`로 안전하게 되돌립니다. 현재 결정적 검증기와 AI 코드 검증기 버전이 모두 같고 근거도 변경되지 않은 판정만 보존하며, 이전 AI 코드 버전은 재검증 대상으로 돌립니다.

같은 문서 버전에 끝난 수동 검토(`reviewed_at` 존재)는 자동 재검증으로 덮어쓰지 않습니다. 그러나 수집 본문의 hash가 바뀌어 새 `web_document`가 현재 버전이 되면 크롤러가 이전 판정·검수 메타데이터를 초기화합니다. 새 버전은 자동 판정과 필요한 수동 검토를 다시 통과해야 합니다.

### 4. `REVIEW_REQUIRED` AI 보조 검증

AI 보조 검증은 결정적 검증기가 `REVIEW_REQUIRED`로 남긴 현재 문서만 대상으로 합니다. 기본 실행은 후보 계획만 출력하며 게이트웨이를 호출하거나 DB를 변경하지 않습니다.

```bash
python etl/ai_review_policy_sources.py --limit 10 --show-url
```

개인 `.env`에 `LITELLM_API_KEY`, `LITELLM_BASE_URL`, 팀에서 허용한 `LITELLM_CHAT_MODEL` 별칭을 설정한 뒤 한 건부터 shadow 실행합니다. 모델 별칭은 임베딩 별칭과 별개이며 코드가 임의로 추측하지 않습니다.

```bash
python etl/ai_review_policy_sources.py --shadow --limit 1 --write
python etl/ai_review_policy_sources.py --show-observations --limit 20
```

`--shadow --write`의 쓰기는 `policy_web_ai_review` 관찰 저장만 뜻합니다. AI는 폐쇄형 JSON으로 페이지 유형·정책 정체성·기관·지역·신청기간·고유어의 직접 인용과 충돌·종료 표시를 추출할 뿐 최종 상태를 쓰지 않습니다. 코드는 모든 인용을 저장된 제목·H1·heading·본문에서 다시 찾고 API 정책값과 비교합니다. 정확한 단일 정책 정체성과 서로 독립적인 근거 2종 이상이 있고, 충돌·다중 정책·종료 표시·검증 실패 근거가 하나도 없어야 `VERIFIED`를 제안합니다. 입력 한도를 넘는 본문은 잘라 판단하지 않고 실패 관찰로 남깁니다.

관찰 적용은 UUID 하나를 명시해야 합니다. 먼저 dry-run으로 저장 제안을 확인한 뒤 `--write`를 붙입니다.

```bash
python etl/ai_review_policy_sources.py --apply-review-id <AI_REVIEW_UUID>
python etl/ai_review_policy_sources.py --apply-review-id <AI_REVIEW_UUID> --write
```

적용 트랜잭션은 활성 정책, 현재 문서 ID와 `content_hash`, 정책 근거 hash, 미검토 `REVIEW_REQUIRED` 관계를 잠그고 저장된 JSON을 현재 코드로 다시 검증합니다. stale 관찰, 이미 적용한 관찰, 근거가 부족한 제안, 사람 검토가 끝난 관계는 거부합니다. `REJECTED` 제안은 대상 정책번호가 없고 다른 정책명이 제목·H1·개별 heading에서 직접 확인된 경우로 제한합니다. AI 적용은 사람 검수 필드인 `reviewed_by`, `reviewed_at`을 채우지 않습니다.

### 5. `REVIEW_REQUIRED` 수동 검토

AI 보조 검증 뒤에도 근거가 부족하거나 판정이 의심스러운 원문은 사람이 검토합니다. 기본 실행은 후보를 읽기만 하며 DB를 바꾸지 않습니다.

```bash
python etl/review_policy_sources.py --limit 20 --show-url
```

후보의 정책명·문서 제목·본문·URL을 확인한 뒤 다음 세 상태 중 하나를 선택합니다.

- `VERIFIED`: 해당 정책의 사실 근거로 쓸 수 있음
- `REJECTED`: 다른 정책·기관·지역의 페이지이거나 오류 페이지임
- `LINK_ONLY`: 공통 포털·목록·검색·로그인 페이지처럼 이동 경로로만 유용함

정확한 문서 버전에 판정을 묶기 위해 후보 출력의 `document_id`를 `--expected-document-id`로 다시 제출합니다. 먼저 dry-run으로 대상을 확인합니다.

```bash
python etl/review_policy_sources.py \
  --policy-no <POLICY_NO> \
  --source-id <SOURCE_UUID> \
  --expected-document-id <DOCUMENT_UUID> \
  --decision <VERIFIED|REJECTED|LINK_ONLY> \
  --reviewed-by <REVIEWER> \
  --reason "구체적인 판정 근거"
```

출력된 정책·원문·문서 버전·판정이 정확할 때만 같은 명령에 `--write`를 붙입니다. 해당 문서가 아직 최신이고, 활성 정책의 활성·미검토 `REVIEW_REQUIRED` 관계일 때만 판정이 저장됩니다. 동일 정책-URL이 복수 관계 유형으로 연결돼 있으면 모두 한 트랜잭션으로 적용되며 일부만 바뀌지 않습니다.

수동 판정은 `policy_web_source`의 검수 메타데이터만 바꾸며 `policy`, `policy_chunk`, `web_document` 본문을 수정하지 않습니다. `VERIFIED`만 검증 View와 이후 웹 청크 입력에 노출됩니다. `REJECTED`와 `LINK_ONLY`는 기록은 보존하지만 웹 청크를 생성하지 않습니다.

AI 적용과 수동 판정은 모두 관계 상태까지만 바꿉니다. `VERIFIED`가 새로 생긴 뒤에는 `generate_policy_web_chunks.py`, `embed_policy_web_chunks.py`, `sync_rag_retrieval_chunks.py`를 별도로 실행해야 검색 projection에 반영됩니다.

## 누락 필드 우선순위

현재 후보 점수는 활성 정책에서 다음 값이 `NULL`인 개수입니다.

- `application_method`
- `screening_method`
- `submission_documents`
- `participation_exclusion`
- `additional_qualification`

점수는 수집 순서를 정할 뿐 관련성 승인 점수가 아닙니다. 원문에서 구조화 사실을 추출하더라도 기존 API 값을 절대 덮어쓰지 않습니다. 추출 결과는 `policy_enrichment_fact`에 값·직접 인용 근거·문서 ID를 별도로 저장하고 독립적으로 검증합니다.

## 검증 쿼리

```sql
SELECT fetch_status, COUNT(*)
FROM web_source
GROUP BY fetch_status
ORDER BY fetch_status;

SELECT validation_status, COUNT(*)
FROM policy_web_source
WHERE is_active = TRUE
GROUP BY validation_status
ORDER BY validation_status;

SELECT COUNT(*) AS verified_current_documents
FROM v_verified_policy_web_document;

SELECT field_name, COUNT(*) AS verified_facts
FROM v_verified_policy_enrichment
GROUP BY field_name
ORDER BY field_name;
```

`web_document`의 행 수와 검증 View의 행 수는 같을 필요가 없습니다. 수집은 성공했지만 정책과의 관련성이 부족한 원문은 staging에만 남는 것이 정상입니다.

## 별도 원천 청크와 통합 RAG 검색 projection

API 원천과 크롤링 원천은 끝까지 분리하고 검색용 파생 계층에서만 합칩니다.

```text
API:      policy → policy_chunk ──────────────┐
                                               ├→ rag_retrieval_chunk → 단일 검색
크롤링:   web_document → policy_web_chunk ────┘
```

`generate_policy_web_chunks.py`는 `v_verified_policy_web_document`만 읽고 `policy_web_chunk`만 갱신합니다. `embed_policy_web_chunks.py`도 검증 View에 남아 있는 웹 청크만 임베딩하며 `policy`나 `policy_chunk`를 수정하지 않습니다.

```bash
python etl/generate_policy_web_chunks.py
python etl/generate_policy_web_chunks.py --write

python etl/embed_policy_web_chunks.py
python etl/embed_policy_web_chunks.py --write

python etl/sync_rag_retrieval_chunks.py
python etl/sync_rag_retrieval_chunks.py --write
```

publish ETL은 활성 정책의 정상 API 임베딩과 `v_verified_policy_web_chunk`에 노출되는 정상 웹 임베딩만 `rag_retrieval_chunk`에 복사합니다. 질문 임베딩과 동일한 모델을 대상으로 HNSW 검색을 한 번 수행하며, RDB 조건으로 얻은 후보 `policy_no`를 반드시 전달합니다. 결과에는 `source_type`, 원문 URL, `document_id`가 포함되므로 API와 웹의 권위·출처를 후단에서 구분할 수 있습니다. `source_priority`는 정책별 그룹화와 context 구성에 사용하고 벡터 거리식 자체를 바꾸지는 않습니다.

```sql
-- :candidate_policy_nos는 RDB 단계에서 이미 선택된 정책 번호 배열
SELECT
    policy_no,
    document_id,
    source_type,
    COALESCE(final_url, canonical_url) AS source_url,
    content,
    1 - (embedding <=> :query_embedding::vector) AS similarity
FROM rag_retrieval_chunk
WHERE policy_no = ANY(:candidate_policy_nos)
  AND embedding_model = :embedding_model
ORDER BY embedding <=> :query_embedding::vector
LIMIT :limit;
```

검증되지 않은 `web_document`, `PENDING`, `REVIEW_REQUIRED`, `REJECTED`, `LINK_ONLY` 관계를 웹 청크 생성이나 검색 projection 입력으로 사용하면 안 됩니다. 웹 청크가 변경되어도 API 기반 `policy_chunk` 임베딩은 그대로 유지됩니다. 승인 취소·문서 교체 뒤에는 publish ETL을 같은 운영 작업 안에서 실행하여 과거 projection 행을 제거해야 합니다.

## 2026-11-07 로컬 검증 스냅샷

- 정책 3,708건, 활성 3,664건, API `policy_chunk` 14,647건
- `web_document` 전체 30건, 현재 문서 26건, 검증된 현재 원문 9건
- `policy_web_chunk` 6건, 기존 binary Titan V2 1024차원 임베딩 6건
- AI 관찰 18건: `FAILED` 1건, `REVIEW_REQUIRED` 제안 12건, `VERIFIED` 제안 5건이며 그중 4건을 파일럿 적용
- 소스 상태: `DISCOVERED` 2,933 / `FETCHED` 26 / `UNSUPPORTED` 6 / `BLOCKED` 5
- 활성 관계 상태: `PENDING` 4,463 / `REVIEW_REQUIRED` 10 / `VERIFIED` 9 / `LINK_ONLY` 14 / `REJECTED` 1

이 스냅샷의 크롤링 결과와 임베딩은 별도 웹 계층에만 있으며 API `policy`와 `policy_chunk`를 수정하지 않았습니다. 기존 웹 청크에 아직 반영하지 않은 검증 원문 5건의 청크 생성과 float 임베딩은 전체 float 전환 작업까지 보류합니다. 남은 `REVIEW_REQUIRED` 10건은 종료·일몰 표시, 독립 근거 부족, API와 원문 기관값 충돌 또는 반복 판정 불안정 때문에 자동 적용하지 않습니다.

2026-11-07 도메인 파일럿에서는 `youth.gwangju.go.kr` 3건이 모두 HTTP 200과 약 55KB HTML을 반환했지만 정책 본문이 없는 공용 경로 또는 JavaScript shell이어서 `INSUFFICIENT_TEXT`·`UNSUPPORTED`로 종료됐습니다. 같은 URL을 반복 요청하거나 도메인 전용 자동 승인을 추가하지 않았습니다. `youth.incheon.go.kr` 3건은 모두 상세 본문 수집에 성공했고 5개 정책 관계 중 결정적 근거가 충분한 1개만 `VERIFIED`, 나머지 4개는 `REVIEW_REQUIRED`가 됐습니다. 4개 모두 AI shadow에서도 독립 근거 부족, 미검증 모델 근거 또는 API·페이지 기관값 충돌로 보류됐습니다. 전체 현재 문서 재검증 계획은 `stale_ai=0`, `documents=0`이었습니다.
