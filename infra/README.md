# DynamoDB 구성과 실행

현재 Terraform 구성은 알림 중복 방지용 DynamoDB 테이블 하나를 관리한다. 2026-10-23 실제 적용 후 로컬 CLI에서 Discord 전송·완료 상태 저장·재실행 시 중복 차단을 확인했다. Lambda·Scheduler·IAM 사용자·IAM 정책은 이 구성에서 생성하지 않으며 자동 실행은 아직 없다.

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

Terraform 1.7 이상·2 미만을 사용한다. 현재 검증 버전은 1.17.0이며 설치 방법은 [공식 문서](https://developer.hashicorp.com/terraform/install)를 따른다.

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

이는 로컬 단발 실행의 실연동 검증이다. Lambda에서의 조회·실행, Scheduler 자동 감시, 실환경 동시 실행·장애 복구는 별도 검증이 필요하다.
