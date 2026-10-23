# 운영 확인과 다음 단계

## 1. 운영 안정성 — 관찰 진행 중

며칠간의 가용성, 신규 회차의 AWS→Discord 전송, 실제 청구 비용은 아직 검증 완료가 아니다. 다음 명령은 Lambda를 호출하거나 Discord·DynamoDB 상태를 변경하지 않고 서울 리전 로그를 집계한다.

```bash
AWS_PROFILE=movie-alert uv run --locked --extra console-login python scripts/operations_report.py --hours 24

# 배포 초기 검사와 구분할 때는 최근 72시간 이내의 시작 시각을 지정한다.
AWS_PROFILE=movie-alert uv run --locked --extra console-login python scripts/operations_report.py \
  --since 2026-10-23T20:35:00+09:00
```

모든 로그 페이지에서 조회 오류, handler 실패 유형, 알림 결과, 실행 시간, 메모리 최대치, 과금 대상 GB-seconds를 집계한다. 원문 로그·Webhook·환경 변수·state는 출력하지 않는다. 기본 실행은 Cost Explorer를 호출하지 않는다. CloudWatch 로그 조회 요금은 계정 조건에 따라 별도 확인한다.

`logs.status`와 종료 코드는 **집계 완전성**을 나타낸다. `ok`여도 오류·실패 유형·알림 결과 필드를 확인한다. `unavailable`, `partial`, `no_data`는 정상 0건으로 간주하지 않으며 종료 코드 1이다. 실행 중이거나 늦게 도착한 로그는 다음 점검과 대조한다. `last_check_at`의 최신성을 확인하고 Scheduler 활성 상태·종료일은 기존 `scripts/check_lambda.py --scheduled`로 확인한다.

