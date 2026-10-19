# Foundry BYOM 에이전트 스크립트

Foundry에서 만든 프롬프트 에이전트가 BYOM(Bring your own model) 연결로 A.X K2를 호출하고 도구를 쓰는지 확인하는 스크립트다.
연결 구조는 `Foundry 에이전트 → APIM → 모델 API → GPU 서버`다.
이 작업은 외부 모델 연결(BYOM) 검증이다. Model Catalog 정식 등록이 아니다.

설계, 확인된 사실, 미확인 항목은 [DESIGN.md](DESIGN.md)에 있다. 이 문서는 사용법과 현재 상태만 적는다.
인프라(APIM, Foundry 리소스, 연결)와 모델 API는 이 디렉터리의 범위 밖이다.

## 현재 상태

모델 API의 코드 시점은 두 가지다. e70debb는 수정 전이고 9cd2220은 재배포 뒤다. 호출마다 어느 시점의 결과인지는 DESIGN.md 3.4절에 있다.

| 단계 | 상태 |
|---|---|
| Phase 0: 스크립트, 시험, 설계 | 완료 |
| Phase 0.5: 연결 조회, 도구 없는 에이전트 버전 생성 | 완료. 연결 2개가 계약과 같고, 저장된 모델 참조가 요청과 같다. |
| Phase 1a: 도구 없는 호출 | NVFP4 첫 시도는 모델 쪽 503으로 실패했다(GPU 링크 장애). 링크가 복구된 뒤 FP8와 NVFP4 모두 성공했다. |
| Phase 1b: 함수 도구 | FP8와 NVFP4 모두 성공했다. 모델이 `function_call`을 내고 로컬 함수 결과를 받아 최종 답을 냈다. |
| Phase 1c: Learn MCP | FP8에서 Foundry가 도구를 실행했다(`mcp_call`). `never`는 강화한 지시문으로 성공했고, `always`는 승인 흐름이 성공했다. ablation 에이전트(`temperature` 미지정)는 첫 호출이 500이었고 재시험 3회는 성공했다. 500은 재현되지 않았고 원인은 모른다(U18). |
| Phase 1c: Web IQ MCP | 요청 단위 헤더는 400으로 거부됐다(`agent_reference`와 요청의 `tools`는 함께 쓸 수 없다). 키를 프로젝트 연결에 저장하는 방식은 사용자 승인을 기다린다. 실키는 쓰지 않았다. |
| 콘솔 확인 | 미실행. 사용자 로그인이 필요하다. |

Foundry 에이전트가 BYOM 연결로 모델 API에 요청하고 답을 받는 것, 함수 도구 왕복, Foundry 쪽 MCP 도구 실행은 확인했다.
사용 모델이 A.X K2라는 것은 아직 확정하지 않았다. 에이전트 정의와 응답의 `model` 필드는 모델 참조일 뿐이다. APIM 요청 본문의 모델 이름과 모델 런타임 출처가 같은 시간대로 맞아야 한다(DESIGN.md 7절). 재시험 3회에서 모델 API 담당이 보고한 vLLM 출력 토큰 증가분은 Foundry의 출력 usage와 같았다. 이것은 시간대 대조의 일부이며 요청 단위 대조는 아니다. 입력 토큰 증가분은 호출마다 Foundry usage보다 486개 많았고 원인은 모른다.
확인된 사실과 미확인 항목의 최신 목록은 DESIGN.md 3.4절에 있다.

## 구성

| 파일 | 역할 | 클라우드 호출 |
|---|---|---|
| `inspect_connections.py` | 프로젝트 연결을 읽기 전용으로 조회하고 연결 계약과 비교한다. 자격 증명 값은 요청하지 않는다. | 읽기 |
| `create_agent.py` | 프롬프트 에이전트 버전을 멱등하게 만들고 저장된 모델 참조를 읽어 비교한다. | 에이전트 버전 쓰기와 읽기 |
| `run_prompt.py` | 에이전트를 Responses API로 한 번 호출하고 출력 항목, 도구 호출, 최종 답, usage를 요약한다. | 모델 호출 |
| `collect_evidence.py` | 비밀을 제거한 증거 JSON을 만들고 점검 항목을 판정한다. | 읽기(`--offline`이면 없음) |
| `byom_common.py` | 설정 검증, 비밀 제거, `az` 토큰 요청의 구독 지정, 클라이언트 생성, 증거 파일 쓰기 | 없음 |
| `requirements.txt` | 필요한 패키지 | 없음 |

