# 격리된 subscription 배포

이 대상 배포는 릴리스된 FP8/NVFP4 chat, 비교,
MAF, 업로드, 문서/표/파일 tool과 샘플 8개를 모두 보존한다. Hosted
Teams identity, app role, 시험 설정, 원본 subscription 리소스,
자격 증명은 복사하지 않는다. 릴리스 `f146262`가 데모 기준선이다. 이후 성공한 Hosted
후보 `567f380`은 선택 사항인 Hosted route와 JWT 의존성만 변경했다.

## 범위와 고정 비용

Terraform은 전용 resource group 하나, 용량 1의 Linux B1 웹 plan,
Python 3.12 web app, UK South AML workspace 하나와 그 Storage/KV/Insights/Log
Analytics 의존 리소스, 그리고 LowPriority `Standard_ND96amsr_A100_v4` 클러스터 하나를 만든다.
클러스터는 **min=0, max=3**이며, FP8 TP8/PP2(노드 두 대)와 NVFP4 TP8/PP1
(노드 한 대)이 함께 쓴다. UK South만 구성되어 있으며, 자동 리전 경쟁은 없다.
서로 독립된 두 profile store는 모두 기본값이 **OFF**이다.

웹 identity는 **대상 workspace에만** AzureML Data Scientist 역할을 받는다.
compute identity는 **자기 storage account에만** Storage Blob Data Contributor 역할을 받는다.
AML은 identity 기반 시스템 datastore 접근을 사용한다. Storage와 Key
Vault의 공용 네트워크 접근은 비활성화되어 있으며, 이는 대상 환경에서 강제하는
정책과 일치한다. Shared-key 인증과 익명 blob 접근도 비활성화되어 있다.
새 private endpoint, NAT, Bastion, ACR은 배포하지 않는다. 이 데모는 가중치를
공용 Hugging Face에서 내려받고, 코드는 인증된 reverse-link bootstrap으로 받는다.
이 storage account에서 받지 않는다. 운영자 노트북에서 AML 출력
blob으로의 private data-plane 접근은 이 배포가 구성하지 않는다. 필요해지면
승인된 private 접근 경로를 따로 구성한다.
기존에 고정한 공용 vLLM 이미지를 Docker Hub에서 재사용한다. provisioning 중에는 이미지 빌드나
가중치 전송을 실행하지 않는다. 공용 이미지 가용성/rate limit과
Spot 재고는 여전히 실제 시작 테스트로 확인해야 한다.

공용 Azure Retail API는 2026-10-19에 Korea Central의 Linux B1을 USD 0.018/hour로
반환했다(730시간 기준 약 USD 13.14, 세금/계약 적용 전). Storage,
Key Vault 작업, telemetry 비용은 별도이다. telemetry에는 0.1 GB/day 상한이 있다
(요금이 0이라는 보장은 아니다). GPU min=0만으로는 비용이 0이라는 충분한 증거가 아니다.
`status`는 ARM current/target, 실제 노드, 활성 job을 검사한다. GPU OFF는
웹이나 storage 요금을 멈추지 않는다. AML Spot 할당량은 일반 VM 할당량과 별개이며,
할당량이 용량을 보장하지는 않는다.
UK South ND96amsr A100 Linux의 경우, 같은 Retail API는 2026-10-19에 Low Priority를
USD 8.192/node-hour로, 별도 Spot meter를 USD 12.886645/node-hour로 나열했다.
따라서 세 노드 구성은 할당된 동안 시간당 대략
USD 24.58-38.66에 해당한다. 실제로 적용되는 청구
meter/계약에 따라 달라질 수 있으며, 보장된 청구 요율이나 엄격한 예산 상한이 아니다.

## 초기화, provisioning, 배포

WSL/Linux와 **이미 인증된 대상 subscription**을 사용한다. 이 명령들은
`az account set`을 호출하거나, 계정을 전환하거나, `az login`을 실행하지 않는다. 모든 Azure CLI 호출은
대상 subscription을 지정한다. Terraform과 ARM URL도 명시적이다.
필요하면 시작 전에 기존의 대상 전용 `AZURE_CONFIG_DIR`을 설정한다.
다른 tenant의 토큰이나 자격 증명을 복사하지 않는다.

```bash
export PATH="$HOME/.local/bin:$PATH"
D="$HOME/.axk2-targets/<unique-alphanumeric-app-name>"
python3 demo/target.py --directory "$D" init \
  --subscription <destination-subscription-uuid> --name <unique-alphanumeric-app-name>
python3 demo/target.py --directory "$D" provision
python3 demo/target.py --directory "$D" deploy
python3 demo/target.py --directory "$D" status
# Read-only refresh/plan, including on an existing running target (never applies):
python3 demo/target.py --directory "$D" plan
```

