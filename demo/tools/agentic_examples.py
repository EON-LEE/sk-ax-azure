"""Candidate prompts for the demo's agent examples, what a good run of each looks like, and the synthetic files two of
them need (standard library only).

Everything here is made up for demos: the contract parties, prices, traffic numbers and the buggy script. No real
customer document, host or account appears in this file.

The server forces a tool in round 1 only when the prompt asks for one by name or by a search/file keyword
(agent.requested_tool). The wording below is chosen so that the forced first tool is the step the scenario starts with:
"계산기" is used only when the calculator is step 1, and a web search is always asked for with "웹 검색" or "Web IQ".
A turn has at most 5 model calls and no tools in the last one, so a scenario needs at most 4 tool rounds.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Example:
    """One candidate. The check fields are what check_agentic_examples.py holds a run to.

    forced     tool the server forces in round 1 for this prompt (None: the model chooses)
    tools      tools that must succeed, in this order (other calls are allowed in between)
    any_of     groups of tools of which at least one must succeed
    args       (tool, regex): some successful call of the tool has arguments matching the regex
    results    (tool, regex): some successful call of the tool returned text matching the regex
    artifacts  artifact kinds a successful tool must have returned (preview, chart)
    files      files the turn must have written (names, any folder)
    preview    HTML file that must open through the preview route
    contains   (answer or file name, regex): the text must match; thousands separators are removed first
    sources    distinct links the answer and written files must cite; every cited link must come from a tool result
    hosts      at least one cited link must be on one of these hosts
    inputs     every number given to the calculator comes from the prompt or an earlier tool result
    shown      the last calculator result appears (rounded) in the answer or a written file
    upload     fixture uploaded and attached before the turn
    changes    uploaded file the turn must have rewritten
    external   needs Web IQ
    calls      planned model calls (at most 5)
    """

    id: str
    label: str
    group: str
    prompt: str
    forced: str | None = None
    tools: tuple[str, ...] = ()
    any_of: tuple[tuple[str, ...], ...] = ()
    args: tuple[tuple[str, str], ...] = ()
    results: tuple[tuple[str, str], ...] = ()
    artifacts: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    preview: str = ""
    contains: tuple[tuple[str, str], ...] = ()
    sources: int = 0
    hosts: tuple[str, ...] = ()
    inputs: bool = False
    shown: bool = False
    upload: str = ""
    changes: str = ""
    external: bool = False
    calls: int = 2
    risk: str = ""


EXAMPLES = (
    Example(
        id="time-zones", label="세 도시 현재 시각", group="quick",
        prompt="서울(Asia/Seoul)·뉴욕(America/New_York)·런던(Europe/London)의 현재 시각을 get_current_time 도구로 "
               "각각 확인해서 한 줄씩 알려 주고, 세 도시가 함께 화상회의를 하기에 덜 불편한 시간대를 한 문장으로 "
               "제안해 줘.",
        tools=("get_current_time",) * 3,
        args=(("get_current_time", "America/New_York"), ("get_current_time", "Europe/London")),
        calls=2, risk="Asia/Seoul은 기본값이라 인자가 비어 있을 수 있음, 서버의 시간대 데이터가 없으면 도구 오류"),
    Example(
        id="plan-fee", label="요금제 총액 계산", group="quick",
        prompt="월 69000원 요금제를 24개월 쓰고 25% 할인을 받으면 총 얼마인지 계산기로 정확히 구하고, "
               "할인 전 금액과의 차이도 알려 줘.",
        forced="calculator", tools=("calculator",), shown=True,
        contains=(("answer", r"1242000|124만\s*2천"), ("answer", r"414000|41만\s*4천")),
        calls=2),
    Example(
        id="code-baseline", label="도구 없이 코드 작성", group="quick",
        prompt="파이썬으로 월별 데이터 사용량(MB) 목록을 받아 합계와 일 평균을 돌려주는 함수를 작성하고, "
               "사용 예시와 시간 복잡도를 짧게 설명해 줘.",
        contains=(("answer", r"def\s+\w+\("),),
        calls=1, risk="도구 없는 기준선: 모델 호출 1번"),
    Example(
        id="ai-chip-news", label="AI 반도체 뉴스 브리핑", group="agent",
        prompt="웹 검색으로 AI 반도체 관련 최신 뉴스 3건을 찾아 제목·언론사·날짜를 briefing.html 표로 만들어 저장하고 "
               "미리보기로 열어 줘. CSS·스크립트·링크·이미지 없이 h1과 table만 쓰고, 답변에는 기사 3건의 실제 출처 "
               "URL을 적어 줘.",
        forced="web_iq_news", tools=("web_iq_news", "write_file", "preview_html"),
        files=("briefing.html",), preview="briefing.html", artifacts=("preview",),
        contains=(("briefing.html", r"(?:<tr\b[\s\S]*?){3}"),),
        sources=3, external=True, calls=4, risk="HTML 출력 길이, 뉴스가 3건 미만일 수 있음"),
    Example(
        id="chip-price-ratio", label="반도체 두 종목 시세 비율", group="agent",
        prompt="Web IQ로 SK하이닉스(000660)와 삼성전자(005930)의 주가를 실제 조회하고, 두 현재가의 "
               "비율(SK하이닉스 ÷ 삼성전자)을 정확히 계산해 줘. 시세 기준 시각·통화·지연 여부와 출처 URL을 함께 적고, "
               "제공되지 않은 값은 '미확인'으로 표시해 줘. 투자 조언은 하지 마.",
        forced="web_iq_finance", tools=("web_iq_finance", "calculator"),
        results=(("web_iq_finance", r"000660"), ("web_iq_finance", r"005930")),
        contains=(("answer", r"지연|실시간|미확인"),),
        sources=1, inputs=True, shown=True, external=True, calls=4,
        risk="종목마다 조회 1번씩이라 모델 호출 4번"),
    Example(
        id="axk2-public-research", label="A.X K2 공개 자료 조사", group="agent",
        prompt="웹 검색으로 'A.X K2' 공개 자료를 찾고, 가장 관련 있는 페이지 1개를 browse로 읽어 핵심 3가지를 요약해 줘. "
               "마지막에 출처 표(제목·URL)를 붙이고, 읽지 못한 내용은 추측하지 말고 '미확인'이라고 적어 줘.",
        forced="web_iq_web", tools=("web_iq_web", "web_iq_browse"),
        sources=1, external=True, calls=3, risk="검색 색인에 자료가 얇거나 browse가 거부될 수 있음"),
    Example(
        id="foundry-byom-steps", label="Foundry BYOM 연결 방법", group="agent",
        prompt="웹 검색으로 Microsoft Learn에서 Foundry에 직접 만든 모델(BYOM)을 연결하는 방법 문서를 찾고, 가장 관련 있는 "
               "문서 1개를 browse로 읽어 연결 단계를 번호 목록으로 요약해 줘. 주소에 물음표(?)가 있으면 물음표 앞부분만 "
               "사용하고, 읽은 범위에 없는 단계는 추측하지 말고 출처 URL을 마지막에 적어 줘.",
        forced="web_iq_web", tools=("web_iq_web", "web_iq_browse"),
        hosts=("learn.microsoft.com",), sources=1, external=True, calls=3,
        risk="browse는 물음표(?)가 있는 주소를 거부하고 본문을 1500자까지만 돌려줌"),
    Example(
        id="monthly-sales-report", label="월별 매출 리포트", group="agent",
        prompt="먼저 sales.csv 파일을 만들어 월,매출(만원) 열로 1월 1200, 2월 1350, 3월 1280, 4월 1520, 5월 1610, "
               "6월 1700을 저장해 줘. 그다음 매출 합계와 1월 대비 6월 증감률(%)을 정확히 계산해서 report.html에 h1과 "
               "table만 써서 저장하고 미리보기로 열어 줘.",
        forced="write_file", tools=("write_file", "write_file", "preview_html"),
        any_of=(("calculator", "analyze_table"),),
        files=("sales.csv", "report.html"), preview="report.html", artifacts=("preview",),
        contains=(("answer", r"8660"), ("report.html", r"8660"), ("report.html", r"41\.[67]|\b42\s*%")),
        shown=True, calls=4, risk="도구 호출을 한 번에 묶지 않으면 모델 호출 5번째에 미리보기 단계가 빠짐"),
    Example(
        id="contract-review", label="계약서 PDF 조항 점검", group="agent",
        prompt="첨부한 계약서(PDF)에서 해지·위약금·비밀유지 조항을 찾아 조항 번호와 페이지 근거를 붙인 체크리스트로 "
               "정리해 줘. 계약 시작 12개월 뒤에 해지하면 위약금이 얼마인지 정확히 계산해서 contract_check.md에 기록해 줘. "
               "첨부가 없으면 PDF를 첨부해 달라고만 안내해 줘.",
        tools=("read_pdf", "read_pdf", "calculator", "write_file"),
        files=("contract_check.md",),
        contains=(("answer", r"1188000"), ("contract_check.md", r"1188000"),
                  ("contract_check.md", r"제\s*4\s*조"), ("contract_check.md", r"제\s*5\s*조"),
                  ("contract_check.md", r"제\s*6\s*조"),
                  ("contract_check.md", r"(?i)(?:p\.?\s*|page\s*|페이지\s*)[23]|[23]\s*(?:페이지|쪽)")),
        shown=True, upload="contract.pdf", calls=4,
        risk="PDF를 먼저 첨부해야 함, 서버가 첫 쪽만 미리 읽으므로 나머지 쪽은 모델이 다시 읽어야 함"),
    Example(
        id="pangyo-lunch-plan", label="판교역 점심 일정", group="agent",
        prompt="Web IQ 장소 도구로 판교역 근처 점심 식당 두 곳을 실제로 검색하고, get_current_time으로 오늘 날짜를 확인해 "
               "오늘 12시 30분 점심 일정을 lunch_plan.md에 기록해 줘. 식당 이름·주소·출처 URL을 함께 넣어 줘.",
        forced="web_iq_places", tools=("web_iq_places", "get_current_time", "write_file"),
        files=("lunch_plan.md",), contains=(("lunch_plan.md", r"12\s*[:시]\s*30"),),
        sources=1, external=True, calls=4, risk="장소 색인의 판교 커버리지"),
    Example(
        id="code-fix-diff", label="코드 버그 수정과 diff", group="agent",
        prompt="첨부한 discount.py를 읽고 버그를 찾아 고친 뒤 같은 이름으로 저장하고, 원본과의 diff를 보여 줘. "
               "고친 이유를 한 줄씩 설명해 줘.",
        forced="read_file", tools=("read_file", "write_file", "diff_file"),
        results=(("diff_file", r"(?m)^[+-][^+-]"),),
        contains=(("discount.py", r"1(?:\.0)?\s*-\s*rate|price\s*-\s*\(?\s*price\s*\*\s*rate"),
                  ("discount.py", r"if\s+(?:not\s+)?values|not\s+len\(|len\(\s*values\s*\)\s*(?:==|!=|>|<)|"
                                  r"\belse\s+0\b|\bor\s+0\b|except\s+ZeroDivisionError")),
        upload="discount.py", changes="discount.py", calls=4,
        risk="diff_file은 업로드 원본과만 비교하므로 파일을 먼저 첨부해야 함"),
    Example(
        id="csv-chart", label="CSV 합계와 막대 차트", group="agent",
        prompt="월별 모바일 데이터 사용량(TB)을 1월 410, 2월 395, 3월 430, 4월 465, 5월 500, 6월 520으로 traffic.csv "
               "파일에 저장하고, 합계와 월 평균을 정확히 계산한 뒤 막대 차트로 보여 줘. 시연용 가상 데이터라고 표시해 줘.",
        forced="write_file", tools=("write_file", "chart_table"),
        any_of=(("analyze_table", "calculator"),),
        files=("traffic.csv",), artifacts=("chart",),
        contains=(("answer", r"2720"), ("answer", r"453"), ("answer", r"가상|시연용|예시")),
        calls=4, risk="차트 열 이름이 CSV 머리글과 정확히 같아야 함"),
)

CONTRACT_PAGES = (
    ("서비스 이용 계약서 (시연용 가상 문서)",
     "",
     "이 문서는 시연을 위해 만든 가상의 계약서이며 실제 계약이 아닙니다.",
     "",
     "제1조 (당사자) 갑은 가나다통신 주식회사, 을은 라마바소프트 주식회사로 한다.",
     "제2조 (계약 기간) 계약 기간은 시작일부터 24개월로 한다.",
     "제3조 (이용료) 월 이용료는 330000원이며 매월 말일에 청구한다."),
    ("제4조 (해지) 갑은 계약 기간 중 30일 전에 서면으로 알리면",
     "계약을 해지할 수 있다.",
     "",
     "제5조 (위약금) 계약 기간 중에 갑이 해지하면 아래 금액을",
     "위약금으로 낸다.",
     "위약금 = 남은 계약 개월 수 × 월 이용료 × 30%",
     "남은 계약 개월 수는 24개월에서 이미 이용한 개월 수를 뺀 값이다."),
    ("제6조 (비밀유지) 양 당사자는 계약으로 알게 된 상대방의 비밀을",
     "계약이 끝난 뒤에도 3년간 지킨다.",
     "",
     "제7조 (책임 제한) 을의 손해배상 책임은 최근 12개월 동안 받은",
     "이용료의 합계를 넘지 않는다."),
)

DISCOUNT_LINES = (
    '"""통신 요금 할인 계산 (시연용 가상 코드)."""',
    "",
    "",
    "def discounted_total(prices, rate):",
    '    """할인율 rate(0~1)를 적용한 결제 금액의 합계를 돌려준다.',
    "",
    "    예: discounted_total([1000, 2000], 0.1) 은 2700 이어야 한다.",
    '    """',
    "    total = 0",
    "    for price in prices:",
    "        total += price * rate",
    "    return total",
    "",
    "",
    "def average(values):",
    '    """값의 평균을 돌려준다. 값이 하나도 없으면 0을 돌려준다."""',
    "    return sum(values) / len(values)",
    "",
)