`--metrics`를 추가하면 `Invocations`, `Errors`, `Throttles` 합계를 조회한다. 데이터가 없으면 0 대신 `null`이다. `Errors / Invocations`로 함수 오류율을 판단하되 throttled 호출은 두 지표에 포함되지 않는다. 개발 사용자의 `cloudwatch:GetMetricStatistics` 권한 부족으로 실제 지표 확인은 거부됐다. 점검 사용자에게 읽기 권한을 검토하고 Lambda 실행 역할을 넓히지 않는다. [Lambda 지표](https://docs.aws.amazon.com/lambda/latest/dg/monitoring-metrics-types.html)

메모리는 같은 실행 환경을 공유한 호출 전체의 최대치다. 상승만으로 누수를 확정하거나 설정 메모리를 늘리지 않고 OOM·timeout·실행 시간과 함께 추적한다. [REPORT 필드 의미](https://docs.aws.amazon.com/lambda/latest/dg/python-logging.html)

### 최초 기준선 — 2026-10-23

KST 20:35~20:50:48 구간의 모든 페이지를 읽었으며 마지막 수집 로그는 20:49:47이었다. 배포 초기 검사 오류와 이전 30초 재조회 실행은 제외했다.

| 항목 | 결과 |
| --- | --- |
| 완료된 실행 / 확인 | 15 / 15 |
| 조회 오류 / handler 실패 | 0 / 0 |
| 기존 회차 중복 차단 | 45건 (`not_claimed`) |
| 실행 시간 평균 / 최대 | 약 6.744초 / 10.949초 |
| 메모리 최대 / 설정 | 188MB / 256MB |
| 수집한 REPORT의 과금 대상 사용량 | 25.29075 GB-seconds |
| AWS에서 새 회차 실제 전송 | 이 구간에는 없음, 검증 대기 |

짧은 초기 관측이며 하루 또는 한 달의 비용 추정값이 아니다. 10월 24·25·26일에 최근 24시간 보고서를 각각 확인하고 오류·메모리 추이와 비용을 기록한다. 점검 도구 자체가 정기 실행되는 것은 아니다. 로컬 결과는 Git에서 제외된 `build/operations/` 등에 보관한다.

### 신규 알림과 비용 확인

새 회차가 실제 조회에 추가된 다음 자동 실행의 `notification_outcomes.sent`, 같은 실행 ID의 로그, Discord 메시지, 해당 회차 DynamoDB `SENT`를 대조한다. 이후 실행은 `not_claimed`여야 한다. 특정 키만 읽고 일상 점검에서 전체 테이블을 scan하지 않는다. 기존 `SENT` 삭제나 회차 키 변경으로 검증 성공을 만들지 않는다. `uncertain`, `failed`, `state_error`, `deferred`는 전송 여부와 상태를 확인하기 전 재전송하지 않는다.

비용은 AWS Billing / Cost Explorer 콘솔에서 일별 Lambda·DynamoDB·CloudWatch·EventBridge와 크레딧 적용을 확인한다. 데이터가 지연되므로 당일 0원은 무료 운영의 증거가 아니다. 현재 실제 청구 비용은 미확인이다.

`ce:GetCostAndUsage` 읽기 권한을 마련한 뒤 `operations_report.py --cost`를 선택적으로 쓸 수 있다. **Cost Explorer API는 페이지당 USD 0.01**이며 반복 조회하지 않는다. 직전 7개 완료 UTC 일자를 조회하고 `Estimated`를 보존한다. 계정 전체 서비스별 `UnblendedCost`이므로 다른 프로젝트 비용도 포함할 수 있다. [가격](https://aws.amazon.com/aws-cost-management/aws-cost-explorer/pricing/), [데이터 갱신·캐싱](https://docs.aws.amazon.com/cost-management/latest/userguide/ce-api-best-practices.html)

### 릴레이 한도와 단일 대상 선택

2026-10-23 로컬의 정상 시간표 응답에서 `X-RateLimit-Limit: 3000`, `X-RateLimit-Remaining: 2728`을 확인했다. 로컬 잔여량을 AWS 실행 IP의 잔여량으로 해석하지 않는다.

[공개 미들웨어](https://github.com/hmmhmmhm/daiso-mcp/blob/main/src/middleware/dailyRateLimit.ts)는 CGV 등 제한 대상 GET 요청을 같은 IP의 카운터에 합산한다. **지점·상영 날짜별이 아닌 IP별 하루 3,000회**이며 KST 자정에 초기화한다. 초과 시 429·`Retry-After`를 반환한다. 실제 적용 설정은 응답 헤더도 대조한다. [캐시 명세](https://github.com/hmmhmmhm/daiso-mcp/blob/main/docs/service-reference.md)의 시간표 3분·영화 목록 10분 캐시도 감지 지연에 영향을 준다.

변경 전 재시도 0회·4일×6지점은 한 실행에 시간표 최대 24회, 빈 결과 교차 확인까지 최대 48회였다. 매분이면 최대 하루 69,120회다. 빈 날짜에서 이후 날짜 조회를 중단해도 첫 날짜 6지점을 매분 조회하는 것만으로 하루 8,640회다. 요청량은 정상 빈 응답·교차 확인·재시도 정책에 따라 달라진다.

현재는 `[selection].branch_index = 3`·`date_index = 0`으로 수유·10월 28일 하나만 1분마다 조회한다. 후보 목록은 그대로 보관하며 인덱스 두 개를 바꾸고 재배포하면 대상이 바뀐다. 재시도 0회에서 시간표 1회·교차 확인 최대 1회로 최대 하루 2,880회다. 시간표가 없거나 선택한 날짜가 지났어도 다른 후보로 자동 이동하지 않는다. 수동 실행·같은 IP의 다른 이용·재시도 변경은 별도이므로 총 한도 여유를 확인한다. IP 변동이나 다른 경로를 한도 회피 수단으로 사용하지 않는다.

2026-10-23 KST 21:16 자동 실행에서 수유·2026-10-28 한 대상의 조회, 감지 1·오류 0·기존 회차 중복 차단 1을 확인했다. Lambda 갱신은 코드 1건만 적용했고 스케줄·테이블·기존 알림 상태를 유지했다. 배포 후 plan은 변경 없음이었고 Python 테스트 297개·Ruff·Terraform 모의 테스트 3개가 통과했다.

MIT 소스 라이선스와 공개 REST 제공은 장기 반복 조회·CGV 원천 이용 허용·가용성 보장을 뜻하지 않는다. 운영자에게 개인 알림 목적, 영화·지점·날짜 수, 교차 확인 포함 요청 상한, 기간을 설명하고 허용 주기와 별도 조건을 확인해야 한다. 문의를 전송하지는 않았다.

## 2. GitHub Actions CI — 코드 작성·로컬 검증

[CI workflow](../.github/workflows/ci.yml)는 push·PR·수동 실행에서 Python 3.13 / 고정 uv로 lockfile 의존성을 설치하고 pytest, Ruff lint/format을 검사한다. 이어 Lambda ZIP 빌드와 Terraform 1.17.0의 fmt·init·validate·mock test를 실행한다.

AWS 인증·Webhook·운영 state·배포 권한은 사용하지 않는다. init은 `-backend=false -lockfile=readonly`이며 모의 테스트는 리소스를 만들지 않는다. action은 commit SHA로 고정하고 GitHub token은 `contents: read`만 허용한다. [GitHub 보안 지침](https://docs.github.com/en/actions/reference/security/secure-use), [uv 연동](https://docs.astral.sh/uv/guides/integration/github/)

사용자가 커밋을 push한 이후 원격 실행 결과를 확인한다. 로컬 검사와 GitHub 실제 실행 성공은 별개다. branch protection은 아직 변경하지 않았다.

## 3. 배포 자동화 — 다음 단계

1. S3 state 저장소의 public access 차단·암호화·versioning·lock과 비용을 검토한다. 로컬 state를 안전하게 백업한 뒤 기존 state를 마이그레이션한다. 빈 state로 기존 리소스를 재생성하지 않는다.
2. Discord 비밀 관리 방식을 정한다. 현재 state·plan에 Webhook이 저장된다. GitHub secret으로 옮기는 것만으로 state의 비밀이 제거되지 않으며 과거 state 버전의 접근·보관도 고려한다.
3. OIDC provider와 배포 역할을 별도로 마련하고 실제 저장소 `sub` 형식을 확인해 main 또는 보호 environment로 신뢰를 제한한다. PR에 배포 권한을 주지 않는다. bootstrap 권한이 있다고 가정하지 않는다.
4. 검증 성공 후 코드·감시 설정을 패키징해 plan 검토·적용을 연결한다. 동시 배포를 막고 스케줄 활성 상태·종료일·롤백 절차를 보존한다.

원격 state·OIDC·배포 workflow는 아직 도입하지 않았다. [S3 backend](https://developer.hashicorp.com/terraform/language/backend/s3), [GitHub OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws)

## 4. 장애 감지와 복구 — 다음 단계

연속 실패 임계치·호출 누락 판정 시간·운영 알림 채널을 정하고 비용 검토 후 Terraform으로 구성한다. 회차 알림과 운영 장애 알림을 구분한다. 아직 장애 알림은 구성하지 않았다.

복구는 특정 회차 키와 Discord 수신 여부를 확인하고 소유자·현재 상태·lease 조건을 둔 갱신으로 구현한다. `UNCERTAIN` 자동 재전송이나 테이블 초기화를 하지 않는다. dry-run·제한된 운영 권한·실패별 복구 절차 검증이 완료 기준이다.
