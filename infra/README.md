# Movie Alert AWS 구성과 운영

현재 DynamoDB·Lambda·Scheduler·실행 역할·7일 보존 로그 그룹을 Terraform으로 관리한다. 2026-10-23 로컬 CLI의 실제 Discord 전송·상태 저장과 배포한 Lambda의 자동 조회·중복 차단을 확인했다. 스케줄은 활성 상태이며 컴퓨터가 꺼져 있어도 실행된다. 개발 IAM 사용자와 초기 관리형 정책은 콘솔에서 준비한다.

일별 운영 집계 도구, 신규 전송·비용 확인 기준, 릴레이 호출 한도와 CI 도입 상태는 [운영 확인 문서](../docs/operations.md)를 따른다. S3 state·GitHub OIDC·자동 배포 초기 구성과 완료 조건은 [배포 안내](../docs/deployment.md)에 정리했다.

## 1. 개발용 AWS 로그인

콘솔에 로그인한 상태와 CLI 인증은 별개다. `default` 프로필은 2026-10-23 확인 시 `InvalidClientTokenId`였으며, 기존 프로필과 인증 파일을 수정하지 않았다.

루트 사용자는 초기 IAM 사용자 준비에만 사용하고 프로젝트 작업에는 `movie-alert-dev` IAM 사용자와 `movie-alert` CLI 프로필을 사용한다.

1. AWS 콘솔에서 계정 ID(12자리)를 확인한다.
2. IAM → 사용자 → 사용자 생성에서 `movie-alert-dev`를 만들고 콘솔 액세스를 활성화한다. 콘솔 로그인용 IAM 사용자를 생성하는 옵션을 선택한다.
3. AWS 관리형 정책 `SignInLocalDevelopmentAccess`를 연결한다. 관리자 권한은 추가하지 않는다.
4. [developer-policy.json.example](iam/developer-policy.json.example)의 `000000000000`을 실제 계정 ID로 바꾼 내용을 인라인 정책 `MovieAlertStateDevelopment`로 연결한다. 다른 리전·테이블을 쓸 때는 해당 ARN도 수정한다. 정책은 지정 테이블의 생성·설정·정리 및 앱의 상태 갱신만 허용한다.
5. IAM 사용자의 콘솔 로그인과 MFA를 설정한다. 계정 ID·IAM 사용자 이름으로 새 콘솔 세션에 로그인한다.

