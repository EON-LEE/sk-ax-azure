# A.X hosted-agent 로컬 계약 실험

이것은 **클라우드 배포가 아니다**. 실제 Microsoft Agent
Framework와 Foundry `ResponsesHostServer`를 기존 A.X 텍스트 chat
wire 계약에 대해 실행해 본다. 테스트는 A.X 추론이 아니라 결정적인 transport를 제공한다.
로컬 모드는 loopback이 아닌 엔드포인트를 거부한다. opt-in 방식의 인증된 클라우드 모드는
전용 NVFP4 Hosted gateway route와 그 API 애플리케이션
audience만 받는다. GPT client, fallback, 데모 자격 증명, Teams 게시는
포함하지 않는다. 자격 증명 fixture는 클라우드 identity 지원의 증거가 아니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --pre -r experiments\hosted_ax\requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_hosted_ax.py
```

Hosted 계약과 미등록 MCP 계약을 모두 실행하려면
`.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_hosted_ax*.py"`를 사용한다.

진입점은 `/api/chat`(FP8) 또는 `/models/nvfp4/api/chat`(NVFP4)로 끝나는
`AX_CHAT_ENDPOINT`를 **loopback에서만** 받는다. 이것은 실험이며,
OpenAI 호환 모델 URL이 아니다. 운영 `/api/agent`는 여전히 App Service MAF
agent이며, 이를 proxy해도 Hosted Agent 실행을 입증하지 못한다.

## 독립 클라우드 실험 A

배포 실험은 회사 Teams/Microsoft 365
channel bridge와 분리되어 있다. 두 tenant는 의도적으로 다르다. Foundry 직접 게시는
tenant 간 설계가 아니다. GitHub MCP 작업은 보류 중이다.

Source ZIP의 경우, `main.py`와 `requirements-cloud.txt`의 내용
(`requirements.txt`라는 이름으로)을 ZIP 루트에 둔다. `python_3_13`, `remote_build`,
Responses protocol `2.0.0`, 진입점 `["python", "main.py"]`를 선택한다.
클라우드 패키지에는 MCP가 포함되지 않는다.

`AX_HOSTED_RUNTIME=1`을 설정하고, `AX_CHAT_ENDPOINT`를
`https://axk2-a100-demo.azurewebsites.net/models/nvfp4/api/hosted/chat`로,
`AX_GATEWAY_SCOPE`를 승인된 gateway의 `api://<application-id>/.default`로 설정한다.
이 값들은 secret이 아닌 설정이다. 런타임은 port 8088에 바인딩하고,
`ManagedIdentityCredential`로 토큰을 얻는다. 개발자 로그인 fallback이나
내장 자격 증명은 쓰지 않는다. 이 사용자 지정 audience에 대해 플랫폼이 실제로 agent identity를
발급하는지는 사용 가능하다고 선언하기 전에 클라우드에서 확인해야 한다.

gateway는 issuer, 의도한 audience, 만료, 서명,
`Model.Invoke` role, 승인된 Hosted 호출자 identity를 강제해야 한다. ARM, Graph,
Foundry 서비스 토큰은 gateway 토큰이 아니다. HTTP redirect와 자동
추론 재시도는 비활성화되어 있다. 요청 correlation에는 prompt 텍스트가 아니라
무작위 식별자가 들어간다. 텍스트 전용 요청은 출력 토큰을 최대 64개 사용한다.

gateway의 전역 3회 호출 시험 할당량과 동시성 1 gate는 세션별 Hosted lock으로
대체할 수 없다. 합성 텍스트만 승인되었다. 클라우드
agent 배포와 API identity/gateway는 승인된 제한적 작업이다.
회사 channel 등록/게시와 리소스 삭제에는 별도
승인이 필요하다. 이 로컬 테스트는 실제 Hosted A.X 성공을 주장하지 않는다.

## 승인 단계 경계

대상 경로: Teams -> Foundry Activity bridge -> Responses -> Hosted MAF
orchestration -> 인증된 모델 gateway -> 기존 A.X reverse relay.
gateway는 모델 추론만 전달해야 하며, `/api/agent`를 호출하면 안 된다.
gateway의 권한 부여는 최소 권한으로 agent identity와 의도한 audience를 검증해야 한다.
데모 cookie/암호는 운영 인증 설계가 아니다.
GPU port는 private으로 유지한다. 기존 GPU job과 compute는 변경하지 않아야 한다.
승인된 frontend 변경은 인증된 모델 전용 route로 한정된다.
기존 relay에는 검토된 agent-identity 모델 엔드포인트가 없다. `ChatRelay`에 연결한
별도의 모델 전용 route가 필요할 수 있다. 이는 승인이 필요한 운영 소유자
변경이며, HTTP adapter만으로 제공할 수 있는 것이 아니다.

