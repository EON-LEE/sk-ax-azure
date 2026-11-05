"""Slides 2-4: background, architecture, kernel swaps."""
from common import *
import data as D

GREY2 = "#9AA5B1"


def s02():
    fig, ax = slide(
        "A100은 H100의 3분의 1 가격, 대신 메모리가 모자라다",
        "같은 8GPU 노드 기준 시간당 소매가와, 모델 가중치가 노드 메모리에 들어가는지 비교",
        "가격: Azure 소매가(retail), 영국 남부, 2026-11-02 조회. Spot·예약 요금은 제외. 메모리: 가중치만 계산했고 KV 캐시와 활성값은 별도. "
        "출처: tables/t08_prices_memory.csv, evidence/a100-nvfp4-single-node.json",
    )
    label(ax, 4, 45.0, "시간당 가격 (USD, 노드 기준)", 15, INK, True)
    axp = panel(fig, 6, 24, 38, 18.5)
    grouped_bars(axp, ["노드 1대", "노드 2대"],
                 [("A100", [40.962, 81.924], NAVY), ("H100", [122.90, 245.80], GREY2)], fmt="${:,.2f}", ymax=300, fs_val=12.5, fs_tick=13.5)
    axp.set_yticks([0, 100, 200, 300])
    axp.text(-0.42, 288, "A100은 H100의 33%\n(노드 1대·2대 모두)", ha="left", va="top", fontsize=13.5, color=NAVY, fontweight="bold")
    legend_row(ax, [("A100 (ND96amsr_A100_v4) 사용", NAVY), ("H100 (ND96isr_H100_v5) 미사용", GREY2)], 4.5, 20.4, fs=12)

    label(ax, 50, 45.0, "가중치 크기와 노드 GPU 메모리 (GB)", 15, INK, True)
    axm = panel(fig, 58, 24, 38, 18.5)
    axm.yaxis.grid(False)
    axm.xaxis.grid(True, color=LIGHT, lw=1.2)
    axm.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    fp8 = D.NV["checkpoint"]["fp8_checkpoint_size_gb"]
    nvf = D.NV["checkpoint"]["size_gb"]
    axm.barh([1, 0], [fp8, nvf], 0.5, color=[NAVY, GREEN], zorder=3)
    axm.set_yticks([1, 0])
    axm.set_yticklabels(["FP8\n체크포인트", "NVFP4\n체크포인트"], fontsize=13)
    axm.set_xlim(0, 1400)
    axm.set_ylim(-0.55, 1.95)
    axm.set_xticks([0, 400, 800, 1200])
    for x, t in ((640, "노드 1대\n640 GB"), (1280, "노드 2대\n1,280 GB")):
        axm.axvline(x, ymax=0.8, color=VERM, ls="--", lw=2, zorder=4)
        axm.text(x, 1.5, t, color=VERM, ha="center", va="bottom", fontsize=12, fontweight="bold")
    white = dict(fc="white", ec="none", pad=1.5)
    axm.text(fp8 / 2, 1, f"{fp8:,.0f} GB", ha="center", va="center", color="white", fontsize=13, fontweight="bold", zorder=5)
    axm.text(fp8 + 30, 1, "노드 1대에\n안 들어감", ha="left", va="center", color=VERM, fontsize=12.5, fontweight="bold", zorder=5, bbox=white)
    axm.text(nvf / 2, 0, f"{nvf:,.0f} GB", ha="center", va="center", color="white", fontsize=13, fontweight="bold", zorder=5)
    axm.text(nvf + 30, 0, "노드 1대에\n들어감", ha="left", va="center", color=GREEN, fontsize=12.5, fontweight="bold", zorder=5, bbox=white)

    w, gap, y0, h = 30.0, 2.0, 6.2, 11.2
    card(ax, 3, y0, w, h, "① 메모리가 모자라다",
         "FP8 체크포인트 694 GB가 노드 1대의 640 GB보다 크다. 노드 2대에 층을 나눠 올린다 (파이프라인 병렬).", ec=NAVY, fs=12.5)
    card(ax, 3 + w + gap, y0, w, h, "② FP8 텐서코어가 없다",
         "A100(SM80)은 FP8 연산기가 없다. FP8 가중치를 커널 안에서 펼쳐 BF16 활성값과 계산한다 (Marlin W8A16).", ec=NAVY, fs=12.5)
    card(ax, 3 + 2 * (w + gap), y0, w, h, "③ DSA 커널이 SM90·SM100 전용",
         "희소 어텐션 커널이 A100에서 돌지 않는다. 인덱서와 희소 MLA를 Triton 포트로 바꿨다 (vLLM PR #38476, 미병합).", ec=NAVY, fs=12.5)
    save(fig, "s02_price-and-memory.png")


