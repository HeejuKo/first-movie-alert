# 1st Movie Alert

### 원하는 영화·지점·날짜의 **가장 이른 상영 회차**를 발견하면 Discord로 알려주는 서비스

AWS에서 자동 실행되므로 노트북이 꺼져 있어도 감시를 계속한다. 현재는 CGV의 지점 하나와 날짜 하나를 선택해 1분마다 조회한다.

<img width="588" height="352" alt="image" src="https://github.com/user-attachments/assets/c05ec521-8112-40db-af95-5009ab82d778" />


## 주요 기능

- 설정 파일에서 영화·지점·날짜 선택
- 첫 상영 회차 발견 시 Discord 알림
- 같은 회차의 중복 알림 방지
- 연속 조회·알림 처리 실패와 정상 복귀 알림
- GitHub Actions를 통한 코드·설정 자동 배포

시간표 발견 알림은 **실제 예매 가능 여부를 보장하지 않는다.** 현재 알림에는 예매 가능 여부를 `미확인`으로 표시한다.

## 기술 스택

Python 3.13 · uv · AWS Lambda · EventBridge Scheduler · DynamoDB · Discord Webhook · Terraform · GitHub Actions · CloudWatch

## 시작하기

Python 3.13과 [uv](https://docs.astral.sh/uv/getting-started/installation/)가 필요하다.

```bash
uv sync --locked
```

### 감시 대상 선택

[config/watch.example.toml](config/watch.example.toml) 상단의 두 인덱스를 변경한다.

```toml
[selection]
branch_index = 3 # 수유
date_index = 0   # dates 목록의 첫 번째 날짜
```

번호는 **0부터 시작**한다. 지점 번호는 `[[watches]]`, 날짜 번호는 선택한 지점에 적용되는 `dates` 목록의 작성 순서를 따른다. 지점별 번호는 파일 상단 주석에서 확인할 수 있다.

영화·날짜 목록·감시 종료 시각(`active_until`)은 `[watch_defaults]`, 지점 이름과 ID는 `[[watches]]`에서 관리한다. 영화와 지점을 바꿀 때는 해당 이름과 ID를 함께 변경한다.

선택한 날짜가 지났거나 시간표가 없어도 다른 후보로 자동 이동하지 않으며, 감시 기간이 끝나면 조회를 중단한다.

### 조회와 알림 미리보기

```bash
uv run --locked movie-alert --config config/watch.example.toml
```

한 번 조회한 결과와 알림 미리보기를 출력한다. Discord 메시지는 전송하지 않는다.

### Discord 알림 전송

먼저 AWS 로그인과 DynamoDB 테이블을 준비한다. 준비 방법은 [AWS 구성 안내](infra/README.md)를 따른다.

`.env`가 없다면 `.env.example`을 복사하고 다음 값을 입력한다. Webhook URL과 `.env`는 Git에 올리지 않는다.

| 환경 변수                    | 값                             |
| ---------------------------- | ------------------------------ |
| `DISCORD_WEBHOOK_URL`        | 알림을 받을 채널의 Webhook URL |
| `MOVIE_ALERT_DYNAMODB_TABLE` | 준비한 DynamoDB 테이블 이름    |
| `AWS_REGION`                 | AWS 리전, 예: `ap-northeast-2` |

콘솔 단기 로그인용 `movie-alert` 프로필을 사용하는 경우:

```bash
AWS_PROFILE=movie-alert uv run --locked --extra console-login --env-file .env movie-alert \
  --config config/watch.example.toml --notify
```

시작 전 첫 회차를 새로 발견했을 때 전송하며, 이미 알린 회차는 다시 전송하지 않는다.

## AWS 자동 실행과 배포

AWS 초기 구성과 GitHub 배포 설정을 마치면, `main`에 push한 코드와 감시 설정은 검사를 통과한 뒤 자동 배포된다. 이후 EventBridge Scheduler가 Lambda를 매분 실행한다.

**지점·날짜 등 설정 변경도 재배포가 성공해야 AWS에 반영된다.** 초기 설정과 배포 방법은 [배포 안내](docs/deployment.md)를 참고한다.

현재 조회는 제3자 CGV 릴레이를 사용한다. 릴레이의 캐시·호출 제한·가용성에 따라 감지가 지연되거나 조회가 실패할 수 있다. 감시 범위나 주기를 늘리기 전 [운영 안내](docs/operations.md)의 요청 한도를 확인한다.

## 상세 문서

- [AWS 구성과 비용](infra/README.md)
- [GitHub Actions 자동 배포](docs/deployment.md)
- [운영 점검과 릴레이 제약](docs/operations.md)
- [장애 알림과 상태 복구](docs/recovery.md)
