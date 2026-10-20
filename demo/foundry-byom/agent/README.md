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
| Phase 1c: Learn MCP | FP8에서 Foundry가 도구를 실행했다(`mcp_call`). `never`는 강화한 지시문으로 성공했고, `always`는 승인 흐름이 성공했다. ablation 에이전트(`temperature` 미지정)는 첫 호출이 500이었다. 재시험 3회와 재실행 3회(`run_series.py`, DESIGN.md의 ⓐ)는 성공했다. 이 에이전트의 호출은 첫 호출, 재시험 3회, 재실행 3회, ⓑ 5회(약한 지시문 에이전트와 번갈아 보낸 10회 중), 캡처 3회로 모두 15회다. 2회가 Foundry 500(`model_error`)이고 13회가 완료다(도구를 부른 11회, 안 부른 2회). 500의 원인은 모른다(U18). 같은 에이전트의 새 버전(`temperature` 0.3, 다른 변경 없음)은 3회 모두 완료했고 도구를 불렀다(온도 0.3 확인). 표본이 작아 이 설정이 500을 막는다는 증거는 아니다. |
| Phase 1c: Web IQ MCP | 요청 단위 헤더는 400으로 거부됐다(`agent_reference`와 요청의 `tools`는 함께 쓸 수 없다). 키를 프로젝트 연결에 저장하는 방식은 사용자 승인을 기다린다. 실키는 쓰지 않았다. 더미 값으로 연결을 만들어 호출 1회를 연습했다(D-1). Foundry가 MCP 인증 실패를 요청 수준의 400(`tool_user_error`)으로 돌려줬고 APIM 요청은 0건이었다(인프라 담당 보고). 실키 호출(D-2)은 하지 않았다(DESIGN.md 4.6절). |
| 콘솔 확인 | 이 작업은 화면을 보지 못했고 캡처가 없다. 사용자가 포털에서 모델 배포가 보이고 플레이그라운드로 에이전트를 쓸 수 있음을 직접 확인했다고 전달했다. 그 화면이 BYOM 연결과 `byom-demo-*` 에이전트인지는 미확인이다(DESIGN.md U11). 로그인과 MFA는 사용자가 한다. |

Foundry 에이전트가 BYOM 연결로 모델 API에 요청하고 답을 받는 것, 함수 도구 왕복, Foundry 쪽 MCP 도구 실행은 확인했다.
사용 모델이 A.X K2라는 것은 아직 확정하지 않았다. 에이전트 정의와 응답의 `model` 필드는 모델 참조일 뿐이다. APIM 요청 본문의 모델 이름과 모델 런타임 출처가 같은 시간대로 맞아야 한다(DESIGN.md 7절). 인프라 담당이 읽은 APIM 요청 본문의 모델 이름은 재시험 6건, 재실행(ⓐ) 6건, ⓑ 13건, 온도 0.3 확인 6건 모두 `ax-k2-fp8`이었다. 캡처 3회는 요청 5건의 본문 모델 이름이 모두 `ax-k2-fp8`이었다(보고됨(인프라 담당), ID가 아니라 시각 순서와 간격으로 호출에 맞춤). 모델 API 담당이 보고한 vLLM 출력 토큰 증가분은 Foundry의 출력 usage와 같았다(18회 중 18회. 재시험 3회, 재실행 3회, ⓑ 완료 9회, 온도 0.3 확인 3회). 이것은 시간대 대조의 일부이며 요청 단위 대조는 아니다. 모델을 두 번 부른 12회는 입력 토큰 증가분의 합이 Foundry의 입력 usage보다 486개 많았다. 이 486은 482와 4의 합이다(관찰됨(산술 일치)). 482는 첫 요청의 프롬프트 토큰이다. 라운드를 나눠 읽은 7회는 첫 라운드가 482였고 둘째 라운드가 입력 usage보다 4 컸다. 재시험 3회의 482는 직접 측정한 값이 아니라 같은 에이전트이므로 맞는 수치로 쓴 것이다. 한 요청마다 4개가 달라지는 원인과, 입력 usage가 마지막 요청만 센 값이라는 해석은 확인하지 못했다(미확인). 근거는 DESIGN.md 3.4절의 "재시험의 vLLM 토큰 대조" 행에 있다.
확인된 사실과 미확인 항목의 최신 목록은 DESIGN.md 3.4절에 있다.