현재 `/api/chat` allow-list는 임의의 MAF tool을 전달하지 않고 tool schema를
고정한다. 따라서 이 실험은 텍스트만 지원하며 tool/파일 입력은 명시적으로 거부한다.
tool은 검토된 모델 전용 gateway가 필요한 tool-call 계약을 지원한 뒤에
Hosted MAF 안에서 실행해야 한다. Teams 첨부 파일,
PDF 처리, 사용자 위임, 종단 간 취소는 아직 검증되지 않았다.

`ResponsesHostServer`는 AgentServer history를 사용하며, 이 비저장 client에 대해
downstream 저장 옵션을 제거한다. 그렇다고 Foundry나 Teams의
보존이 비활성화되는 것은 **아니다**. 대화 간 memory를 추가하지 않는다. Foundry는 대화
history를 세션별 sandbox 상태와 별도로 저장한다. 문서에 나온, 30일간 비활성 후의
sandbox 삭제는 대화 삭제를 보장하지 않는다. 회사 데이터를 사용하기 전에
reset/삭제 API, tenant 보존 정책, telemetry를 검토한다.
테스트에서는 로컬 SDK 상태에 임시 `AGENTSERVER_STATE_ROOT`를 사용해야 한다.
이 로컬 history 테스트는 tenant 권한 부여나 사용자 간 보안
테스트가 아니다. 로컬 Responses API 취소는 fixture 모델 stream을 닫는다. 실제
Teams 취소와 GPU 중단 전파는 여전히 종단 간 증거가 필요하다.

## 2026-10-08에 확인한 공식 근거