`run_prompt.py`만 모델을 호출한다. `collect_evidence.py`는 에이전트 정의, 응답, 대화 항목을 읽기만 한다.

## 준비

1. Python 3.10 이상을 쓴다. 가상환경은 저장소 밖에 만들고 커밋하지 않는다.
2. 패키지를 설치한다: `pip install -r demo/foundry-byom/agent/requirements.txt`
3. 인증은 `az` 로그인 세션을 쓴다(`--auth cli`). 스크립트는 `az login`과 `az account set`을 실행하지 않는다. 같은 환경을 쓰는 모든 프로세스가 로그인 상태를 공유하기 때문이다.
4. 호출하기 전에 로그인 계정이 배포 계정과 같은지 확인한다(DESIGN.md 8.1절).
5. 실행 신원에게 프로젝트 범위의 역할(예: Azure AI User)이 있어야 한다. 부여 직후에는 전파에 몇 분이 걸릴 수 있다.
6. 환경변수는 실행할 때만 지정한다. 값은 커밋하지 않는다.

```bash
export FOUNDRY_PROJECT_ENDPOINT="https://<resource>.services.ai.azure.com/api/projects/<project>"
export FOUNDRY_BYOM_AZ_SUBSCRIPTION="<구독 이름 또는 ID>"
export BYOM_REDACT_LITERALS="<배포 접두어>,<실제 연결 이름>=<conn-nvfp4>"
export BYOM_PRIVATE_DIR="$HOME/.byom-evidence-raw"
```

`FOUNDRY_BYOM_AZ_SUBSCRIPTION`은 `az` 토큰 명령에 `--subscription`을 붙인다. 로그인 계정이 바뀌어도 다른 구독의 토큰을 조용히 쓰지 않고 실패한다.
`BYOM_REDACT_LITERALS`는 쉼표로 구분한 목록이다. `실제값=별칭` 항목은 실제값을 별칭으로 바꾸고, 값만 적은 항목은 `<redacted>`로 바꾼다. 대소문자는 구분하지 않는다.
전체 환경변수 목록은 DESIGN.md 5.2절에 있다.

## 실행 순서

모든 스크립트는 `--dry-run`으로 계획만 출력할 수 있다. `--dry-run`은 SDK, 자격 증명, 네트워크를 쓰지 않는다.
`<conn-nvfp4>`와 `<conn-fp8>`는 연결 이름의 별칭이다. `<label>`은 증거 파일 이름이다.

```bash
cd demo/foundry-byom/agent

# 1. 연결 조회(읽기 전용)
python inspect_connections.py --expect <conn-nvfp4>=ax-k2-nvfp4 --expect <conn-fp8>=ax-k2-fp8 --evidence-label <label>

# 2. 도구 없는 에이전트 버전 만들기(모델 호출 없음)
python create_agent.py --connection <conn-nvfp4> --model ax-k2-nvfp4 --evidence-label <label>

# 3. 도구 없는 호출(모델 호출)
python run_prompt.py --scenario plain --max-output-tokens 256 --max-tool-calls 0 --evidence-label <label>

# 4. 함수 도구: 모델의 도구 호출, 로컬 실행, 도구 결과 뒤의 최종 답
python create_agent.py --connection <conn-nvfp4> --agent-name <fn-agent> --tool function --evidence-label <label>
python run_prompt.py --agent-name <fn-agent> --scenario function --max-rounds 3 --max-output-tokens 256 --max-tool-calls 0 --evidence-label <label>

# 5. 공개 MCP(Microsoft Learn): Foundry가 도구를 실행한다(FP8 연결, 지시문 파일 사용)
python create_agent.py --connection <conn-fp8> --model ax-k2-fp8 --agent-name <mcp-agent> --tool mcp --mcp-preset learn --require-approval never --temperature 0.3 --instructions-file <지시문 파일> --evidence-label <label>
python run_prompt.py --agent-name <mcp-agent> --scenario mcp --max-output-tokens 384 --evidence-label <label>

# 5-1. 승인 흐름: always 버전을 새로 만들고 도구 이름을 지정해 승인한다
python create_agent.py --connection <conn-fp8> --model ax-k2-fp8 --agent-name <mcp-agent> --tool mcp --mcp-preset learn --require-approval always --temperature 0.3 --instructions-file <지시문 파일> --evidence-label <label>
python run_prompt.py --agent-name <mcp-agent> --scenario mcp --approve-tool microsoft_docs_search --max-output-tokens 384 --evidence-label <label>

# 6. 증거 수집과 수동 점검 기록(읽기 전용)
python collect_evidence.py --label <label> --connection <conn-fp8> --model ax-k2-fp8 --agent-name <mcp-agent>
```