def s03():
    fig, ax = slide(
        "검증한 구조: Foundry → 게이트웨이 → 웹앱 → GPU",
        "Foundry 카탈로그 배포는 시험하지 않았고, BYOM 연결로 호출한 경로만 검증했다",
        "GPU 리전은 실험마다 영국 남부 또는 이탈리아 북부, 웹앱은 Korea Central. 출처: docs/SERVING_DESIGN.md §3, §8.6. 실제 화면 캡처: s03_demo-chat-thinking.png, s03_demo-chat-tools.png",
    )
    y, h = 36.0, 10.0
    box(ax, 3, y, 14, h, "Foundry 에이전트\n(BYOM 연결)", fc=PALE_BLUE, ec=BLUE, fs=13, bold=False)
    box(ax, 20, y, 17, h, "API Management\n게이트웨이\n키 확인 후 전달", fc=PALE_BLUE, ec=BLUE, fs=13)
    box(ax, 40, y, 22, h, "웹앱 모델 API\nApp Service B1\nAlways On · Korea Central", fc=PALE_BLUE, ec=BLUE, fs=13)
    arrow(ax, 17.1, y + h / 2, 19.9, y + h / 2, BLUE)
    arrow(ax, 37.1, y + h / 2, 39.9, y + h / 2, BLUE)

    gx, gw = 72.0, 25.0
    box(ax, gx, 6.2, gw, 40.3, "", fc="#F7F9FB", ec=NAVY, lw=2.0)
    label(ax, gx + 1.2, 44.3, "GPU 쪽: AML 명령 작업", 13.5, NAVY, True, max_w=gw - 2)
    label(ax, gx + 1.2, 42.0, "low-priority ND96amsr_A100_v4 ×2", 11.5, GREY, max_w=gw - 2)
    box(ax, gx + 2, 28.2, gw - 4, 11.2, "노드 0 · Ray head\nvLLM API + PP 단계 0\n층 0–30 · TP8 (NVLink)", fc=PALE_GREEN, ec=GREEN, fs=12)
    box(ax, gx + 2, 14.2, gw - 4, 11.2, "노드 1 · Ray worker\nPP 단계 1\n층 31–60 + LM head · TP8", fc=PALE_GREEN, ec=GREEN, fs=12)
    arrow(ax, gx + 6.5, 28.1, gx + 6.5, 25.5, NAVY, style="<|-|>", ms=14)
    label(ax, gx + 8.2, 26.8, "은닉 상태 · IB", 11.5, NAVY)
    label(ax, gx + 1.2, 11.6, "NVFP4는 노드 0 한 대만 사용 (TP8)", 11, GREY, max_w=gw - 2)
    label(ax, gx + 1.2, 9.4, "가중치: 시작할 때 비공개 Blob에서 내려받음", 11, GREY, max_w=gw - 2)
    arrow(ax, gx, 41.0, 62.2, 41.0, VERM, ls="--", lw=2.2)
    label(ax, 62.6, 44.0, "역방향 링크", 11.5, VERM, True)
    label(ax, 62.6, 38.6, "GPU가 먼저 연결", 11, VERM)

    cw = 28.5
    card(ax, 3, 22.0, cw, 11.2, "FP8 프로필",
         "경로 /v1\n모델 ax-k2-fp8 (별칭 skt/A.X-K2)\nGPU 2노드 · TP8×PP2", ec=NAVY, fs=12.5)
    card(ax, 3 + cw + 2, 22.0, cw, 11.2, "NVFP4 프로필",
         "경로 /models/nvfp4/v1\n모델 ax-k2-nvfp4\nGPU 1노드 · TP8", ec=NAVY, fs=12.5)
    card(ax, 3, 6.2, cw, 13.8, "시험 범위: BYOM 호출만",
         "FP8·NVFP4 두 프로필 모두 일반 채팅, 함수 도구, MCP 도구 호출을 시험했다. Foundry 카탈로그 배포는 시험하지 않았다.",
         ec=RED, fc=PALE_RED, ls="--", fs=12.5, hc=RED)
    card(ax, 3 + cw + 2, 6.2, cw, 13.8, "운영 중 관찰 (2026-10-19~20)",
         "약 16시간 동안 GPU 역방향 링크가 8번(6건) 끊겼고, 복구까지 약 32~116분 걸렸다. 원인은 Spot 회수로 추정하나 확인하지 못했다.",
         ec=GREY, fs=12.5, hc=GREY)
    save(fig, "s03_architecture.png")