AWS CLI 2.32.0 이상과 프로젝트의 선택 의존성 `console-login`이 필요하다. 설치된 CLI가 더 오래된 버전이면 먼저 업데이트한다. [AWS 공식 로그인 문서](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sign-in.html), [Python SDK 로그인 요구사항](https://docs.aws.amazon.com/sdkref/latest/guide/feature-login-credentials.html)을 따른다.

```bash
aws login --profile movie-alert --region ap-northeast-2
aws sts get-caller-identity --profile movie-alert --region ap-northeast-2
uv sync --locked --extra console-login
```

브라우저에서는 `movie-alert-dev` 세션을 선택한다. `GetCallerIdentity`의 ARN이 루트가 아닌 의도한 IAM 사용자인지 확인한다. 새 프로필을 선택해 오래된 `default` 인증과 분리한다. `AWS_ACCESS_KEY_ID` 등 기존 인증 환경 변수가 설정돼 있으면 그 값이 프로필보다 우선할 수 있으므로 실행 환경을 확인한다. 장기 Access Key를 발급하거나 `.env`에 넣는 절차는 필요 없다. 단기 로그인 세션이 끝나면 `aws login`을 다시 실행한다.

앱은 설정 리전을 `boto3.Session`에 전달한다. 프로필에 리전이 없어도 임시 자격 증명 갱신에 필요한 Sign-In 클라이언트가 같은 리전을 사용하도록 하기 위한 것이다.

개발 IAM 사용자의 생성은 콘솔 로그인을 위한 초기 준비다. Terraform은 이 사용자를 생성·변경하지 않는다. 향후 Lambda 실행 역할에는 아래 개발 권한 전체를 주지 않고 `runtime_dynamodb_policy_json`의 테이블별 `UpdateItem`만 허용한다.

## 2. 비용과 최소 구성

- 기본 리전: `ap-northeast-2`(서울).
- 테이블: `movie-alert-state`, `STANDARD`, `PROVISIONED`, 1 RCU / 1 WCU.
- 문자열 partition key: `id`, TTL 속성: `expires_at`.
- AWS 소유 키를 사용하는 기본 저장 암호화를 사용한다. 별도 KMS 키, Streams, PITR, 자동 확장, 복제, VPC, NAT Gateway는 만들지 않는다.
- 삭제 보호를 켜 중복 방지 기록이 실수로 사라지는 것을 막는다.

2026-10-23 확인한 [AWS DynamoDB 가격](https://aws.amazon.com/dynamodb/pricing/)은 Standard provisioned 용량에 25 RCU·25 WCU, 저장 공간에 25 GB의 Free Tier를 안내한다. 이는 계정·리전의 다른 사용량과 자격 조건을 함께 확인해야 하며 무료 운영을 보장하지 않는다.

[서울 리전 공식 Price List](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonDynamoDB/current/ap-northeast-2/index.json)의 Free Tier 초과 단가는 RCU당 시간당 USD 0.00014098, WCU당 시간당 USD 0.0007049이다. Free Tier·크레딧을 적용하지 않고 기본 용량을 30일(720시간) 유지하면 용량 비용은 약 **USD 0.61/월**이다. 저장 공간은 무료 범위를 초과한 경우 GB당 월 USD 0.27075이며 데이터 전송·세금 등은 별도다. 단가는 적용 전에 다시 확인한다.

On-demand Standard 단가는 읽기 100만 단위당 USD 0.1355, 쓰기 100만 단위당 USD 0.68이다. 사용량이 작고 Free Tier가 적용되지 않으면 on-demand가 더 저렴할 수 있으나, 현재는 provisioned Free Tier를 활용하는 최소 고정 용량으로 시작한다. 용량·과금 모델 변경은 실제 계정 사용량과 조건부 쓰기·실패 요청의 과금도 확인한 뒤 수행한다. 동시 감지로 throttling이 발생하면 측정 후 용량을 조정한다.

## 3. Terraform 검증과 적용

Terraform 1.10 이상·2 미만을 사용한다. 현재 검증 버전은 1.17.0이며 설치 방법은 [공식 문서](https://developer.hashicorp.com/terraform/install)를 따른다.

```bash
terraform -chdir=infra init
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra validate
terraform -chdir=infra test
```

`terraform test`는 mock AWS provider로 계약을 검사하며 실제 AWS 리소스를 만들지 않는다.

[terraform.tfvars.example](terraform.tfvars.example)을 `infra/terraform.tfvars`로 복사하고 `expected_account_id`를 로그인한 AWS 계정 ID로 바꾼다. `allowed_account_ids`가 다른 계정에 적용하는 실수를 차단한다. 기본 리전·테이블 이름을 바꿨다면 IAM 정책의 ARN도 맞춘다.

```bash
AWS_PROFILE=movie-alert terraform -chdir=infra plan -out=notification.tfplan
AWS_PROFILE=movie-alert terraform -chdir=infra apply notification.tfplan
terraform -chdir=infra output
```

최초 적용 전에 plan이 의도한 테이블 한 개의 생성만 포함하는지 확인한다. 이후 변경도 새로운 plan으로 검토하며 이미 적용한 plan을 재사용하지 않는다. Terraform state에 없는 동명 테이블이 이미 있으면 새로 만들거나 설정을 덮어쓰지 말고 테이블 구조·TTL·과금 모델을 확인한 뒤 import와 차이를 검토한다.

초기 state는 `infra/terraform.tfstate`에 로컬 저장한다. state·plan·실제 tfvars·`.terraform/`는 Git에서 제외하며 state를 외부에 공개하거나 삭제하지 않는다. 로컬 state와 백업의 접근 권한을 제한하고 안전하게 보관한다. `.terraform.lock.hcl`은 커밋한다. 여러 사람이 적용하거나 CI 배포를 시작하기 전에 암호화·접근 통제·잠금이 있는 원격 backend를 도입한다.

테이블 정리는 감시 종료와 상태 보관 기간을 확인한 뒤 수행한다. 삭제 보호 해제는 `deletion_protection_enabled = false`의 plan을 검토·적용하는 별도 단계다. 상태를 삭제하면 같은 회차가 다시 알림 대상이 될 수 있다.

## 4. 실제 Discord 전송과 중복 방지 확인

테이블 생성이 완료된 뒤 루트의 Git 제외 파일 `.env`에 다음 비밀이 아닌 값 두 개를 추가한다. 기존 Webhook 값은 유지한다.

```dotenv
MOVIE_ALERT_DYNAMODB_TABLE=movie-alert-state
AWS_REGION=ap-northeast-2
```

```bash
AWS_PROFILE=movie-alert uv run --locked --extra console-login --env-file .env movie-alert \
  --config config/watch.example.toml --notify
```

1. 실제 조회가 오류 없이 끝나고 시작 전 회차가 발견됐을 때만 알림을 보낸다.
2. 첫 실행의 `notifications.outcomes`에서 `sent`와 Discord 메시지를 확인한다.
3. 같은 명령을 다시 실행해 같은 회차가 `not_claimed`이며 추가 메시지가 생기지 않는지 확인한다.
4. `state_error`·`uncertain`·`failed`가 있으면 실행이 완료된 것으로 간주하지 않고 원인을 확인한다. 임의로 상태를 지우거나 Webhook만 직접 호출해 우회하지 않는다.

2026-10-23 실제 검증 결과는 다음과 같다.

- 테이블은 `ACTIVE`, TTL은 `expires_at`에 `ENABLED`, 삭제 보호는 활성 상태였다. 적용 후 새 plan은 변경 없음이었다.
- 강변 10월 28일 10:30, 수유 10월 28일 11:00, 강변 10월 31일 14:40(KST) 첫 회차 3건이 `sent`로 처리됐다. Discord 메시지 ID 확인과 DynamoDB `SENT` 기록이 모두 성공했다.
- 임시 인증 갱신 시 리전 누락을 수정한 뒤 같은 설정으로 다시 실행했을 때, 같은 3건은 모두 `not_claimed`였으며 추가 전송이 없었다.

이는 로컬 단발 실행의 실연동 검증이다. 실환경 동시 실행·장애 복구는 별도 검증이 필요하다.

## 5. Lambda와 Scheduler 준비

`movie-alert-checker`는 Python 3.13·ARM64·256MB·타임아웃 180초를 사용한다. VPC·NAT Gateway·S3 패키지 버킷·Provisioned Concurrency는 추가하지 않는다. ZIP은 `uv.lock`의 운영 의존성을 Linux ARM64 대상으로 설치한 뒤 소스와 지정 TOML만 담는다. 로컬 `.env`·인증 파일·테스트·개발 의존성·`console-login` extra는 패키지에 포함하지 않는다.

Scheduler는 `rate(1 minute)`, flexible window OFF, 기본 비활성으로 생성한다. 현재 자동 감시는 `checks: 1`로 매분 한 번 조회하며 30초 추가 대기는 없다. HTTP 단계별 타임아웃·재시도·릴레이 교차 확인을 고려한 여유 시간이 부족하면 새 조회를 시작하지 않는다. 조회 실패 시 알림을 보내지 않는다. Scheduler와 Lambda 비동기 재시도는 0회·이벤트 최대 수명은 60초이고, 다음 정기 호출이 새로 조회한다. 앱은 90초를 초과해 지연된 스케줄 호출과 감시 기간 밖 호출을 추가 차단한다. DLQ는 없으므로 실패는 CloudWatch Errors와 로그에서 확인한다.

Scheduler 입력의 `<aws.scheduler.scheduled-time>`은 AWS가 실행 시각으로 치환한다. Terraform `jsonencode`는 꺾쇠를 Unicode escape로 바꾸므로 해당 입력은 literal JSON heredoc으로 유지한다. JSON으로 해석했을 때 같은 값이어도 Scheduler 치환 단계의 결과는 다를 수 있으며 모의 테스트에서 원문 placeholder를 검사한다.

이 계정은 reserved concurrency 1을 지정하면 예약되지 않은 동시성의 최소값 10을 만족하지 못해 AWS가 거부했다. 기본 `reserved_concurrency = -1`로 예약을 생략하고 DynamoDB의 별도 `polling-lock:v1` 항목으로 Lambda 조회 실행을 원자적으로 확보한다. 다른 소유자의 유효한 잠금이 있으면 조회를 생략하며, 정상·오류 종료 모두 같은 소유자 조건으로 해제한다. 잠금 저장 실패 시 조회하지 않는다. 비정상 종료 시에는 Lambda 잔여 실행 시간보다 60초 긴 lease가 만료된 뒤 복구한다. TTL은 청소 용도이며 잠금 판정에는 쓰지 않는다. 향후 계정 quota가 허용하면 `reserved_concurrency = 1`도 추가할 수 있다. 회차별 알림 중복 방지는 별도 기존 상태가 담당하고 로컬 CLI는 Lambda 조회 잠금을 사용하지 않는다.

종료 시각은 패키징할 때 활성 watch의 `active_until` 최댓값에서 가져와 Scheduler `end_date`와 같은 설정을 사용한다. 현재 설정은 KST `2026-11-08T00:00:00+09:00` 미만이다. 시작일이 미래면 Scheduler에도 지정하고, 이미 지났으면 `UpdateSchedule`의 과거 시작일 제한 때문에 생략한다. 앱은 계속 각 watch의 `active_from`을 검사한다. 설정은 ZIP에 포함되므로 날짜·지점·종료일 변경 후 다시 plan·apply해야 AWS에 반영된다. 로컬 파일 수정만으로 배포된 설정이 바뀌지 않는다.

### 초기 배포 권한

기존 `MovieAlertStateDevelopment`는 DynamoDB 전용이다. IAM 콘솔에서 다음 두 고객 관리형 정책을 준비한다. 예시의 `000000000000`을 실제 계정 ID로 바꾸고 이름·리전·리소스 범위를 검토한다.

1. [runtime-boundary.json.example](iam/runtime-boundary.json.example)을 `MovieAlertRuntimeBoundary`로 생성한다. 사용자에게 연결하지 않는다. Terraform이 생성하는 Lambda·Scheduler 역할의 permissions boundary로 사용하며, 상태 테이블 갱신·해당 로그 그룹 쓰기·해당 Lambda 호출만 상한으로 허용한다.
2. [deployment-policy.json.example](iam/deployment-policy.json.example)을 `MovieAlertDeployment`로 생성하고 `movie-alert-dev`에 연결한다. 기존 정책은 유지한다.

두 정책 모두 **IAM 왼쪽 메뉴 → 정책 → 정책 생성 → JSON**에서 고객 관리형 정책으로 만든다. 사용자 화면의 ‘인라인 정책 추가’는 사용하지 않는다. 사용자 인라인 정책은 합계 2,048자, 고객 관리형 정책은 정책당 6,144자 제한이며, 배포 JSON은 공백 제외 2,861자다. [IAM 정책 크기 제한](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_iam-quotas.html)을 참고한다.

개발자는 지정한 두 역할만 관리·전달할 수 있다. 역할 생성에는 위 boundary가 필수이고 boundary 자체 수정·제거 권한은 없다. `PassRole`은 역할별 Lambda 또는 Scheduler 서비스로 제한한다. Logs `DescribeLogGroups`는 ARN 제한을 지원하지 않아 서울 리전의 조회만 허용한다. 그 외 배포 대상은 함수 하나·로그 그룹 하나·스케줄 그룹과 스케줄 하나로 제한한다.

정책은 IAM Policy Autopilot 0.3.0의 정적 분석 결과를 검토해 불필요한 서비스·작업·와일드카드를 제외한 것이다. 실행 코드 분석에는 다음 명령을 사용했다. 생성 결과의 KMS 권한은 고객 관리 KMS 키를 사용하지 않는 현재 구성에 추가하지 않았다.

```bash
DISABLE_IAM_POLICY_AUTOPILOT_TELEMETRY=true uvx iam-policy-autopilot@latest generate-policies \
  "$PWD/src/movie_alert/runtime.py" "$PWD/src/movie_alert/state.py" \
  --region ap-northeast-2 --account YOUR_ACCOUNT_ID --service-hints dynamodb \
  --tf-dir "$PWD/infra" --tfstate "$PWD/infra/terraform.tfstate" --pretty
```

배포 정책은 비밀값을 제거한 실제 Terraform 생성 plan JSON을 분석했으며, 갱신·정리에 대한 권한은 같은 리소스의 합성 lifecycle 입력으로 추가 분석했다. 합성 입력은 IAM 생성용이며 실제 검토·적용 가능한 Terraform plan이 아니다. 생성 결과를 자동 업로드하지 않는다.

### 배포와 활성화

```bash
uv run --locked python scripts/build_lambda.py
terraform -chdir=infra fmt -check -recursive
terraform -chdir=infra validate
terraform -chdir=infra test

# 패키지를 다시 만들고, 스케줄이 꺼진 최초 배포 계획을 저장한다.
uv run --locked --extra console-login --env-file .env python scripts/terraform_lambda.py plan
uv run --locked --extra console-login --env-file .env python scripts/terraform_lambda.py apply

# 첫 검사는 실제 조회만 수행한다. --notify를 지정하면 기존 상태를 사용해 실제 알림을 전송한다.
AWS_PROFILE=movie-alert uv run --locked --extra console-login python scripts/check_lambda.py
AWS_PROFILE=movie-alert uv run --locked --extra console-login python scripts/check_lambda.py --notify --checks 1

# 위 검증이 성공하면 새 계획으로 스케줄을 활성화한다.
uv run --locked --extra console-login --env-file .env python scripts/terraform_lambda.py plan --enable-schedule
uv run --locked --extra console-login --env-file .env python scripts/terraform_lambda.py apply
AWS_PROFILE=movie-alert uv run --locked --extra console-login python scripts/check_lambda.py --scheduled
```

실행 도구가 기본 PATH에 없다면 `terraform_lambda.py --terraform /absolute/path/to/terraform`을 사용한다. `plan`은 패키지·manifest와 Git 제외 파일 `infra/deployment.auto.tfvars.json`을 갱신한다. `apply`는 저장한 plan만 적용하며 이미 적용한 plan을 재사용하지 않는다. 활성화·비활성화 상태는 plan 단계에서 지정해야 한다. 활성화 후 일반 `plan`을 실행하면 스케줄을 끄는 계획이므로 상태를 유지하려면 `plan --enable-schedule`을 사용한다.

Webhook은 셸 인수나 ZIP에 넣지 않고 `.env`에서 Terraform 환경 변수로 전달한다. **Terraform state와 plan에는 평문 비밀값이 저장된다.** `sensitive`는 출력 마스킹일 뿐 저장 암호화가 아니다. 도구는 `umask 077`을 사용하며 파일·백업은 소유자만 읽도록 유지한다. state·plan을 채팅·공개 아티팩트·Git에 올리지 않는다. Lambda 환경 변수는 AWS에서 저장 시 암호화된다. 공유 운영·CI 도입 전에 state 보안과 비밀 저장 방식을 다시 검토한다.

스케줄 중지는 `plan` → 검토 → `apply`로 수행한다. 함수 코드를 되돌릴 때도 이전 커밋을 별도 checkout에서 패키징한 뒤 현재 state를 보존하며 plan 차이를 검토한다. 중복 방지 테이블을 삭제하거나 state를 초기화하지 않는다. 종료 뒤 Lambda·역할·스케줄만 정리하려면 `deploy_lambda = false` 계획에서 테이블 삭제가 없는지 먼저 확인한다.

### 추가 비용과 요청량

현재는 매분 한 번 조회하고 30초 대기를 제거했다. 아래 35/40/60초 수치는 실행 시간별 비용 비교 예시이며 현재 실행 시간이 해당 값으로 고정됐다는 뜻은 아니다. 변경 후 CloudWatch 실행 시간을 기준으로 비용을 다시 추정한다.

2026-10-23 [서울 리전 Lambda Price List](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AWSLambda/current/ap-northeast-2/index.json)의 ARM 첫 구간은 GB-second당 USD 0.0000133334, 요청 100만 건당 USD 0.20이다. 30일·매분 1회 호출은 43,200건이다. 256MB·평균 35/40/60초면 각각 378,000/432,000/648,000 GB-seconds이며 Free Tier 미적용 시 Lambda 비용은 요청 포함 약 USD 5.05/5.77/8.65다. 대기·초기화·실패 실행도 과금된다. 실제 실행 시간으로 다시 계산하며 무료라고 단정하지 않는다.

[Lambda 가격](https://aws.amazon.com/lambda/pricing/)은 월 100만 요청·400,000 GB-seconds, [Scheduler 가격](https://aws.amazon.com/eventbridge/pricing/)은 월 1,400만 호출 Free Tier를 안내한다. 계정의 다른 사용량·자격·크레딧 조건은 미확인이다. Scheduler는 Free Tier 미적용 시 100만 호출당 USD 1 기준으로 43,200건에 약 USD 0.0432이며, DynamoDB·CloudWatch 로그 수집/7일 보관·전송·세금은 별도다. IAM 역할·권한 경계에는 별도 실행 요금이 없다.

현재 설정은 `[selection]`에서 선택한 한 지점·한 날짜를 매분 한 번 조회하며 재시도는 0회다. 시간표 요청은 하루 최대 1,440건, 빈 결과 교차 확인까지 최대 2,880건이다. 인덱스는 후보 목록의 작성 순서를 따르며 변경 후 재배포해야 반영된다. 수동 호출이나 같은 IP의 다른 사용량은 별도다. 조회 잠금 확보·해제에는 실행마다 DynamoDB 조건부 쓰기 2회가 추가된다. 릴레이 시간표는 3분·영화 목록은 10분 캐시이므로 조회마다 새 데이터가 보이는 것은 보장하지 않는다. 지속 운영 허용 범위·가용성은 여전히 확인 대상이다.

## 6. 최초 실배포 검증 기록 — 30초 재확인 방식

2026-10-23 배포 후 다음을 확인했다.

- Lambda는 `Active`·Python 3.13·ARM64·256MB이며 실제 조회에서 시간표 3건을 감지했다.
- 실제 알림 모드의 한 실행 안에서 두 차례 확인이 성공했고, 이미 로컬에서 전송한 3건은 각 확인에서 `not_claimed`로 차단했다. 별도로 겹쳐 보낸 호출은 `already_running`으로 조회를 생략했다.
- Scheduler는 `ENABLED`이며 KST 11월 8일 00:00 종료다. 수동 호출 없이 KST 20:09~20:11 사이 연속 두 자동 실행의 각 두 차례 확인에서 감지 3·오류 0·중복 차단 3을 확인했다.
- 자동 실행 소요 시간은 약 33초, 최대 메모리 사용은 관측한 실행 중 135MB였다. 향후 실행 시간·요청량·비용은 서비스 응답에 따라 달라진다.
- 배포 뒤 Terraform plan은 변경 없음이었고, Python 테스트 270개·Ruff·Terraform 모의 테스트 3개를 통과했다. 실환경 강제 종료·Discord 전송 실패 복구와 장기 가용성은 별도 검증 대상이다.

2026-10-23 KST 20:33에 매분 한 번 조회하도록 변경했다. 변경 후 20:34·20:35 자동 실행은 각각 한 번만 조회하고 종료했으며, 감지 3·오류 0·중복 차단 3을 확인했다. 실행 시간은 각각 약 11.3초·4.3초였다. Python 테스트 271개·Ruff·Terraform 모의 테스트 3개를 통과했고 적용 후 plan은 변경 없음이었다.

감지된 새 회차가 없으면 Discord 메시지가 오지 않는 것이 정상이다. 실행 상태는 `scripts/check_lambda.py --scheduled`와 CloudWatch 로그에서 확인한다. 날짜 변경 뒤에는 위의 `plan --enable-schedule` → 검토 → `apply` 순서로 다시 배포한다.