- [Hosted agents](https://learn.microsoft.com/en-us/azure/foundry/agents/concepts/hosted-agents):
  BYO code, downstream 서비스, Responses-to-Activity Teams bridge, 격리된
  세션. Korea Central이 나열되어 있다. Hosted는 관리형 실행을 뜻하며, no-code를 뜻하지 않는다.
- [새 agent 모델](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/migrate-agent-applications):
  legacy Agent Applications가 아니라 고유 identity와 안정적인 엔드포인트를 사용한다.
- [Teams 게시](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot)
  및 [REST 게시](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/publish-copilot-virtual-network)
  (2026-10-06): 현재 문서는 REST 게시를 명시적으로 지원한다.
  이는 migration 문서의 portal 전용이라는 설명과 모순된다. 실제 API 검증 전까지는
  더 새롭고 목적에 맞는 게시 계약을 우선한다.
- [배포](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent):
  port 8088, Linux amd64 이미지, 전용 agent identity, 과금되는 CPU/memory.
- [Source ZIP 배포](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/deploy-hosted-agent-code):
  ACR 없이 쓸 수 있는 선택 경로, Python 3.13/3.14 또는 .NET 10. **여기서는 테스트하지 않았다**.
- [게시 문제 해결](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/troubleshoot-publish-copilot):
  Copilot Chat은 기본 agent 사용에 Copilot 라이선스를 요구하지 않는다. 그러나 tool과
  tenant 데이터 접근에는 라이선스/사용량 요건이 있을 수 있다. 회사 Teams
  가용성, 앱 정책, 사용 권한은 여전히 tenant에서 확인해야 한다.

이 private A.X 모델을 native prompt-agent로 등록할 수 있다는 근거는 없다.
조사 중인 지원 경로는 보안 downstream gateway를 통해 모델을 호출하는
BYO-code Hosted Agent이다.

## 별도로 필요한 승인

프로젝트나 registry를 재사용하기 전에 Foundry/Teams tenant 관계와 리소스 소유자 권한을
확인한다. 제한된 호스팅 비용/시간 한도, 새 agent identity/version,
필요한 Foundry/RBAC 연결을 승인받는다. 컨테이너 경로에서는
이미지 업로드와 범위가 제한된 ACR pull/push 접근을 승인받는다. 새 ACR이
반드시 필요한 것은 아니다. Source ZIP은 대안이며, 별도의 업로드
승인과 지원되는 런타임이 필요하다.

모델 gateway 위치, 인증/네트워크 변경, trace
정책은 운영 소유자와 별도로 승인받는다. Bot Service provider
등록/리소스/channel 생성과 Teams 게시/설치/테스트
메시지는 각각 별도로 승인받는다. 조직 전체 접근이 아니라 개인 범위부터 시작한다.
문서에 나온 개인 게시 흐름은 조직 전체 승인이 필요 없지만,
여전히 Azure 권한과 호환되는 tenant 정책이 필요하다. tenant 범위에는
Microsoft 365 관리자 승인이 필요하다.

Hosted 실행 및 A.X 추론 trace와 연관된 실제 회사 Teams 메시지만이
최종 목표를 충족한다. 로컬 fixture 성공은 충족하지 않는다.

## client 세 개, 통합 계약 두 개

| Client | 공통 Hosted A.X agent에 연결하는 방식 | 성공이 증명하는 것 |
| --- | --- | --- |
| 회사 Teams | Responses로 가는 Foundry Activity bridge, 개인 게시 범위 | Hosted 실행과 A.X 추론 trace가 있는 실제 1:1 답장 |
| Microsoft 365 Copilot | 같은 Foundry Microsoft 365 게시 흐름과 엔드포인트 | trace가 연관된 실제 Copilot agent 대화 |
| GitHub Copilot CLI/app/IDE | 명시적 `ask_ax` MCP tool이 Hosted Responses 엔드포인트에 위임한다 | Copilot이 tool을 호출하고 실제 Hosted A.X 결과를 받는다 |

GitHub MCP 위임은 Copilot의 주 모델을 대체하거나, A.X를 Copilot의
기본 모델 선택기에 추가하거나, Copilot의 모든 추론이 A.X에서 실행됨을 증명하지 **않는다**. Copilot 호스트는
반환된 텍스트를 자체 모델로 처리하고 요약할 수 있다. 회사
prompt/결과를 어느 서비스로든 전송하려면 해당 승인이 필요하다.

`mcp_wrapper.py`는 **미등록 로컬 전용** tool factory이다. 그 테스트는
실제 MCP SDK tool dispatcher, Responses host, MAF를 결정적 모델
fixture와 함께 사용한다. 실제 Copilot client나 클라우드 agent는 사용하지 않는다. 각 호출은
`store=False`인 단일 턴이다. 호출자가 지정한 conversation/response ID가 없고,
repo/history/파일을 자동으로 전달하지 않는다. 입력 상한은 4 KiB, 요청은 64 토큰,
응답 상한은 64 KiB이며, 동시에 처리하는 호출은 하나이다. 이는 로컬 계약 한도이며,
배포된 서버 측 할당량이 아니다. 확인 인자는 사람의 동의를
증명하지 않는다. 실제 Copilot tool 승인과 정확한 텍스트 위임을 활성화해야 한다.

클라우드에서 사용하기 전에, 로컬 전용 제한을 명시적으로 승인된
고정 HTTPS agent 엔드포인트와 Entra audience/tenant 인증으로 교체한다. 최초 GitHub 테스트에는
사용자가 승인한 identity를 쓰는 로컬 stdio wrapper를 우선한다.
토큰을 내장하거나, 데모/GPU 자격 증명을 공유하거나, 인증되지 않은
원격 MCP listener를 노출하지 않는다. 원격 wrapper에는 별도의 OAuth/MCP 권한 부여,
최소 권한 agent 접근, 신뢰할 수 있는 호출자 격리가 필요하다. channel
대화는 기본적으로 분리한다. client 간 history 공유는 승인되지 않았다.

로컬 취소는 Responses HTTP transport를 닫는다. 이것이 연결 끊김 시
Hosted/GPU 취소를 증명하지는 **않는다**. 긴 요청을 활성화하기 전에
MCP 취소 -> 소유한 Responses response 취소 -> A.X relay/GPU 중단을 검증한다.
이때 신뢰할 수 있는 호출자에 바인딩된 response ID와 제한된 cleanup을 사용한다. 이 실험은
임의의 response ID를 취소하는 tool을 노출하지 않는다.

[GitHub의 MCP 문서](https://docs.github.com/en/copilot/concepts/context/mcp)는
CLI/app/IDE tool을 지원한다. 회사 Copilot Business/Enterprise의 경우,
조직의 **MCP servers in Copilot** 정책이 사용을 허용해야 한다. 실제 정책,
client 등록, 앱 설치, 모든 클라우드 권한은 아직
검증되지 않았으며 개별 승인이 필요하다. 이 이전 통합 스케치는
회사 channel 등록을 승인하지 않는다. 실험 A는 기존
AX tenant에서 호스팅한다. 실험 B는 별도로 승인된 회사 channel bridge를 담당한다.
