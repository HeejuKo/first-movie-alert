# GitHub Actions 배포

검사를 통과한 `main` push에서 코드와 감시 설정을 함께 배포하도록 준비했다. `DEPLOY_ENABLED=true`일 때만 배포 job이 실행된다. 초기 S3·OIDC 구성, 기존 state 이전, GitHub 설정과 최초 실제 배포 확인이 완료 기준이다. PR·수동 실행은 검사만 수행한다.

2026-10-23 S3·OIDC·배포 역할 생성, state 이전, GitHub 설정과 CI 성공을 확인했다. 최초 자동 배포의 실제 OIDC 인증·plan·apply 확인을 진행 중이다.

## 초기 구성과 state 이전

`infra/bootstrap/`은 앱과 별도 로컬 state로 S3 버킷·OIDC 공급자·배포 역할을 관리한다. 기존 GitHub OIDC 공급자가 있다면 URL·audience를 확인하고 `existing_oidc_provider_arn`으로 재사용한다. 공용 공급자를 중복 생성하거나 삭제하지 않는다.

루트 콘솔에서 공개 예시의 `000000000000`을 실제 계정 번호로 치환한 고객 관리형 정책을 준비한다.

1. [github-deployment-boundary.json.example](../infra/iam/github-deployment-boundary.json.example)을 `MovieAlertGithubDeploymentBoundary`로 생성한다. 사용자에게 연결하지 않는다.
2. [bootstrap-policy.json.example](../infra/iam/bootstrap-policy.json.example)을 `MovieAlertBootstrap`으로 생성해 개발 사용자에 연결한다. 첫 역할 ARN은 `movie-alert-github-deploy`이며 OIDC·S3 초기 구성 권한이 들어 있다. 권한 경계 JSON과 혼동하지 않는다.

기존 `MovieAlertDeployment`와 `MovieAlertRuntimeBoundary`는 유지한다. 새 배포 역할에도 전용 권한 경계를 적용하며 초기 구성 사용자에게 경계 수정 권한을 주지 않는다. 정책 초안은 IAM Policy Autopilot 0.3.0으로 bootstrap plan JSON을 분석한 뒤 관련 없는 서비스·태깅·광범위한 PassRole을 제거하고 역할·버킷·객체로 범위를 제한했다. 생성 결과를 자동 업로드하지 않는다.

```bash
terraform -chdir=infra/bootstrap init -input=false -lockfile=readonly
terraform -chdir=infra/bootstrap validate
terraform -chdir=infra/bootstrap test
AWS_PROFILE=movie-alert terraform -chdir=infra/bootstrap plan \
  -var=expected_account_id=YOUR_ACCOUNT_ID -out=bootstrap.tfplan
# 생성 대상·권한·비용을 검토한 같은 plan만 적용한다.
AWS_PROFILE=movie-alert terraform -chdir=infra/bootstrap apply bootstrap.tfplan
```

bootstrap state·plan도 Git에서 제외하고 소유자만 읽게 유지한다. 버킷에는 `prevent_destroy`를 적용한다. OIDC 신규 구성 시 생성 계획은 11개다.

자동 배포를 끈 상태에서 기존 `infra/terraform.tfstate`와 백업을 안전하게 복사한다. 대상 `state/movie-alert.tfstate`가 비어 있는지 확인하고, 기존 객체가 있다면 lineage·serial·리소스를 비교해 임의로 덮어쓰지 않는다.

```bash
uv run --locked python scripts/configure_backend.py \
  --bucket YOUR_STATE_BUCKET --account YOUR_ACCOUNT_ID
AWS_PROFILE=movie-alert terraform -chdir=infra init -migrate-state \
  -backend-config=backend.local.hcl
```

준비 스크립트는 Git에서 제외된 `backend.local.tf`·`backend.local.hcl`만 작성하며 기존의 다른 S3 backend를 자동 변경하지 않는다. 이전 후 lineage·serial을 기록하고 리소스 ID·전체 리소스 정보·출력값을 원본과 비교한다. 이번 이전에서는 새 lineage·serial 1이 생성됐지만 리소스와 출력값은 모두 동일했다. 기존 plan은 재사용하지 않고 원격 state에서 다시 plan해 재생성 여부를 확인한다. 빈 state로 기존 리소스를 재생성하거나 알림 `SENT` 기록을 지우지 않는다. 로컬 백업도 계속 보호한다.