4단계의 함수는 `run_prompt.py`가 로컬에서 실행한다. Foundry가 도구를 실행하는 경로는 5단계의 MCP다.
지시문 파일에는 질문에 답하기 전에 `microsoft_docs_search`를 먼저 호출하고 그 결과에만 근거해 답하라고 쓴다. 기본 지시문과 `temperature` 미지정 조건에서는 모델이 도구를 부르지 않은 적이 있다(DESIGN.md U19).
`--temperature 0.3`은 예방 조치다. 이 설정이 Foundry의 500을 막는다는 증거는 없다(DESIGN.md U18).
`--prompt-file`로 프롬프트를 파일에서 읽을 수 있다. `run_prompt.py`는 실행마다 대화를 하나 만들고 콘솔에서 볼 수 있도록 남긴다. 지우려면 `--delete-conversation`을 쓴다.
Web IQ MCP는 요청 단위 헤더(옵션 B)가 400으로 거부되어 쓸 수 없다. 키를 프로젝트 연결에 저장하는 옵션 D만 남았고, 키가 Foundry에 저장되므로 사용자 승인 뒤에만 한다(DESIGN.md 4.5절, 4.6절). `run_prompt.py --mcp-request-header`는 옵션 B를 시험하려고 만든 실험 옵션이며 Foundry가 거부했다.

## 실행 결과 읽는 법

`run_prompt.py`는 비밀을 제거한 JSON 요약을 출력한다.

| 키 | 뜻 |
|---|---|
| `stopped`, `ok` | 끝난 이유와 성공 판정. `stopped`가 `completed`면 성공이다. |
| `request` | 호출 조건. 시나리오, 에이전트 이름, 확인 코드(`nonce`), 상한(`limits`: `max_output_tokens`, `max_tool_calls`, `max_rounds`, `timeout_s`), 승인한 도구 |
| `started_at`, `last_request_at`, `finished_at` | 시작, 마지막 요청, 종료 시각(UTC, 초 단위. KST는 UTC+9). 인프라 담당이 APIM 기록과 시간대를 맞출 때 쓴다. |
| `rounds` | 응답마다 한 항목. 주요 키는 `response_id`, `status`, `model`, `output_item_types`, `usage`, `error`, `chained_by`, `requested_at`, `elapsed_s`다. |
| `item_sequence` | 출력 항목 종류의 순서. 도구 호출 흔적을 본다. |
| `tool_events` | 도구 목록, 도구 호출, 승인 요청 항목의 요약 |
| `function_results` | 로컬에서 실행한 함수의 결과 |
| `final_answer`, `final_answer_contains_nonce` | 최종 답과 확인 코드(`--nonce`) 포함 여부 |
| `client_http` | 클라이언트가 실제로 보낸 HTTP 요청 수. `responses_posts`가 `responses.create` 호출 수보다 크면 SDK가 다시 보낸 것이다. SDK 자동 재시도는 꺼져 있다(`max_retries=0`). |
| `error` | 실패한 호출의 상태 코드, Foundry 오류 코드, 메시지 |
| `conversation_id`, `conversation_deleted` | 이 호출이 만든 대화의 ID(증거 파일에는 앞 8자). `--delete-conversation`을 쓴 호출만 삭제 결과가 들어가고, 아니면 `null`이다. |
| `request_header_echoed` | `--mcp-request-header`를 쓴 호출에서 헤더 값이 Foundry 응답에 다시 나타났는지 검사한 결과. 쓰지 않았으면 `null`이다. 값이 나타나면 Foundry에 저장된 것으로 보고 그 값을 교체한다. |

