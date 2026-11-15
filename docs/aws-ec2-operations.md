# AWS EC2 운영 자동화

YouthLink 운영 서버는 **Amazon Linux 2023 또는 Ubuntu처럼 systemd를 사용하는 EC2 Linux**를 기준으로 합니다. Windows 예약 작업은 로컬 개발 PC에서만 사용하고, EC2에서는 정책 API pipeline과 웹 pipeline을 systemd 서비스로 실행합니다.

## 실행 구조

```text
youthlink-policy-pipeline.timer
  매일 04:30 Asia/Seoul, 누락 실행 보완
        │
        ▼
youthlink-policy-pipeline.service
  정책 API → 청크 → 임베딩 → RAG 발행 → 검증
        │ 성공한 경우에만
        ▼
youthlink-web-pipeline.service
  URL 동기화 → 제한 크롤링 → AI 보조 검증
  → 웹 청크·임베딩 → RAG 발행 → 전체 상태 점검
```

두 Python pipeline은 기존 PostgreSQL advisory lock, 단계 이력, 실패 단계 재개 기능을 그대로 사용합니다. systemd는 실행 예약, 프로세스 재시작, 부팅 후 누락 실행, 서버 로그만 담당합니다.

## EC2 준비 조건

- Amazon Linux 2023 또는 Ubuntu 22.04 이상
- 서버에서 정책 API와 LiteLLM Gateway로 나가는 HTTPS(443) 허용
- PostgreSQL이 RDS라면 EC2 보안 그룹에서 DB 보안 그룹의 PostgreSQL 포트 접근 허용
- ETL 자체를 위해 외부에 새 인바운드 포트를 열 필요는 없음
- 서버 시간대와 무관하게 timer가 `Asia/Seoul` 04:30을 사용

## 최초 설치

애플리케이션을 실행할 일반 Linux 계정으로 저장소와 Python 환경을 준비합니다. 아래 `/opt/youthlink-data-pipeline`은 실제 배포 경로로 바꿀 수 있지만 공백 없는 절대 경로를 사용합니다.

```bash
sudo mkdir -p /opt/youthlink-data-pipeline
sudo chown "$(id -un):$(id -gn)" /opt/youthlink-data-pipeline
git clone https://github.com/inha-youthlink/youthlink-data-pipeline.git \
  /opt/youthlink-data-pipeline
cd /opt/youthlink-data-pipeline

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# .env에 실제 DB·API·LiteLLM 설정 입력
chmod 600 .env
```

비밀값은 Git, unit 파일, 실행 명령 인자에 넣지 않습니다. 서버의 `.env`는 서비스 계정만 읽을 수 있게 유지합니다. 운영 안정화 후에는 같은 환경변수를 AWS Systems Manager Parameter Store 또는 Secrets Manager에서 배포하도록 확장할 수 있습니다.

기존 운영 DB라면 migration 상태를 확인하고 적용합니다.

```bash
.venv/bin/python etl/apply_migrations.py --status
.venv/bin/python etl/apply_migrations.py
```

설치 전에 비밀값을 출력하지 않는 사전 검사를 실행합니다.

```bash
bash ops/aws/install_systemd.sh \
  --check-only \
  --repo-dir "$PWD" \
  --service-user "$(id -un)"
```

검사가 통과하면 systemd 서비스를 설치합니다.

```bash
sudo bash ops/aws/install_systemd.sh \
  --repo-dir "$PWD" \
  --service-user "$(id -un)"
```

설치 명령은 정책 timer만 활성화합니다. 웹 서비스는 별도 시간 예약을 두지 않고 정책 서비스가 성공한 경우에만 자동 시작합니다.

## 최초 운영 검증

정기 실행을 기다리지 않고 정책부터 전체 흐름을 1회 실행합니다.

```bash
sudo systemctl start youthlink-policy-pipeline.service
```

정책 작업이 성공하면 웹 작업이 자동으로 이어집니다. 상태와 로그는 다음 명령으로 확인합니다.

```bash
systemctl status youthlink-policy-pipeline.service --no-pager
systemctl status youthlink-web-pipeline.service --no-pager
systemctl list-timers youthlink-policy-pipeline.timer --no-pager

journalctl -u youthlink-policy-pipeline.service -n 200 --no-pager
journalctl -u youthlink-web-pipeline.service -n 200 --no-pager
```

DB까지 포함한 최종 상태는 서비스 계정으로 확인합니다.

```bash
cd /opt/youthlink-data-pipeline
.venv/bin/python etl/check_pipeline_health.py --json
```

서버 접근 권한이 없는 팀원에게 결과를 전달해야 한다면 다음 읽기 전용 점검 명령을 사용합니다. 이 명령은 정책 API, 크롤링, AI 검토를 새로 실행하지 않으며 비밀값도 출력하지 않습니다.

```bash
cd /opt/youthlink-data-pipeline
sudo bash ops/aws/verify_systemd.sh
```

출력의 `verification_result=OK`와 함께 timer 예약, 두 서비스의 마지막 실행 시각·종료 상태, DB health JSON을 확인합니다. 실패하면 출력 마지막의 `journalctl` 명령으로 오류 로그를 별도로 확인합니다. 최초 실행 전에는 두 서비스의 실행 이력이 없으므로 점검이 실패하는 것이 정상입니다.

## 장애와 재실행

- 각 서비스 실패 시 15분 간격으로 최대 2회 자동 재시도
- Python pipeline은 최근 실패 작업이 있으면 실패 단계부터 새 작업으로 재개
- 정책 pipeline이 최종 실패하면 웹 pipeline은 시작하지 않음
- `Persistent=true`이므로 예약 시각에 EC2가 중지돼 있었다면 다음 부팅 후 누락 실행
- DB advisory lock으로 수동 실행과 예약 실행의 중복 처리 방지
- `verify_systemd.sh`는 상태만 읽으므로 API·임베딩·AI 토큰 비용 없음

수동 재실행은 다음과 같습니다.

```bash
sudo systemctl reset-failed youthlink-policy-pipeline.service
sudo systemctl start youthlink-policy-pipeline.service

# 정책 재수집 없이 웹 작업만 재실행할 때
sudo systemctl reset-failed youthlink-web-pipeline.service
sudo systemctl start youthlink-web-pipeline.service
```

## 코드 업데이트

서비스가 실행 중이 아닌지 확인한 뒤 서비스 계정으로 코드를 갱신합니다.

```bash
cd /opt/youthlink-data-pipeline
git pull --ff-only
.venv/bin/pip install -r requirements.txt
.venv/bin/python etl/apply_migrations.py
```

`ops/aws`의 unit template이 바뀐 배포에서는 설치 명령을 다시 실행합니다. Python 코드만 바뀌었다면 다음 예약 실행부터 새 코드가 사용됩니다.

## Windows 자동화 파일 유지 이유

`ops/*.ps1`은 삭제하지 않습니다. 로컬 개발 PC에서 실제 데이터를 확인하거나 서버 장애 시 제한적으로 실행하는 보조 수단이며, 이미 등록된 Windows 작업도 해당 경로를 사용하기 때문입니다. README에서 Windows는 **로컬용**, systemd는 **AWS 운영용**으로 역할을 구분합니다.