`init`은 지정한 캐시 계정만 사용하여 명시적인 tenant/subscription 바인딩을 저장한다.
기존 바인딩은 덮어쓰지 않는다. state 디렉터리는
checkout 밖, Linux 파일 시스템 위에 있어야 하며 mode는 0700이어야 한다. 생성되는 파일은 모두
0600을 사용한다. Terraform state, input, plan, 진단 로그, 배포 ZIP, 생성된
관리자 자격 증명은 이 디렉터리에 남으며 git이나 채팅에 넣지 않는다. Terraform state에는
refresh된 민감한 app setting이 들어갈 수 있다. **디렉터리 전체를 자격 증명으로 취급한다**.
이 디렉터리를 artifact로 업로드하거나 `terraform show/state` 출력을 표시하지 않는다.
이 대상에는 legacy `demo/ctl.sh` 또는 `demo/deploy.sh` 기본값을 사용하지 않는다.
그 과거 리소스/암호 위치는 원래 데모를 가리킨다.
대상 runner는 `~/.axk2-demo/passwords`를 읽거나 덮어쓰지 않는다.

runner는 모든 대상 작업에 nonblocking `flock`을 건다. Terraform의 local
backend는 전용 state를 따로 잠근다. backend와 state 모두 원본 배포와
공유하지 않는다. 이것은 **단일 머신** backend이며, 분산 잠금이 아니다.
복사한 state 두 개를 동시에 실행하지 않는다. 팀 단위로 운영하려면, 별도로 승인된
Azure AD 및 blob-lease 잠금 기반 Azure Blob backend로 한 번 이전한다.
기존 subscription의 state를 재사용하지 않는다.

`provision`은 실제 plan을 검증하고 저장한다. 삭제/교체와, 대상 밖의 리소스
ID 또는 resource group 변경은 모두 거부한다. 그런 다음 저장한 그 plan을
그대로 적용한다. `safe-plan.json`은 리소스 작업과 원본 변경 0건을 기록한다.
FTP와 WebDeploy 기본 인증은 명시적으로 비활성화되어 있다. 배포
runner는 ARM 토큰을 사용한다. 필요한 대상 resource provider만 등록한다. secret은
Terraform input 밖에서 `deploy`가 만든다. 원본 Web IQ 키나 Hosted identity는 읽지 않는다.

`deploy`는 한 번만 업로드하고, 실제 OneDeploy 완료 여부를 확인한다. 그리고 저장된
`output.tar.zst` 안의 모든 소스 파일을 패키지된 SHA map과 직접 비교한다.
timeout이 다시 업로드해도 된다는 허가는 아니다. 먼저 기존 배포를
확인한다. 이후 재배포는 실행 중인 reverse link를 방해하지 않도록 GPU OFF 상태여야
한다. 배포가 성공해도 모델 준비 완료를 증명하지는 않는다.

## 실수로 GPU를 계속 쓰지 않도록 시작/중지하기

```bash
# Read-only: both profiles, actual current/target/nodes and nonterminal AML jobs.
python3 demo/target.py --directory "$D" status

# Stops both profiles durably; waits for TWO zero-node observations.
python3 demo/target.py --directory "$D" off

# ONLY after explicit approval for GPU/download costs. Keep this process attached.
# This example grants a 60-minute lease, INCLUDING startup/download time.
python3 demo/target.py --directory "$D" run --seconds 3600
```

명시적으로 승인된 대화형 데모가 검증 후에도 계속 사용 가능해야 하면
`python3 demo/target.py --directory "$D" on --keep-on`을 사용한다.
이 명령에는 의도적으로 **자동 종료가 없다**. 사용자가 작업을 마치면 운영자가 `off`를
실행해야 한다. 명시적 opt-in 없이 기한 없는 시작을 요청하면 이 명령은 거부한다.
시작 시각은 private 대상 디렉터리에 기록한다.
provisioning 기본값으로 사용하지 않는다.