## 구성

| 파일 | 역할 | 클라우드 호출 |
|---|---|---|
| `inspect_connections.py` | 프로젝트 연결을 읽기 전용으로 조회하고 연결 계약과 비교한다. 자격 증명 값은 요청하지 않는다. | 읽기 |
| `create_agent.py` | 프롬프트 에이전트 버전을 멱등하게 만들고 저장된 모델 참조를 읽어 비교한다. | 에이전트 버전 쓰기와 읽기 |
| `run_prompt.py` | 에이전트를 Responses API로 한 번 호출하고 출력 항목, 도구 호출, 최종 답, usage를 요약한다. | 모델 호출 |
| `run_series.py` | 에이전트 버전 호출 묶음을 한 번에 하나씩 순서대로 실행하고 호출마다 기록한다. 호출 전에 에이전트 버전을 읽어 점검하고, 첫 비정상 호출에서 멈추며, 재시도하지 않는다. `--continue-on-model-error`를 쓰면 `model_error`로 끝난 호출만 기록하고 계속한다. `--precheck-only`는 점검만 하고 끝낸다. | 읽기(호출 전 점검), 모델 호출 |
| `collect_evidence.py` | 비밀을 제거한 증거 JSON을 만들고 점검 항목을 판정한다. | 읽기(`--offline`이면 없음) |
| `byom_common.py` | 설정 검증, 비밀 제거, `az` 토큰 요청의 구독 지정, 클라이언트 생성, 증거 파일 쓰기 | 없음 |
| `requirements.txt` | 필요한 패키지 | 없음 |

`run_prompt.py`와 `run_series.py`만 모델을 호출한다. `run_series.py`는 `run_prompt.py`의 실행 로직을 그대로 쓴다. `collect_evidence.py`는 에이전트 정의, 응답, 대화 항목을 읽기만 한다.

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
`FOUNDRY_PROJECT_ENDPOINT`와 MCP 서버 URL(`--mcp-url`, `FOUNDRY_BYOM_MCP_URL`)에 공백, 제어 문자, 비ASCII 문자가 있으면 설정 오류(종료 코드 2)다. CRLF 줄바꿈 파일에서 값을 읽으면 끝에 `\r`이 붙는 경우가 흔하므로 읽은 값에서 `\r`을 지운다.
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

