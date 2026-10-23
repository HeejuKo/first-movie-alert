# 운영 장애 알림과 회차 알림 복구

현재 코드는 로컬 검증 단계다. 운영 적용 전 아래 권한 반영과 Lambda 배포·실행 확인이 필요하다. 실제 실패 회차를 임의로 만들거나 기존 `SENT`를 삭제해 검증하지 않는다.

## 운영 알림

`config/watch.example.toml`에서 설정한다. 이 블록을 생략하면 운영 알림은 꺼진다. CLI와 저장 응답 재생은 운영 상태를 변경하지 않는다.

```toml
[operations]
enabled = true
failure_threshold = 3
```

실제 알림 모드의 Lambda에서 조회·알림 처리 실패가 3회 연속 관찰되면 `Movie Alert 운영 장애`를 보낸다. 정상적인 빈 시간표는 성공이며 실패 횟수를 초기화한다. 정상 처리로 돌아오면 `Movie Alert 운영 복구`를 보낸다. 이는 미전송 회차가 복구됐다는 뜻이 아니다. `FAILED`·`UNCERTAIN` 기록은 계속 별도 확인한다.

운영 상태는 같은 테이블의 `health:v1:checker`, 전송 상태는 `operations:v1:<incident_id>:failure/recovery`로 구분한다. 운영 상태는 일관된 단건 읽기와 revision 조건부 갱신으로 저장한다. 같은 장애의 전송 키를 재사용하며, 연결 실패·429는 기존 최대 3회 claim 정책을 따른다. 재시도 기한이나 lease 때문에 claim이 막혀도 같은 대기 항목을 보존한다. 결과 불명·영구 거부는 자동 재전송하지 않는다. 정상 복귀나 새 장애가 발생하면 오래된 대기 이벤트를 최신 상태로 대체하며 무제한 큐를 만들지 않는다.

SDK 클라이언트는 실행 환경에서 재사용한다. 운영 상태 처리 전 잔여 시간이 80초 미만이면 추가 I/O를 생략하고 로그를 남긴다. 운영용 SDK는 연결·응답 제한 각각 2초·시도 1회, Discord는 단계별 5초 제한이다. 원래 조회가 실패한 상태에서 운영 상태 기록까지 실패해도 원래 오류를 보존한다.

이 감지는 함수가 실행돼 처리 결과를 기록할 수 있어야 동작한다. 호출 누락·import 실패·강제 종료·OOM·타임아웃·DynamoDB 또는 Discord 전체 장애를 독립적으로 감지하지 못한다. 이 범위에는 별도 CloudWatch 경보가 필요하며 아직 추가하지 않았다. Webhook 전송과 DynamoDB 저장 사이에는 중복 가능성이 남는다.

기존 Lambda·테이블·Webhook을 사용하고 새 AWS 서비스를 만들지 않는다. 정상 실행당 단건 읽기 1회·상태 쓰기 1회가 추가되므로 매분 실행·30일이면 각각 최대 약 43,200회다. 운영 메시지 전송 시 claim·완료·확인 쓰기가 추가된다. 테이블의 1 RCU/1 WCU 설정은 유지하며 사용량·저장량·로그와 실제 청구 조건을 계속 확인한다. 무료 운영을 보장하지 않는다.

## 적용할 IAM 권한

계정 번호를 치환한 로컬 JSON은 Git에서 제외한다. 다음 작업은 기존 개발 사용자에게 경계 정책 수정 권한이 없어 루트 콘솔에서 수행한다.

1. 기존 고객 관리형 `MovieAlertRuntimeBoundary`를 [runtime-boundary.json.example](../infra/iam/runtime-boundary.json.example) 기준으로 갱신한다. `ReadCheckerHealth`는 `GetItem`을 `health:v1:checker`에만 허용하며 기존 권한은 유지한다. Lambda의 실제 역할 정책은 Terraform 배포에서 같은 읽기 권한을 추가한다.
2. [recovery-operator.json.example](../infra/iam/recovery-operator.json.example)을 고객 관리형 `MovieAlertNotificationRecovery`로 생성해 복구 담당자에게 연결한다. `showtime:v1:*`의 `GetItem`·`UpdateItem`만 추가하며 scan·삭제·조회 잠금·운영 상태를 허용하지 않는다. 기존 개발 사용자의 다른 정책과 권한은 합산되므로 이 정책이 기존 권한을 줄이지는 않는다.