`run`은 60-7200초를 허용한다.
기존 profile 두 개를 모두 시작하고, `finally`에서 지속적인 OFF를 호출한다. 일반적인
interrupt/SIGTERM에서도 마찬가지이다. 이 명령은 compute 용량을 바꾸거나, 다른
리전을 만들거나, 벤치마크를 시작하지 않는다. 컨트롤러 머신 장애/SIGKILL 시에는 로컬
lease가 cleanup을 실행할 수 없다. 보존된 대상 디렉터리에서 `off` 명령을
호출한다. 이 로컬 lease를 클라우드에서 강제되는 예산 보장으로 사용하지 않는다.
시작 과정이 준비 완료에 이르지 못한 채 lease 전체를 소비할 수 있다. 이것은 실제
실패/준비 안 됨 결과이며, 모델 테스트 통과가 아니다.

릴리스된 downloader는 각 노드에 전체 checkpoint를 준비한다. 2026-10-19 기준 고정
revision의 공개 metadata에 따르면 FP8 노드당 약 694.386 GB,
NVFP4는 398.278 GB이며, **세 노드 cold start에 약 1.787 TB**가 필요하다. 여기에는
이미지/패키지/재시도가 포함되지 않는다. 기존 subscription의 가중치는 복사하지 않는다. 다운로드와
모델 로딩은 OFF 전용 웹 rollout으로는 검증되지 않는다.

`off`는 먼저 인증된 Store/Supervisor 전원 route 두 개를 모두 사용하여
재제출을 막는다. 그 다음 바인딩된/활성 job이 없고, 종료되지 않은 compute job이 없고,
ARM current=target=0이며, 실제 노드 목록이 비어 있을 때까지 기다린다. 공개 OFF 배지만으로
취소나 비용 0을 주장하지 않는다. frontend/auth를 사용할 수 없으면, 운영하기 전에
저장된 배포/자격 증명을 복원한다. 원하는 상태가 ON인 supervisor가 살아 있어 job을 다시 제출할 수 있는
동안에는 AML job만 취소하지 않는다.

## 모델 전용 API 준비와 검토된 웹 배포

모델 전용 API 자격 증명은 관리자/데모 자격 증명 및 Web IQ와 분리되어 있다.
`AXK2_MODEL_API_KEY_SHA256`이 설정되지 않으면 API는 비활성 상태로 남는다. Terraform은
이 외부 관리 설정의 값을 Terraform 변수로 받지 않고 기존 값을 보존한다.
그래도 refresh된 Terraform state에는 이 값이 들어갈 수 있다. 위에서 설명한 대로
state 디렉터리 전체를 보호한다.

```bash
# Local preparation only: generates a 256-bit key once; never changes Azure.
python3 demo/target.py --directory "$D" model-key
# Only after the deployment owner receives approval for this exact commit:
python3 demo/target.py --directory "$D" deploy-revision \
  --commit <full-40-character-reviewed-commit> --allow-live-web-recycle
# After disabled-code regression passes, activate in a separate explicit step:
python3 demo/target.py --directory "$D" model-settings --allow-live-web-recycle
```

원본 키는 `$D/model-api-backend.key`(0600)에만 존재한다. digest는
`$D/model-api-settings.json`(0600)에 준비되며, 표시하거나 명령줄로 전달하지 않는다.
두 값 모두 문서, shell history, evidence, git에 넣지 않는다.
기존 키가 있으면 재사용한다. `model-key --rotate`는 새 키를 준비하고, 겹치는 기간을 위해
이전 digest를 유지한다. 승인된 private 소비자 설정을 통해 새 키를 배포한 다음에
`model-key --retire-previous`로 이전 digest 제거를 준비한다. 첫 번째 rotation을
retire하기 전까지 두 번째 rotation은 거부된다. 이 명령들은 파일만
준비한다. 최초 코드 배포 이후에는, 준비된
rotation/retirement를 `model-settings --allow-live-web-recycle`로 활성화한다. 이 작업은
명시적으로 승인된 시간대에만 한다. 이 설정 전용 작업에는 저장된
모델 API 배포 성공 증명이 필요하며, 원래의 reverse link 두 개를 모두 다시 확인한다.

선택 설정은 G의 모델 API 계약을 따르며, Terraform도 이 값을 보존한다.
기본값은 준비할 필요가 없다. Azure를 건드리지 않고 override를 준비하려면
`model-key`에 `--model-api-setting NAME=VALUE`를 반복해서 지정한다. 이미 준비된 override는
멱등 실행과 키 rotation 후에도 남는다. 다음 이름만 허용한다.
잘못된 옵션은 런타임 대체값에 의존하지 않고 명시적으로 실패한다.

