# 모델 카드 Thinking Mode 표 + A100 측정값

| Domain | Benchmark | A.X K2 (SKT) | A.X K2 on A100 x16 (ours) |
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

Not run: scope = outside this verification's scope (AIME26, KoBALT, CLIcK, IFBench and NIAH were chosen); gated = the dataset is gated (access approval needed), so it was excluded; external = scored by Artificial Analysis with its own harness; not reproducible here; sandbox = needs a code-execution or web-search environment that the demo cluster does not provide; unpublished = the problem set is not published as a dataset
A100 run: `ev20261006-095528`. ± is the 95% confidence half-width over items.
n=done/planned: the run was stopped early; scores are over the items finished so far (a random sample of each suite), so treat them as indicative.
NIAH: from the earlier verification run of the same serving layout (SKT's niah_test.py, thinking off, greedy).