권한 초안은 IAM Policy Autopilot 0.3.0으로 생성하고 검토했다. 사용하지 않는 `ReadDataForReplication`·고객 관리 KMS 키 권한을 제외하고 단건 읽기의 LeadingKeys를 제한했다. `ForAllValues` 조건에는 `Null=false`도 적용한다. 생성기가 만든 원본을 그대로 연결하지 않는다.

```bash
DISABLE_IAM_POLICY_AUTOPILOT_TELEMETRY=true uvx iam-policy-autopilot@latest generate-policies \
  "$PWD/src/movie_alert/runtime.py" "$PWD/src/movie_alert/health.py" \
  "$PWD/src/movie_alert/recovery.py" --region ap-northeast-2 \
  --account YOUR_ACCOUNT_ID --service-hints dynamodb --tf-dir "$PWD/infra" --pretty
```

## 확인할 회차 찾기

기존 읽기 전용 보고서를 실행한다. `notifications_needing_review`는 로그에서 관찰한 키·상태·메시지 ID이며 현재 DynamoDB 상태의 확정값은 아니다. `operational_notification_outcomes`, `health_record_errors`, `health_record_skips`도 확인한다.

```bash
AWS_PROFILE=movie-alert uv run --locked --extra console-login \
  python scripts/operations_report.py --hours 24
```

새 코드의 CloudWatch `회차 알림 확인 필요:` 로그와 CLI 미리보기의 `key`에서도 회차 키를 확인할 수 있다. 같은 영화·지점·상영일의 현재 시간표를 대조하며 상태 테이블 전체를 scan하지 않는다.

## 단건 조회와 복구

아래 변수는 실제 출력값으로 채운다. 비밀값을 넣지 않는다. 복구 도구는 Webhook을 읽거나 메시지를 전송하지 않는다.

```bash
NOTIFICATION_KEY='showtime:v1:실제_64자리_해시'
AWS_PROFILE=movie-alert uv run --locked --extra console-login \
  python scripts/recover_notification.py inspect --key "$NOTIFICATION_KEY"
```

`inspect`의 `revision`을 기록하고 상태·owner·attempt_count·lease_until·next_retry_at을 확인한다. 시각은 epoch seconds다. `SENT`, 아직 유효한 lease, 없는 항목, `showtime:v1:` 외 키는 복구하지 않는다. `PENDING`은 재시도 기한과 시도 횟수를 확인해 정상적인 자동 재시도를 기다릴 수 있는지 먼저 판단한다.

Discord에서 메시지를 실제로 찾았다면 해당 메시지 ID를 사용해 완료 처리의 미리보기를 만든다.

```bash
AWS_PROFILE=movie-alert uv run --locked --extra console-login \
  python scripts/recover_notification.py mark-sent --key "$NOTIFICATION_KEY" \
  --operator HeejuKo --message-id 실제_Discord_메시지_ID
```

미수신을 확인하고 Webhook·네트워크·권한 오류를 해결했다면 재시도 허용의 미리보기를 만든다.

```bash
AWS_PROFILE=movie-alert uv run --locked --extra console-login \
  python scripts/recover_notification.py retry --key "$NOTIFICATION_KEY" \
  --operator HeejuKo --confirm-not-delivered
```

`UNCERTAIN`은 실제 전송됐을 수 있다. 수신 여부를 확정할 수 없다면 보류 상태를 유지한다. 중복 위험을 이해하고 수동 재시도를 선택할 때만 `--accept-duplicate-risk`를 추가한다.

검토한 명령에 `--apply --expected-revision 실제_revision`을 추가해야 실제 변경된다. 도구는 다시 읽은 revision과 관찰한 모든 상태 필드·lease를 조건부로 확인한다. 다른 실행이 먼저 변경했다면 중단하며 무조건 덮어쓰지 않는다. 복구 작업자·시각·동작·고유 ID를 기록하고 기존 보관 기간을 줄이지 않는다.

`retry`는 시도 예산을 수동으로 초기화하는 작업이다. 즉시 전송하지 않으며 이후 정상 감시에서 같은 회차가 다시 발견되고 조건에 맞아야 재시도한다. 현재 선택에서 제외됐거나 시작한 회차는 자동 전송되지 않을 수 있다. 조건부 갱신 실패·응답 유실 때는 다시 inspect해 감사 표식과 Discord 수신 여부를 확인하며 같은 변경을 맹목적으로 반복하지 않는다.