`rounds[].model`은 에이전트의 모델 참조(`<연결 별칭>/<모델 이름>`)다. 이 값만으로는 A.X K2가 답했다고 말할 수 없다(DESIGN.md 7절).

종료 코드는 0이 성공, 1이 실행 또는 API 오류, 2가 설정 오류, 3이 미완이다. 3은 승인이 필요한 도구 호출을 `--approve-tool`로 승인하지 않았을 때, `max_rounds`를 다 썼을 때, OAuth 동의가 필요할 때, 응답이 `incomplete`일 때 나온다(`--expect-failure`를 쓰면 1).

## 증거 파일

증거 파일은 `evidence/foundry-byom/`에 있다.

| 파일 | 내용 |
|---|---|
| `<label>.connections.json` | `inspect_connections.py` 결과 |
| `<label>.create.json` | `create_agent.py` 결과와 저장된 모델 참조 확인 |
| `<label>.run.json` | `run_prompt.py` 결과 |
| `<label>.json` | `collect_evidence.py`의 자동 점검과 수동 점검 판정 |
| `<label>.apim-report.json` | 인프라 담당이 APIM 기록에서 읽어 보고한 값. 이 스크립트가 측정한 값이 아니다. |

- 응답 ID, 대화 ID, 요청 ID는 앞 8자만 남긴다(예: `resp_1a2b3c4d…`). 전체 값은 `BYOM_PRIVATE_DIR`의 개인 원본에만 있다.
- 구독 ID, tenant ID, 객체 ID, 전체 리소스 ID, 호스트, 키, 토큰은 저장하지 않는다. 시험(`HygieneTests`)이 `demo/foundry-byom/agent/`와 `evidence/foundry-byom/`의 파일에서 GUID, 이메일, JWT, 리소스 호스트, 구독 경로가 든 리소스 ID, 전체 응답·요청 ID를 찾는다.
- 증거마다 확인됨, 실패, 미확인을 나눠 적는다. 미확인을 확인됨으로 바꾸려면 실제 호출의 증거가 있어야 한다.
- 모델 API 담당이 보고한 직접 재생 결과와 vLLM 토큰 증가분은 이 저장소에 증거 파일이 없다. DESIGN.md 3.4절에 "보고됨(모델 API 담당)"으로만 적었다.
- 기존 앱 경로(MAF `/api/agent`)의 Web IQ 성공은 이 데모의 증거가 아니다.

## 안전 규칙

1. 비밀은 환경변수로만 받는다. 출력, 파일, 커밋에 쓰지 않는다(DESIGN.md 5.3절).
2. 공개 저장소다. 배포별 이름, 호스트, GUID를 커밋하지 않는다. 연결 이름은 별칭을 쓴다.
3. 모델 호출은 최소로 한다. 동시성 1, 출력 상한 설정, thinking 끔.
4. 호출 예산은 `responses.create` 횟수가 아니라 APIM 요청 수로 센다. 모델 쪽 5xx는 Foundry가 다시 보낼 수 있다(추정). 1a 시도 1에서는 호출 1번이 APIM 요청 4건이 됐다. MCP 호출은 `responses.create` 1번에 APIM 요청이 2건일 것으로 예상한다(추정). 인프라 담당이 확인한 누적 요청은 ablation 재시험 전까지 19건이고 상한은 32건이다(DESIGN.md 8.3절).
5. 호출은 순차로 하고(동시성 1) 호출 사이에 15초 이상 둔다. ablation 재시험은 45초 이상으로 정했고 실제 간격은 50초 이상이었다.
6. 첫 예상 밖 실패에서 멈추고 오류 원문을 보고한다. 이 스크립트는 자동 재시도를 하지 않는다. 승인받은 재시험이 아니면 실패한 호출을 다시 보내지 않는다.
7. 허용되는 클라우드 변경은 기존 프로젝트 안의 에이전트 버전, 대화, 응답 생성뿐이다. 새 리소스, RBAC, APIM, 웹앱, GPU는 건드리지 않는다. 삭제는 지시가 있을 때만 한다.

