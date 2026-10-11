# YouthLink 정책 ETL 호환 안내

이 파일은 기존 링크를 위한 짧은 안내입니다. 설치, 환경변수, DB·지역 구성 순서, 변환 규칙, 오류 처리 및 검증 SQL의 기준 문서는 [README.md](README.md)입니다. 두 문서가 다르면 메인 README를 따릅니다.

모든 명령은 저장소 루트에서 실행합니다. `.env.example`을 복사해 `.env`를 만들고 필요한 API 키와 `DATABASE_URL`을 입력하세요. `.env`는 Git에 커밋하지 않습니다.

로컬 JSON 적재:

```bash
python etl/load_policies.py file sample_response.json
```

온통청년 API 전체 페이지 적재:

```bash
python etl/load_policies.py api

# 이번 실행의 페이지 크기만 지정
python etl/load_policies.py api --page-size 100
```

두 명령은 현재 CLI에서 정상 지원됩니다. 정책은 `policy_no` 기준으로 UPSERT되고, 정책별 지역·자격 조건은 현재 응답에 맞춰 다시 동기화됩니다. 자세한 실행 결과 확인과 실패 로그 해석은 메인 README를 참고하세요.