# 7. 호출 묶음(모델 호출): 같은 프롬프트를 순서대로, 호출 사이에 간격을 두고 보낸다
python run_series.py --call <mcp-agent>:1 --repeat 3 --expect-calls 3 --label <label> --min-gap-s 60 --nonce <확인 코드>
# 7-1. 두 에이전트 버전을 번갈아 보낸다(A, B, A, B, ...)
python run_series.py --call <mcp-agent-a>:1 --call <mcp-agent-b>:1 --repeat 5 --expect-calls 10 --label <label>
# 7-2. model_error로 끝난 호출은 기록하고 다음 호출을 보낸다(그 밖의 비정상 호출은 여전히 멈춘다)
python run_series.py --call <mcp-agent-a>:1 --call <mcp-agent-b>:1 --repeat 5 --expect-calls 10 --label <label> --continue-on-model-error
# 7-3. 모델을 호출하지 않고 에이전트 버전만 읽어 점검한다(읽기 전용, 파일을 쓰지 않는다)
python run_series.py --call <mcp-agent-a>:1 --call <mcp-agent-b>:1 --label <label> --precheck-only
```

4단계의 함수는 `run_prompt.py`가 로컬에서 실행한다. Foundry가 도구를 실행하는 경로는 5단계의 MCP다.
지시문 파일에는 질문에 답하기 전에 `microsoft_docs_search`를 먼저 호출하고 그 결과에만 근거해 답하라고 쓴다. 기본 지시문과 `temperature` 미지정 조건에서는 모델이 도구를 부르지 않은 적이 있다(DESIGN.md U19).
`--temperature 0.3`은 예방 조치다. 이 설정이 Foundry의 500을 막는다는 증거는 없다(DESIGN.md U18).
`--prompt-file`로 프롬프트를 파일에서 읽을 수 있다. `run_prompt.py`는 실행마다 대화를 하나 만들고 콘솔에서 볼 수 있도록 남긴다. 지우려면 `--delete-conversation`을 쓴다.
Web IQ MCP는 요청 단위 헤더(옵션 B)가 400으로 거부되어 쓸 수 없다. 키를 프로젝트 연결에 저장하는 옵션 D만 남았고, 키가 Foundry에 저장되므로 사용자 승인 뒤에만 한다(DESIGN.md 4.5절, 4.6절). `run_prompt.py --mcp-request-header`는 옵션 B를 시험하려고 만든 실험 옵션이며 Foundry가 거부했다.
옵션 D의 연습(D-1)은 더미 값으로 한 번 했다(DESIGN.md 3.4절, 4.6절). 연결 이름은 `create_agent.py --mcp-connection <연결 이름>`으로 넘기며 `project_connection_id`를 채운다. 키 값이 서버 기록에 남았는지는 `collect_evidence.py --scan-env <변수>`로 검사한다. 실키를 쓰는 호출은 하지 않았고 사용자 승인 뒤에만 한다.

7단계의 `run_series.py`는 다음 규칙으로 호출한다.

- `--call <이름>:<버전>`에는 버전을 반드시 적는다. 여러 번 적으면 적은 순서가 한 바퀴이고 `--repeat`만큼 되풀이한다. 한 묶음은 12호출까지다.
- `--expect-calls`는 실제 호출 수와 같아야 한다. `--dry-run`이나 `--precheck-only`가 아니면 필수다.
- 모델을 호출하기 전에 일정에 든 에이전트 버전을 한 번씩 읽는다(GET). 이름이나 버전이 틀리면 모델 호출 없이 멈춘다.
- `--precheck-only`는 이 읽기까지만 하고 끝난다. 프로젝트에는 연결하지만 모델 요청을 보내는 클라이언트는 만들지 않는다. 에이전트 정의 요약(`ok`, `agents`)만 출력하고 파일은 쓰지 않는다. `--dry-run`과 함께 쓰면 설정 오류(종료 코드 2)다. 점검이 실패하면 `stage`와 오류 요약을 출력하고 종료 코드 1이다.
- 호출마다 새 대화를 쓴다. 호출 사이 간격은 앞 호출이 끝난 시각부터 센다(기본 60초, 최소 15초).
- 확인 코드는 호출마다 무작위로 정한다. 이전 실행과 같은 프롬프트를 보내려면 `--nonce <코드>`로 하나를 지정한다. 이때는 모든 호출이 같은 확인 코드를 쓰므로 호출은 시각으로 구분한다.
- 출력 상한은 호출마다 `--max-output-tokens`(기본 384, 1 이상)로 건다. SDK 재시도는 꺼져 있다.
- `completed`가 아닌 호출이 처음 나오면 멈춘다(오류, 5xx, 시간 초과, `failed`, `incomplete`, 승인 필요). 그 호출을 다시 보내지 않고 다음 호출도 보내지 않는다. 모델이 MCP 도구를 부르지 않고 `completed`로 끝난 호출은 정상 결과이므로 멈추지 않는다.
- `--continue-on-model-error`를 쓰면 `model_error`로 끝난 호출은 멈추지 않는다. 그 호출을 기록하고, 간격이 지난 뒤 다음 호출을 보낸다. 그 호출도 다시 보내지 않는다. `model_error`는 HTTP 500이거나 `failed` 응답이고 서비스 오류의 type 또는 code가 `model_error`인 경우다. 503 "model backend not ready"는 code가 `model_error`일 수 있으나 `model_error`로 세지 않으며, 이 옵션을 써도 멈춘다. 시간 초과, 429, `incomplete`, 승인 필요도 멈춘다.
- 호출이 하나 끝날 때마다 시리즈 파일을 다시 쓴다. 중간에 멈추면 그때까지의 기록이 남는다.
- 종료 코드는 모든 호출이 `completed`이면 0이다. 멈춘 경우에는 멈춘 호출의 `run_prompt.py` 종료 코드와 같다. 호출 전 점검이 실패하면 1이고, 설정 오류면 2다. `--continue-on-model-error`로 `model_error` 호출을 넘기고 끝까지 갔을 때도 0이므로 `summary`의 `model_error` 수를 함께 본다.

## 실행 결과 읽는 법

`run_prompt.py`는 비밀을 제거한 JSON 요약을 출력한다.

| 키 | 뜻 |
|---|---|
| `stopped`, `ok` | 끝난 이유와 성공 판정. `stopped`가 `completed`면 성공이다. |
| `request` | 호출 조건. 시나리오, 에이전트 이름, 확인 코드(`nonce`), 상한(`limits`: `max_output_tokens`, `max_tool_calls`, `max_rounds`, `timeout_s`), 승인한 도구 |
| `started_at`, `last_request_at`, `finished_at` | 시작, 마지막 요청, 종료 시각. 클라이언트 시계 기준 UTC이고 밀리초 단위다(`YYYY-MM-DDTHH:MM:SS.mmm+00:00`, KST는 UTC+9). 인프라 담당이 APIM 기록과, 모델 API 담당이 모델 쪽 토큰 카운터와 시간대를 맞출 때 쓴다. |
| `rounds` | 응답마다 한 항목. 주요 키는 `response_id`, `request_id`(서비스가 돌려준 요청 ID의 앞 8자, 없으면 `null`), `status`, `model`, `output_item_types`, `usage`, `error`, `chained_by`, `requested_at`, `responded_at`, `elapsed_s`다. |
| `item_sequence` | 출력 항목 종류의 순서. 도구 호출 흔적을 본다. |
| `tool_events` | 도구 목록, 도구 호출, 승인 요청 항목의 요약 |
| `function_results` | 로컬에서 실행한 함수의 결과 |
| `final_answer`, `final_answer_contains_nonce` | 최종 답과 확인 코드(`--nonce`) 포함 여부 |
| `client_http` | 클라이언트가 실제로 보낸 HTTP 요청 수. `responses_posts`가 `responses.create` 호출 수보다 크면 SDK가 다시 보낸 것이다. SDK 자동 재시도는 꺼져 있다(`max_retries=0`). |
| `error` | 실패한 호출의 상태 코드, Foundry 오류 코드, 메시지. 서비스가 돌려준 오류 type은 `api_type`에 적고, `type`은 예외 클래스 이름이다. 오류가 알려 준 `param`과 요청 ID(앞 8자)가 있으면 함께 적는다. |
| `conversation_id`, `conversation_deleted` | 이 호출이 만든 대화의 ID(증거 파일에는 앞 8자). `--delete-conversation`을 쓴 호출만 삭제 결과가 들어가고, 아니면 `null`이다. |
| `request_header_echoed` | `--mcp-request-header`를 쓴 호출에서 헤더 값이 Foundry 응답에 다시 나타났는지 검사한 결과. 쓰지 않았으면 `null`이다. 값이 나타나면 Foundry에 저장된 것으로 보고 그 값을 교체한다. |

`rounds[].model`은 에이전트의 모델 참조(`<연결 별칭>/<모델 이름>`)다. 이 값만으로는 A.X K2가 답했다고 말할 수 없다(DESIGN.md 7절).

종료 코드는 0이 성공, 1이 실행 또는 API 오류, 2가 설정 오류, 3이 미완이다. 3은 승인이 필요한 도구 호출을 `--approve-tool`로 승인하지 않았을 때, `max_rounds`를 다 썼을 때, OAuth 동의가 필요할 때, 응답이 `incomplete`일 때 나온다(`--expect-failure`를 쓰면 1).

### 호출 묶음 결과(`<label>.series.json`)

`run_series.py`는 호출마다 한 항목을 `calls`에 쌓는다. 프롬프트, 도구 입출력, 최종 답의 원문은 넣지 않고 길이와 항목 종류만 넣는다. 원문 요약은 호출마다 저장하는 `<label>-NN.run.json`에 있다.
Foundry 쪽 span(`invoke_agent`, `chat`, `execute_tool`), APIM 기록, 모델 쪽 토큰 카운터는 이 파일에 없다. 각 담당이 호출 시각과 확인 코드로 맞춘다.

| 키 | 뜻 |
|---|---|
| `plan` | 일정. 호출 목록, 시나리오, 간격, 상한, `continue_on_model_error`, 중단 규칙(`stop_rule`) |
| `agents` | 호출 전에 읽은 에이전트 버전의 정의 요약. 모델 참조, `temperature`, 도구, 지시문 길이와 해시 앞 12자 |
| `planned_calls`, `calls` | 계획한 호출 수와 실제로 실행한 호출의 기록. 중간에 멈추면 `calls`가 더 짧다. |
| `summary` | 호출 수, 성공 수, MCP 도구를 부른 수, MCP 없이 끝난 수, `model_error` 수. `by_agent`는 에이전트 버전별 같은 집계 |
| `stopped_early`, `stop_reason`, `error` | 중간에 멈췄는지, 이유, 호출 전 점검이 실패했을 때의 오류 |

`calls[]`의 키는 다음과 같다.

| 키 | 뜻 |
|---|---|
| `index`, `agent`, `version`, `nonce` | 호출 번호, 에이전트 이름과 버전, 확인 코드 |
| `started_at`, `first_request_at`, `finished_at`, `duration_ms` | 시작, 첫 요청, 종료 시각(UTC, 밀리초)과 소요 시간 |
| `gap_before_s` | 앞 호출이 끝난 뒤 이 호출을 시작하기까지 잰 간격. 첫 호출은 `null` |
| `ok`, `stopped`, `error`, `model_error` | 성공 여부, 끝난 이유, 오류 요약, `model_error` 여부. `error.type`은 예외 클래스 이름이고 서비스가 돌려준 오류 type은 `error.api_type`이다. `model_error`는 HTTP 500(또는 `failed` 응답)이고 `error.code`나 `error.api_type`이 `model_error`일 때만 `true`다. 503은 code가 `model_error`여도 `false`다. |
| `item_sequence`, `mcp_called`, `mcp_calls` | 출력 항목 종류의 순서, MCP 도구를 불렀는지, 도구 호출마다 이름·상태·인자가 객체인지·질의 길이(`query_chars`)·출력 길이(`output_chars`). 질의 원문은 넣지 않는다. |
| `answer_chars`, `final_answer_contains_nonce`, `think_tag` | 최종 답 길이, 확인 코드 포함 여부, `<think>` 태그 여부 |
| `response_model`, `response_temperature`, `response_top_p`, `response_id`, `conversation_id`, `usage`, `rounds` | 마지막 응답의 `model`(에이전트 참조), 응답 객체가 돌려준 `temperature`와 `top_p`(숫자가 아니면 `null`), ID, 대화 ID, 마지막 라운드의 usage, 라운드별 시각·요청 ID·`temperature`·`top_p`·usage. `temperature`와 `top_p`는 서비스가 응답에 되돌려 준 값이며 모델 API가 받은 요청 본문의 값이 아니다. |
| `client_http` | 이 호출 하나에서 클라이언트가 보낸 HTTP 요청 수. `requests`는 대화 생성과 Responses POST를 합친 수이고 `responses_posts`는 Responses POST 수다. |
| `contaminated`, `contamination_note` | 다른 트래픽이 겹친 호출을 사람이 표시하는 칸. 스크립트는 `null`로 두고 `summary`는 이 표시를 반영하지 않는다. |

## 증거 파일

증거 파일은 `evidence/foundry-byom/`에 있다.

| 파일 | 내용 |
|---|---|
| `<label>.connections.json` | `inspect_connections.py` 결과 |
| `<label>.create.json` | `create_agent.py` 결과와 저장된 모델 참조 확인 |
| `<label>.run.json` | `run_prompt.py` 결과 |
| `<label>.series.json` | `run_series.py` 결과. 일정, 호출 전 점검, 호출별 기록, 요약 |
| `<label>-NN.run.json` | `run_series.py`가 호출마다 저장하는 `run_prompt.py` 결과. NN은 호출 번호 |
| `<label>.json` | `collect_evidence.py`의 자동 점검과 수동 점검 판정 |
| `<label>.apim-report.json` | 인프라 담당이 APIM 기록에서 읽어 보고한 값. 이 스크립트가 측정한 값이 아니다. |

- 응답 ID, 대화 ID, 요청 ID는 앞 8자만 남긴다(예: `resp_1a2b3c4d…`). 전체 값은 `BYOM_PRIVATE_DIR`의 개인 원본에만 있다.
- 구독 ID, tenant ID, 객체 ID, 전체 리소스 ID, 호스트, 키, 토큰은 저장하지 않는다. 시험(`HygieneTests`)이 `demo/foundry-byom/agent/`와 `evidence/foundry-byom/`의 파일에서 GUID, 이메일, JWT, 리소스 호스트, 구독 경로가 든 리소스 ID, 전체 응답·요청 ID를 찾는다.
- 증거마다 확인됨, 실패, 미확인을 나눠 적는다. 미확인을 확인됨으로 바꾸려면 실제 호출의 증거가 있어야 한다.
- 모델 API 담당이 보고한 직접 재생 결과와 vLLM 토큰 증가분은 이 저장소에 증거 파일이 없다. DESIGN.md 3.4절에 "보고됨(모델 API 담당)"으로만 적었다.
- 재시험 3회의 APIM 기록은 인프라 담당이 PR #28(48f9c26)의 `gateway-requests-foundry-ablation-retest-{1,2,3}.json`에 올렸고 그 PR은 병합됐다.
- 재실행 3회(ⓐ)의 증거는 `rerun-ablation-fp8-a.series.json`과 `rerun-ablation-fp8-a-{01,02,03}.run.json`이다. 호출별 시작·종료 시각이 UTC 밀리초로 들어 있다. 이 재실행의 APIM 요청별 기록과 Foundry span은 인프라 담당이 읽었다(보고됨). 그 증거 파일은 인프라 담당의 PR #32가 추가하는 `gateway-requests-foundry-f3-ablation-{1,2,3}.json`과 `foundry-spans-f3-ablation.json`이다(PR #32가 병합돼야 이 저장소에 생긴다). 요청 본문의 모델 이름은 6건 모두 `ax-k2-fp8`이었다(보고됨(인프라 담당)). 9f92a3f 이전에 만든 증거 파일의 시각은 초 단위다.
- ⓑ(두 에이전트를 번갈아 10회)의 증거는 `rerun-ablation-fp8-b.series.json`과 `rerun-ablation-fp8-b-{01..10}.run.json`이다. 호출 2는 Foundry 500(`model_error`)이었고 응답 항목과 usage가 없다. 이 호출의 APIM 요청별 기록과 Foundry span은 인프라 담당이 읽었다(보고됨). 그 증거 파일은 인프라 담당의 PR #32가 추가하는 `gateway-requests-foundry-f3b-series.json`과 `foundry-spans-f3b-series.json`이다(PR #32가 병합돼야 이 저장소에 생긴다).
- 캡처 3회의 증거는 `capture-ablation-fp8-b.series.json`과 `capture-ablation-fp8-b-{01,02,03}.run.json`이다. 인프라 담당이 APIM 기록을 켠 구간에 보냈다. 이 호출의 APIM 요청 5건과 Foundry span 8개는 인프라 담당이 읽었다(보고됨). 그 증거 파일은 인프라 담당의 PR #32가 추가하는 `gateway-requests-foundry-f3c-body-capture.json`과 `foundry-spans-f3c-body-capture.json`이다(PR #32가 병합돼야 이 저장소에 생긴다). 호출과의 대응은 ID가 아니라 시각 순서와 간격으로 맞춘 것이다. 요청 본문의 모델 이름은 5건 모두 `ax-k2-fp8`이었다(보고됨(인프라 담당)).
- 온도 0.3 확인 3회의 증거는 `temp03-ablation-fp8-b.create.json`(새 에이전트 버전), `temp03-ablation-fp8-b.series.json`, `temp03-ablation-fp8-b-{01,02,03}.run.json`이다. 응답 객체의 `temperature`는 3회 모두 0.3000000119209289(0.3의 float32 값)이고 `top_p`는 1.0이다. APIM 요청 본문의 `temperature`도 0.3이었다(보고됨(인프라 담당)). Foundry span에는 `temperature` 속성이 없다(보고됨(인프라 담당)).
- D-1(더미 키 연습)의 증거는 `d1-dummy-webiq.create.json`, `d1-dummy-webiq.series.json`, `d1-dummy-webiq-01.run.json`, `d1-dummy-webiq-01.json`이다. 호출 1회가 HTTP 400(`tool_user_error`)으로 끝났다. 더미 값은 어떤 파일에도 없다. 오류 본문의 요청 ID와 대화 ID는 앞 8자만 남겼다.
- 기존 앱 경로(MAF `/api/agent`)의 Web IQ 성공은 이 데모의 증거가 아니다.

## 안전 규칙

1. 비밀은 환경변수로만 받는다. 출력, 파일, 커밋에 쓰지 않는다(DESIGN.md 5.3절).
2. 공개 저장소다. 배포별 이름, 호스트, GUID를 커밋하지 않는다. 연결 이름은 별칭을 쓴다.
3. 모델 호출은 최소로 한다. 동시성 1, 출력 상한 설정, thinking 끔.
4. 호출 예산은 `responses.create` 횟수가 아니라 APIM 요청 수로 센다. 모델 쪽 5xx는 Foundry가 다시 보낼 수 있다(추정). 1a 시도 1에서는 호출 1번이 APIM 요청 4건이 됐다. MCP `never`에서 도구를 부른 호출은 `responses.create` 1번에 APIM 요청이 2건이었다(인프라 담당 보고). 인프라 담당이 확인한 누적 요청은 재시험까지 25건이다. 재실행 3회(ⓐ)의 요청 6건과 ⓑ의 요청 13건은 응답 ID와 요청 ID로 호출마다 맞춰졌다(인프라 담당 보고). 그래서 확인된 누적은 44건이다. 캡처 3회는 요청 5건이었다(인프라 담당 보고, ID가 아니라 시각 순서와 간격으로 호출에 맞춤). 온도 0.3 확인 3회는 요청 6건이었고, D-1의 400 호출은 0건이었다(인프라 담당 보고). 인프라 담당이 센 이 작업의 누적은 캡처 5건을 포함해 55건이다. 상한은 64건이고 이 작업이 보낸 요청만 센다. 이 작업이 보내지 않은 게이트웨이 요청 15건은 누적에 넣지 않았다(DESIGN.md 3.4절, 8.3절).
5. 호출은 순차로 하고(동시성 1) 호출 사이에 15초 이상 둔다. ablation 재시험은 45초 이상으로 정했고 실제 간격은 50초 이상이었다. 재실행 3회(ⓐ), ⓑ, 캡처, 온도 0.3 확인은 60초 이상으로 정했고 실제 간격은 60.0초였다.
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

에이전트는 8개 이름, 11개 버전이다. `create_agent.py --delete <이름>`은 그 이름의 모든 버전을 지운다.

| 에이전트 이름 | 버전 | 용도 |
|---|---|---|
| `byom-demo-agent` | 1 | NVFP4 도구 없는 에이전트(Phase 0.5) |
| `byom-demo-agent-fp8` | 1 | FP8 도구 없는 에이전트 |
| `byom-demo-fn-agent-fp8` | 1 | FP8 함수 도구 |
| `byom-demo-fn-agent-nv` | 1 | NVFP4 함수 도구 |
| `byom-demo-mcp-agent-fp8` | 1, 2, 3 | Learn MCP. 1은 약한 지시문, 2는 강한 지시문과 `temperature` 0.3(`never`), 3은 `always` |
| `byom-demo-webiq-agent-fp8` | 1 | Web IQ 요청 단위 헤더 시험용(400으로 거부됨) |
| `byom-demo-mcp-ablation-fp8` | 1, 2 | 강한 지시문, `never`. 1은 `temperature` 미지정(U18, U19), 2는 `temperature` 0.3(온도 0.3 확인) |
| `dummy-d1-webiq-agent-fp8` | 1 | D-1 더미 키 연습용. FP8, `temperature` 0.3, Web IQ MCP, 더미 키를 담은 프로젝트 연결 사용 |

대화는 35개다. 증거 이름과 대화 ID 앞 8자는 다음과 같다. `run_prompt.py`는 실행마다 대화를 하나 만들기 때문에 호출이 늘면 대화도 늘어난다.

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
| `rerun-ablation-fp8-a-01`(`byom-demo-mcp-ablation-fp8` 1) | `conv_05bd7adf…` |
| `rerun-ablation-fp8-a-02` | `conv_0acb2e05…` |
| `rerun-ablation-fp8-a-03` | `conv_035d6d92…` |
| `rerun-ablation-fp8-b-01`(`byom-demo-mcp-agent-fp8` 1) | `conv_008e5ca1…` |
| `rerun-ablation-fp8-b-02`(`byom-demo-mcp-ablation-fp8` 1. 500으로 실패. 대화 항목 0개를 조회로 확인) | `conv_0b186c53…` |
| `rerun-ablation-fp8-b-03`(`byom-demo-mcp-agent-fp8` 1) | `conv_003184ea…` |
| `rerun-ablation-fp8-b-04`(`byom-demo-mcp-ablation-fp8` 1) | `conv_0996e58c…` |
| `rerun-ablation-fp8-b-05`(`byom-demo-mcp-agent-fp8` 1) | `conv_0ff63cae…` |
| `rerun-ablation-fp8-b-06`(`byom-demo-mcp-ablation-fp8` 1) | `conv_086e4b19…` |
| `rerun-ablation-fp8-b-07`(`byom-demo-mcp-agent-fp8` 1) | `conv_0e727b85…` |
| `rerun-ablation-fp8-b-08`(`byom-demo-mcp-ablation-fp8` 1) | `conv_0e2d2abb…` |
| `rerun-ablation-fp8-b-09`(`byom-demo-mcp-agent-fp8` 1) | `conv_029a68fe…` |
| `rerun-ablation-fp8-b-10`(`byom-demo-mcp-ablation-fp8` 1) | `conv_0755da41…` |
| `capture-ablation-fp8-b-01`(`byom-demo-mcp-ablation-fp8` 1) | `conv_0a1aa325…` |
| `capture-ablation-fp8-b-02` | `conv_0045a00e…` |
| `capture-ablation-fp8-b-03` | `conv_04e55fc6…` |
| `temp03-ablation-fp8-b-01`(`byom-demo-mcp-ablation-fp8` 2) | `conv_0eb5f9fe…` |
| `temp03-ablation-fp8-b-02` | `conv_04e92587…` |
| `temp03-ablation-fp8-b-03` | `conv_0204fc97…` |
| `d1-dummy-webiq-01`(`dummy-d1-webiq-agent-fp8` 1. HTTP 400으로 실패) | `conv_003f50ba…` |

- 더미 연결 `dummy-d1-webiq`는 인프라 담당이 만들었다. 삭제하지 않았고 인프라 담당의 정리 목록에 있다. D-1이 쓴 더미 값은 저장소 밖의 개인 파일에만 있다.
- 사용자가 2026-10-20에 포털에서 직접 만든 항목은 이 목록에 없다. 모델 배포 2개(`gpt-5`, `text-embedding-3-large`)와 포털이 이름을 붙인 에이전트 1개다. 이 작업이 만든 것이 아니므로 정리 대상에 넣지 않는다(DESIGN.md U11).
- 이 스크립트에는 대화 ID로 대화를 지우는 기능이 없다. `run_prompt.py --delete-conversation`은 그 실행이 만든 대화만 지운다. 이미 있는 대화를 지우려면 기능을 추가해야 하고 별도 승인이 필요하다(DESIGN.md 9.1절).
- 응답 객체의 저장 여부와 삭제 동작은 확인하지 않았다.