S3에는 Public Access Block·BucketOwnerEnforced·SSE-S3 암호화·버전 관리·TLS 강제를 적용한다. Terraform 1.10 이상의 `use_lockfile=true`로 쓰기 잠금을 사용한다. 역할의 `DeleteObject`는 `.tflock`에만 허용한다. [S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3)

## GitHub 설정과 배포

저장소 **Settings → Secrets and variables → Actions**에 다음을 등록한다. 장기 AWS Access Key는 등록하지 않는다.

| 종류 | 이름 | 값 |
| --- | --- | --- |
| Variable | `AWS_ACCOUNT_ID` | 배포 대상 12자리 계정 ID |
| Variable | `DEPLOY_ROLE_ARN` | bootstrap의 `deploy_role_arn` 출력 |
| Variable | `TF_STATE_BUCKET` | bootstrap의 `state_bucket` 출력 |
| Secret | `DISCORD_WEBHOOK_URL` | 로컬 `.env`의 현재 Webhook 값 |
| Variable | `DEPLOY_ENABLED` | 이전·검증 완료 후에만 `true` |

이 저장소는 2026-07-15 이후 생성되어 공개 GitHub API에서 확인한 owner/repository ID를 포함한 immutable OIDC subject를 사용하도록 작성했다. `main` ref와 `sts.amazonaws.com` audience만 허용하고 최초 실제 OIDC 로그인도 확인한다. 실패했다고 PR이나 전체 저장소로 신뢰 범위를 넓히지 않는다. [GitHub AWS OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)

활성화 후 `main` push는 검사 → OIDC 로그인 → 기존 S3 backend 연결 → 패키징 → plan 검토 → apply 순서다. 생성·삭제·교체 또는 필수 운영 state 누락은 자동 적용하지 않고 별도 검토한다. 동시 배포와 실행 중 배포 취소를 막는다. 지점·날짜 인덱스 변경도 배포 대상이다.

이 배포는 스케줄 활성 상태를 유지한다. 운영 중지는 먼저 `DEPLOY_ENABLED`를 끄고 기존 수동 `plan` → 검토 → `apply`로 스케줄을 끈다. 롤백은 검증된 이전 코드·설정을 새 커밋으로 복원해 배포하며 state나 알림 기록을 되돌리지 않는다. 배포 후 Actions 결과·Lambda 갱신·Scheduler 상태·다음 자동 조회를 확인한다. 새 회차의 Discord 실제 전송은 별도로 검증한다.

## 비밀값·비용·초기 권한 정리

Webhook은 GitHub encrypted secret → Terraform 환경 변수 → Lambda 환경 변수로 전달한다. **state·plan에는 여전히 평문 Webhook이 저장된다.** `sensitive`는 출력 마스킹이며 저장 내용을 제거하지 않는다. plan JSON은 메모리 안에서만 검토하고 공개 artifact로 올리지 않는다. 별도 Secrets Manager는 이번 구성에 포함하지 않았다.

초기 구성 완료 후 콘솔에서 개발 사용자에 backend 전용 정책(bootstrap의 `developer_state_policy_json` 출력)을 연결한 뒤 `MovieAlertBootstrap`을 해제한다. 역할 관리·버킷 생성 권한을 일상 운영에 남기지 않는다. state 버전 장기 보관·접근 감사·비밀 분리는 후속 운영 개선 대상이다.

2026-10-23 [서울 S3 Price List](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonS3/current/ap-northeast-2/index.json)에서 Standard 첫 구간은 GB-month당 USD 0.025, PUT/COPY/POST/LIST는 1,000회당 USD 0.0045, GET은 10,000회당 USD 0.0035를 확인했다. 모든 버전 합계 0.01GB·월 쓰기/LIST 1,000회·GET 10,000회를 가정하면 약 USD 0.00825이며 전송·세금·Free Tier 자격은 별도다. 버전 증가에 따라 저장 비용이 증가하며 현재 자동 만료 정책은 없다. 별도 KMS 키·NAT Gateway는 추가하지 않는다. [S3 가격](https://aws.amazon.com/s3/pricing/)