def pdf_hex(text):
    return text.encode("utf-16-be").hex().upper().encode("ascii")


def pdf_stream(data):
    return b"<< /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


def to_unicode_map(chars):
    entries = ["<%04X> <%04X>" % (ord(c), ord(c)) for c in sorted(set(chars))]
    blocks = "".join("%d beginbfchar\n%s\nendbfchar\n" % (len(part), "\n".join(part))
                     for part in (entries[i:i + 100] for i in range(0, len(entries), 100)))
    return ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n" + blocks +
            "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend").encode("ascii")


def page_stream(lines, heading):
    commands = []
    for index, line in enumerate(lines):
        if line:
            size = 16 if heading and index == 0 else 11
            commands.append(b"BT /F1 %d Tf 48 %d Td <%s> Tj ET" % (size, 790 - index * 24, pdf_hex(line)))
    return b"\n".join(commands)


def assemble(objects):
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    table = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, table)
    return bytes(out)


def contract_pdf():
    """A 3-page contract with a text layer. The Korean font is not embedded: readers draw the text with a system
    font, and text extraction (all the demo needs) goes through the ToUnicode map."""
    count = len(CONTRACT_PAGES)
    kids = " ".join("%d 0 R" % (6 + 2 * i) for i in range(count))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        ("<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, count)).encode("ascii"),
        b"<< /Type /Font /Subtype /Type0 /BaseFont /HYSMyeongJo-Medium /Encoding /UniKS-UCS2-H "
        b"/DescendantFonts [4 0 R] /ToUnicode 5 0 R >>",
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /HYSMyeongJo-Medium /DW 1000 "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Korea1) /Supplement 2 >> >>",
        pdf_stream(to_unicode_map("".join("".join(page) for page in CONTRACT_PAGES))),
    ]
    for number, lines in enumerate(CONTRACT_PAGES):
        objects.append(("<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
                        "/Contents %d 0 R >>" % (7 + 2 * number)).encode("ascii"))
        objects.append(pdf_stream(page_stream(lines, number == 0)))
    return assemble(objects)


def discount_py():
    return "\n".join(DISCOUNT_LINES).encode("utf-8")


FIXTURES = {"contract.pdf": contract_pdf, "discount.py": discount_py}
