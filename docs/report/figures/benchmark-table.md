# 모델 카드 Thinking Mode 표 + A100 측정값

| 분야 | 벤치마크 | A.X K2 (SKT 공개값) | A.X K2, A100 16장 (이번 측정) |
| --- | --- | ---: | ---: |
| Math | AIME26 | 97.1 | **100.0** (n=10/60 gen) |
| Math | Apex | 45.8 | – (scope) |
| Math | Apex-shortlist | 88.6 | – (scope) |
| Math | KMO26 (1st round) | 92.5 | – (unpublished) |
| Korean | KMMLU-Pro | 80.5 | – (gated) |
| Korean | KoBALT | 73 | **74.3** ± 8.2 (n=109/200) |
| Korean | CLIcK | 91.6 | **85.5** ± 6.1 (n=131/200) |
| Code | LiveCodeBench v6 (Feb-May) | 84 | – (sandbox) |
| Code | SciCode | 41 | – (sandbox) |
| Code | Terminal Bench v2.1* | 36 | – (external) |
| Science & Knowledge | Humanity's Last Exam | 27.8 | – (gated) |
| Science & Knowledge | GPQA Diamond | 85.6 | – (gated) |
| Science & Knowledge | AA-Omniscience* | 39.6 | – (external) |
| General | IFBench | 75.9 | **78.6** ± 7.6 (n=112/200) |
| General | AA-LCR | 66 | – (scope) |
| Agentic | GDPval* (Elo) | 1031 | – (external) |
| Agentic | tau2-Bench* (Telecom) | 98 | – (external) |
| Agentic | tau3-Bench* (Banking) | 13 | – (external) |
| Agentic | BrowseComp (<=10 searches) | 9.3 | – (sandbox) |
| Long context | NIAH | 100 | **100.0** (9/9, 32K, 128K, 256K) |

실행하지 않은 이유: scope = 이번 검증 범위 밖(AIME26, KoBALT, CLIcK, IFBench, NIAH만 골랐다); gated = 접근 승인이 필요한 데이터셋이라 제외했다; external = Artificial Analysis가 자체 도구로 채점한 값이라 여기서 재현할 수 없다; sandbox = 코드 실행이나 웹 검색 환경이 필요한데 데모 클러스터에는 없다; unpublished = 문제 세트가 데이터셋으로 공개되지 않았다
A100 실행: `ev20261006-095528`. ±는 문항 기준 95% 신뢰구간의 절반 폭이다.
n=완료/계획: 실행을 중간에 멈췄다. 점수는 그때까지 끝난 문항(각 세트에서 무작위로 뽑은 문항)만으로 계산했으므로 참고치로만 본다.
NIAH: 같은 서빙 구성으로 먼저 돌린 검증 실행의 값이다(SKT niah_test.py, thinking 끔, 그리디).