## 콘솔 확인

Foundry 포털에서 모델 설정, 대화, 도구 실행 기록을 확인하는 순서는 DESIGN.md 6.5절에 있다.
로그인과 MFA는 사용자가 직접 한다. 화면에 보이지 않는 항목은 "표시 없음"으로 기록한다. 캡처하기 전에 DESIGN.md 6.4절의 비식별 점검표를 적용한다.

## 시험

네트워크와 자격 증명을 쓰지 않는다. SDK가 없는 환경에서는 SDK 호환성 시험만 건너뛴다.

```bash
python -m unittest discover -s tests -p "test_foundry_byom*.py"
```

## 정리 대상

이 데모가 만든 클라우드 개체다. 삭제는 프로젝트 소유자와 합의한 뒤, 지시가 있을 때만 한다. 콘솔 시연에 쓸 수 있으므로 지금은 지우지 않는다.

에이전트는 7개 이름, 9개 버전이다. `create_agent.py --delete <이름>`은 그 이름의 모든 버전을 지운다.

| 에이전트 이름 | 버전 | 용도 |
|---|---|---|
| `byom-demo-agent` | 1 | NVFP4 도구 없는 에이전트(Phase 0.5) |
| `byom-demo-agent-fp8` | 1 | FP8 도구 없는 에이전트 |
| `byom-demo-fn-agent-fp8` | 1 | FP8 함수 도구 |
| `byom-demo-fn-agent-nv` | 1 | NVFP4 함수 도구 |
| `byom-demo-mcp-agent-fp8` | 1, 2, 3 | Learn MCP. 1은 약한 지시문, 2는 강한 지시문과 `temperature` 0.3(`never`), 3은 `always` |
| `byom-demo-webiq-agent-fp8` | 1 | Web IQ 요청 단위 헤더 시험용(400으로 거부됨) |
| `byom-demo-mcp-ablation-fp8` | 1 | 강한 지시문, `temperature` 미지정, `never`(U18, U19) |

대화는 15개다. 증거 이름과 대화 ID 앞 8자는 다음과 같다. `run_prompt.py`는 실행마다 대화를 하나 만들기 때문에 호출이 늘면 대화도 늘어난다.

| 증거 이름 | 대화 |
|---|---|
| `phase1a-plain`(503으로 실패) | `conv_0ec4f3b0…` |
| `phase1a-fp8-plain` | `conv_0f775a44…` |
| `phase1b-fp8-function` | `conv_02f4e790…` |
| `phase1c-fp8-learn-never` | `conv_0685a5c3…` |
| `phase1c-fp8-learn-never-v2` | `conv_0f494d95…` |
| `phase1c-fp8-learn-always` | `conv_0b874d9f…` |
| `phase1c-fp8-webiq-marker`(400으로 실패) | `conv_01a143cb…` |
| `phase1a-nv-plain` | `conv_00e032b9…` |
| `phase1b-nv-function` | `conv_0d87cab5…` |
| `redeploy-9cd2220-nv-plain` | `conv_0f8d5257…` |
| `redeploy-9cd2220-fp8-learn-never-v2` | `conv_07c048f8…` |
| `redeploy-9cd2220-fp8-learn-never-tempdefault`(500으로 실패. 대화 항목 0개를 조회로 확인) | `conv_06aa6c44…` |
| `retest-9cd2220-fp8-ablation-1` | `conv_0dd9df8a…` |
| `retest-9cd2220-fp8-ablation-2` | `conv_0e725356…` |
| `retest-9cd2220-fp8-ablation-3` | `conv_0847c851…` |

- 이 스크립트에는 대화 ID로 대화를 지우는 기능이 없다. `run_prompt.py --delete-conversation`은 그 실행이 만든 대화만 지운다. 이미 있는 대화를 지우려면 기능을 추가해야 하고 별도 승인이 필요하다(DESIGN.md 9.1절).
- 응답 객체의 저장 여부와 삭제 동작은 확인하지 않았다.