| `AXK2_MODEL_API_` 뒤의 접미사 | 기본값 |
| --- | --- |
| `NAME_FP8`, `NAME_NVFP4` | `ax-k2-fp8`, `ax-k2-nvfp4` |
| `ALIASES_FP8`, `ALIASES_NVFP4` | `skt/A.X-K2`, `skt/A.X-K2-NVFP4` |
| `MAX_ACTIVE`, `MAX_QUEUE`, `RATE_PER_MINUTE` | `4`, `8`, `60` |
| `QUEUE_TIMEOUT`, `TIMEOUT`(초) | `30`, `300` |
| `MAX_TOKENS`, `DEFAULT_MAX_TOKENS` | `8192`, `4096` |
| `MAX_BODY_BYTES`, `MAX_MESSAGES` | `2097152`, `400` |
| `MAX_TOOLS`, `MAX_SCHEMA_BYTES` | `64`, `131072` |

`deploy-revision`은 현재 작업 트리가 아니라 정확한 git commit을 패키징한다.
짧은 SHA, tag, branch, 안전하지 않은 archive 경로, symlink는 거부한다. 후보를
fetch하고 검토한 뒤에 그 SHA를 사용한다. 이 명령은 저장된 rollback ZIP을
manifest 및 실제 배포된 archive와 대조하여 검증한다. 그 다음 해당 ZIP,
manifest, app setting, 현재 profile 바인딩을 새 private rollback
디렉터리에 보관한다. 기존 설정은 보존되며, 준비된 API 설정만 바뀐다.
GPU 전원을 호출하거나, compute를 바꾸거나, job을 제출하거나, Terraform을 적용하지 않는다.
클라우드에 쓰기 전에 두 profile의 익명 `/v1/models` 응답을 점검한다.
404는 비활성, 401은 활성을 뜻한다. 두 응답이 일치해야 하며, 알 수 없거나 섞인 상태면
중단한다. 게시 직전에 다시 확인하고, 이후 복구와 최종 회귀 테스트 내내
같은 활성화 상태를 요구한다. 따라서 이미 활성화된 API의 코드만 갱신하면
401이 유지되며 digest를 다시 쓰지 않는다.
활성화된 코드를 재배포한다는 이유만으로 `model-settings`를 다시 실행하지 않는다.

일반적인 OFF 전용 `deploy`와 달리, 이 작업은 실행 중인 웹의 recycle을 명시적으로
허용한다. App Service ZIP 배포와 app setting 갱신은 **별개의
작업**이다. 승인된 절차는 조정된 시간대 하나에서 **웹 recycle 두 번, GPU 재시작
0번**을 허용한다. 최초 rollout에서는 digest가 설정되지 않은 상태로 API 코드가
배포되므로, 새 route는 잠시 비활성(404) 상태가 된다. 활성화 전에
health, status, capabilities, 비활성 route를 확인한다. 그리고 기존
모델 reverse link 두 개가 profile 활성화 시각 변화 없이 빈 queue 상태로
복구되는지 확인해야 한다. 그 뒤에만 `model-settings`를 실행하여 자격 증명을 활성화하고, 두 link와
빈 queue를 다시 기다린 다음 smoke를 실행한다. `deploy-revision`은 키를 활성화하지 않는다.
이 health check는 추론 테스트가 아니다.
각 recycle 후 runner는 최소 4분의 안정화 기간을 강제한다. 그리고 최소
15초 간격으로 연속 세 번의 완전한 검사를 요구하며, 전체는 10분으로
제한한다. 각 검사에는 변경되지 않은 정상 profile 바인딩 두 개, 빈
queue, health/status, Web IQ discovery, 예상되는 비인증 모델
API 상태(활성화 전 404, 활성화 후 401)가 포함된다. 설정 recycle 전에 이전 worker가
응답하는 것은 새 worker가 복구되었다는 증거가 아니다.

배포 후에는 **검토된 후보의**
`demo/tools/model_api_smoke.py`를 보호된 key-file 인자로 실행한다. 키 값을 argv에 넣지 않는다.
검토된 그 파일은 배포한 바로 그 commit에서 private 대상 디렉터리로
export한다. 다른 branch의 현재 사본을 실행하지 않는다.
`$URL`을 대상의 공개 URL로 설정했을 때, 승인된 제한적 NVFP4 순서는 다음과 같다.

```bash
python3 "$D/model_api_smoke.py" \
  --base-url "$URL/models/nvfp4/v1" --model ax-k2-nvfp4 \
  --key-file "$D/model-api-backend.key" \
  --only models,chat,stream,tool,tool_result,auth \
  --max-tokens 64 --timeout 120 --out "$D/model-api-smoke.json"
```