def s04():
    fig, ax = slide(
        "엔진과 커널: 공식 구현이 아니라 포트로 돌린다",
        "SKT fork의 vLLM 0.23 기반. A100에 없는 FP8·FP4 연산과 SM90 전용 커널을 바꿔 끼웠다",
        "공식 구현이 아닌 포트이며, 병목 위치는 프로파일링하지 않았다. 속도 차이의 원인은 구성에서 추론한 것이지 측정값이 아니다. 출처: docs/SERVING_DESIGN.md §5c, §8.4",
    )
    cols = [("구성요소", 14, "l"), ("공식 기준 (B300급 1노드)", 21, "l"), ("이번 A100 배치", 34, "l"), ("영향", 25, "l")]
    b = {"b": True}
    rows = [
        [("행렬곱\n(선형·MoE)", b), "FP8×FP8, FP8 텐서코어 (블록 스케일 W8A8)",
         "FP8 가중치를 커널 안에서 펼치고 BF16 활성값으로 계산 (Marlin W8A16)",
         "가중치 메모리는 같다. 연산이 병목이면 느리다. 비트 단위로는 같지 않다"],
        [("인덱서 점수", b), "DeepGEMM FP8 MQA logits",
         "Triton 커널. 같은 FP8 q/k, BF16 내적, FP32 누적 (PR #38476)", "같은 입력, 같은 규칙, 다른 커널"],
        [("희소 MLA\n(키 2,048개 선택)", b), "FlashMLA / FlashInfer 희소",
         "Triton 희소 MLA (PR #38476, upstream 미병합)", "같은 계산. 더 느리다. upstream 지원이 아니다"],
        [("KV 캐시", b), "BF16 기본, FP8 선택 가능", "BF16만 사용",
         "FP8 KV로 줄이지 못한다. TP 랭크마다 복제되어 A100 16장에 673,792 토큰"],
        [("NVFP4 전문가", b), "W4A4, FP4 텐서코어 (B200)",
         "Marlin W4A16 (vLLM #45306 전체 + #45295의 한 줄)", "4비트 가중치는 그대로, 활성값은 BF16"],
        [("엔진 코드", b), "vLLM 0.23 + SKT fork",
         "같음 + PR 포트 + mla_attention.py 2줄 수정", ("수정이 없으면 A100에서 서버가 죽는다", {"c": RED, "b": True})],
    ]
    bottom = table(ax, 3, 46.8, cols, rows, fs=12.5, hfs=13, pady=0.85)
    label(ax, 3.2, bottom - 1.7, "가중치 저장 형식(FP8·NVFP4)은 그대로이고, 바뀐 것은 계산 방식이다. 시작할 때 apply_overlay.py가 포트를 적용하고 SHA-256을 검사한다.", 12.5, INK, max_w=94)
    label(ax, 3.2, bottom - 3.6, "NVFP4 포트(vLLM #45306, #45295)와 같은 변경은 vLLM 0.24.0 이상에 이미 들어 있다. 이번에 쓴 SKT fork는 0.23.0이다.", 12.5, GREY, max_w=94)
    save(fig, "s04_kernel-swaps.png")


ALL = [s02, s03, s04]