이 순서는 최대 네 번의 모델 admission(chat, stream, tool call, tool-result
replay)을 사용한다. 동시성은 1이고, thinking은 OFF이며, admission당 64 토큰 상한이 있다.
모델 목록 조회와 키 누락/오류 거부는 모델 admission을 소비하지 않는다.
이 도구는 secret이 없는 검사별 결과, status code, 토큰 사용량,
TTFT/TPOT, 경과 시간을 출력한다. 성공했다고 가정하지 말고 실제 결과를 확인한다.
두 번째 smoke 시도를 자동으로 실행하지 않는다.
`candidate-proof.json`은 이 별도 검증이 끝날 때까지 smoke를 미실행으로
명시적으로 기록한다.

업로드 전송이나 OneDeploy 상태가 불확실하면, 다시 제출하지 말고 해당 배포를
확인한다. 복구에 실패하면 후보/rollback 증거를 보존하고
차단 요인을 보고한다. private rollback 디렉터리에는 별도로 승인된 복구에 쓸
이전 ZIP과 설정이 들어 있다. frontend rollback 방법으로 GPU를 종료하거나 재시작하지
않는다.

## Web IQ와 선택 서비스

**승인된 대상 전용** 자격 증명을 구성하기 전까지 Web IQ는 명시적으로 사용할 수 없다.
사용할 수 없는 이유는 `/api/capabilities`와 기존
tool/action UI에 표시된다. 관련 없는 검색/모델 제공자로 대체하지 않는다.

```bash
# Inject these through your secure shell/secret manager; do not put real values
# in command history, tfvars, source code, Terraform output or a chat.
export AXK2_WEBIQ_ENDPOINT
export AXK2_WEBIQ_API_KEY
python3 demo/target.py --directory "$D" webiq
unset AXK2_WEBIQ_API_KEY
```

이 명령은 두 값을 모두 요구하며, 임시 0600 파일만 쓰고 이를
**이 대상 앱**으로 보낸다. 다른 tenant의 키를 추출하거나 전송하지 않는다.
`/api/capabilities`를 다시 확인하여 discovery를 수행한다(검색/추론 없음). 허용된
엔드포인트와 자격 증명이 있다고 해서 검색 성공이나 적절한 쿼리 사용이 증명되지는 않는다.
이는 별도로 승인된 테스트가 필요하다. OCR과 외부 코드 sandbox는 원본 데모와 마찬가지로
`not_configured` 상태로 남는다. 업로드된 코드나 모델이 만든 코드를 대신
웹 호스트나 GPU에서 실행하는 일은 절대 없다.

## 복구와 검증

apply와 배포 후에는 private 대상 디렉터리를 암호화하고 접근을 통제한 백업으로
보관한다. 머신을 잃으면, 운영하기 전에 같은 state/config와
자격 증명을 복구한다. 기존 리소스 위에 새 state로 `init`하거나, 활성 소유자의 잠금을
강제로 해제하거나, 원본 리소스를 import하거나, 오래된 잠금을 우회하려고
리소스를 삭제하지 않는다.

OneDeploy가 실패하면 대상 ARM 인증으로 `https://<app>.scm.azurewebsites.net/api/deployments`를
확인한다. 재시도 전에 종료 상태가 될 때까지 기다린다.
마지막 정상 ZIP과 예상 map을 보존한다. 소스 해시뿐 아니라 Oryx manifest와 압축된 `antenv`
의존성도 확인한다. 소스가 온전해도 uvicorn이 없으면 부팅할 수 없다.
frontend 패키지를 고치려고 정상 GPU job을 재시작하지 않는다.

```bash
terraform -chdir=demo/infra fmt -check
terraform -chdir=demo/infra init -backend=false
terraform -chdir=demo/infra validate
# In a separate WSL Python 3.12 environment with frontend requirements + aiohttp:
python -m unittest discover -s tests -p 'test_demo*.py'
```

rollout 후 검사 항목: root/static UI, 샘플 8개, selector/비교 동작,
`/healthz`, profile 두 개가 모두 OFF인 상태의 `/api/models`, 사실대로 표시된
Web IQ/sandbox/OCR 상태를 담은 `/api/capabilities`, 첨부 파일 생성/업로드/읽기/닫기, 저장된 archive
해시, `status` effective_zero. GPU 세대, tool 기반 모델
동작, 비교 성능은 실제로 실행하지 않았으면 **테스트하지 않음**으로 보고한다.
