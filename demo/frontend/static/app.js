"use strict";
// The demo page shell: login, tabs, the /api/status poll (power badge, banner, cluster tab) and the results tab.
(function () {
  const AX = window.AX;
  const el = AX.el;
  const $ = (id) => document.getElementById(id);
  const state = { status: null, previous: null, rate: null, results: null, loaded: false, poll: null };

  // ------------------------------------------------------------------------------------------- login
  async function checkLogin() {
    let me = { demo: false };
    try {
      me = await AX.api("/api/me");
    } catch (error) {
      console.warn(error);
    }
    AX.show($("login"), !me.demo);
    if (me.demo) start();
    else $("login-password").focus();
  }

  function locked() {
    AX.show($("login"), true);
    $("login-error").textContent = "로그인이 만료되었습니다. 비밀번호를 다시 입력해 주세요.";
    $("login-password").focus();
  }

  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = $("login-form").querySelector("button");
    button.disabled = true;
    $("login-error").textContent = "";
    try {
      await AX.api("/api/login", { body: { password: $("login-password").value } });
      $("login-password").value = "";
      AX.show($("login"), false);
      start();
    } catch (error) {
      $("login-error").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  $("logout").addEventListener("click", async () => {
    try {
      await AX.api("/api/logout", { body: {} });
    } finally {
      AX.chat.reset();
      AX.show($("login"), true);
      $("login-error").textContent = "";
    }
  });

  window.addEventListener("ax:unauthorized", locked);

  // -------------------------------------------------------------------------------------------- tabs
  function selectTab(name) {
    for (const button of AX.$$("[data-tab]")) {
      const on = button.dataset.tab === name;
      button.setAttribute("aria-selected", String(on));
      AX.show($(`tab-${button.dataset.tab}`), on);
    }
    if (name === "results" && !state.loaded) loadResults();
    if (location.hash.slice(1) !== name) history.replaceState(null, "", `#${name}`);
  }
  for (const button of AX.$$("[data-tab]")) button.addEventListener("click", () => selectTab(button.dataset.tab));

  // ------------------------------------------------------------------------------------------ status
  function start() {
    if (state.poll) {
      state.poll();
      return;
    }
    state.poll = AX.every(5, poll);
    const tab = location.hash.slice(1);
    if (["chat", "cluster", "results", "about"].includes(tab)) selectTab(tab);
  }

  async function poll() {
    if (!$("login").classList.contains("hidden")) return;
    let status;
    try {
      status = await AX.api("/api/status");
    } catch (error) {
      if (error.status === 401) locked();
      else showBanner(error.message, true);
      return;
    }
    rate(status);
    state.status = status;
    renderPower(status);
    renderCluster(status);
  }

  // Server-wide generation speed from the counters of two consecutive samples.
  function rate(status) {
    const now = status.metrics && status.sampled ? { t: status.sampled, m: status.metrics } : null;
    const before = state.previous;
    if (now && before && now.t > before.t) {
      const seconds = now.t - before.t;
      const gen = now.m.generation_tokens - before.m.generation_tokens;
      const prompt = now.m.prompt_tokens - before.m.prompt_tokens;
      state.rate = gen >= 0 && prompt >= 0 ? { generation: gen / seconds, prompt: prompt / seconds } : null;
    } else if (!now) {
      state.rate = null;
    }
    if (now && (!before || now.t !== before.t)) state.previous = now;
  }

  function showBanner(text, error) {
    const banner = $("banner");
    banner.textContent = text || "";
    banner.classList.toggle("error", Boolean(error));
    AX.show(banner, Boolean(text));
  }

  function phaseText(status) {
    const phase = AX.PHASES[status.phase] || status.phase || "";
    if (status.phase === "download" && status.download_gb) {
      return `${phase} ${AX.num(status.download_gb)}${status.download_of_gb ? ` / ${AX.num(status.download_of_gb)}` : ""} GB`;
    }
    if (status.phase === "loading" && status.load_step) {
      return `${AX.loadStep(status.load_step)} ${AX.num(status.load_pct)}%`;
    }
    return phase;
  }

  function renderPower(status) {
    const badge = $("power");
    badge.textContent = AX.POWER[status.power] || status.power;
    badge.className = `badge ${AX.powerClass(status.power)}`;
    AX.chat.setPower(status.power);
    const queue = status.queue || {};
    let text = "";
    if (status.power === "off") {
      text = "지금은 GPU 클러스터가 꺼져 있습니다. 관리자가 켜면 대화할 수 있습니다 (켜는 데 수십 분이 걸립니다).";
    } else if (status.power === "stopping") {
      text = "GPU 클러스터를 끄는 중입니다.";
    } else if (status.power === "waiting_capacity") {
      text = "Azure에서 A100 노드 2대를 확보하는 중입니다. 저우선순위 VM이라 GPU 여유가 생길 때까지 기다릴 수 있습니다.";
    } else if (status.power === "starting" || status.power === "booting") {
      text = `GPU 클러스터를 준비하는 중입니다${status.phase ? `: ${phaseText(status)}` : ""}. 가중치를 내려받아 GPU에 올리는 데 수십 분이 걸립니다.`;
    } else if (queue.waiting > 0) {
      text = `사용자가 많아 ${AX.num(queue.waiting)}건이 대기 중입니다. 보내면 순서대로 처리합니다.`;
    }
    showBanner(text, false);
  }

  // ----------------------------------------------------------------------------------------- cluster
  function renderCluster(status) {
    const queue = status.queue || {};
    AX.kv($("state"), [
      ["상태", el("span", { class: `badge ${AX.powerClass(status.power)}`, text: AX.POWER[status.power] || status.power })],
      ["리전", AX.region(status.region)],
      ["단계", status.phase ? `${phaseText(status)}${status.phase_since ? ` (${AX.ago(status.phase_since)})` : ""}` : "—"],
      status.note ? ["메모", status.note] : null,
      ["켜진 시간", status.active_since ? AX.duration(status.t - status.active_since) : "—"],
      ["노드", `${AX.num(status.nodes_per_job)}대 × A100 80GB 8장 (ND96amsr_A100_v4)`],
      ["처리 중 / 대기", `${AX.num(queue.active)} / ${AX.num(queue.waiting)} (최대 ${AX.num(queue.max_active)}건 동시, 대기 ${AX.num(queue.max_queue)}건)`],
      status.eval && status.eval.run ? ["평가 실행", `${status.eval.run} (${evalState(status.eval.state)})`] : null,
    ]);
    const progress = $("progress");
    if (status.phase === "download" && status.download_gb && status.download_of_gb) {
      progress.replaceChildren(AX.bar(status.download_gb / status.download_of_gb,
        `가중치 내려받기 ${AX.pct(status.download_gb / status.download_of_gb, 0)}`));
    } else if (status.phase === "loading" && status.load_pct !== null && status.load_pct !== undefined) {
      progress.replaceChildren(AX.bar(status.load_pct / 100, `${AX.loadStep(status.load_step)} ${AX.num(status.load_pct)}%`));
    } else {
      progress.replaceChildren();
    }

    const m = status.metrics;
    const r = state.rate;
    AX.kv($("metrics"), m ? [
      ["생성 중인 요청", AX.num(m.running)],
      ["엔진 대기 요청", AX.num(m.waiting)],
      ["KV 캐시 사용률", el("div", null, AX.bar(m.kv, AX.pct(m.kv)))],
      ["생성 속도", r ? `${AX.num(r.generation, 1)} 토큰/초` : "측정 중"],
      ["입력 처리 속도", r ? `${AX.num(r.prompt, 1)} 토큰/초` : "측정 중"],
      ["누적 선점(preemption)", AX.num(m.preemptions)],
      ["측정 시각", AX.time(status.sampled)],
    ] : [["지표", status.power === "ready" ? "읽는 중" : "GPU 클러스터가 켜지면 표시됩니다"]]);

    AX.table($("regions"), ["리전", "할당된 노드", "사용 가능", "작업 상태", "서빙"],
      Object.entries(status.regions || {}).map(([name, info]) => [
        AX.region(name), AX.num(info.nodes), AX.num(info.usable), AX.job(info.job),
        info.active ? el("span", { class: "badge ok", text: "이 리전" }) : "—"]),
      "설정된 리전이 없습니다");
  }

  function evalState(value) {
    return { requested: "요청됨", running: "실행 중", stopping: "멈추는 중", stopped: "멈춤", done: "완료",
             failed: "실패", rejected: "거절됨" }[value] || value || "—";
  }

  // --------------------------------------------------------------------------------- static cluster
  function box(title, lines, kind) {
    return el("div", { class: `arch-box ${kind || ""}` }, el("strong", { text: title }),
              lines.map((line) => el("span", { class: "small", text: line })));
  }
  function arrow(text, vertical) {
    return el("div", { class: `arch-arrow${vertical ? " down" : ""}` }, el("span", { class: "small muted", text }));
  }

  function renderArch() {
    const node = (index, layers) => box(`노드 ${index} · ND96amsr_A100_v4`, [
      `A100 80GB × 8 (NVLink)`, `텐서 병렬 8: 레이어 ${layers}`,
      index === 0 ? "Ray 헤드 · vLLM API 서버 · 링크" : "Ray 워커"], "gpu");
    $("arch").replaceChildren(
      box("사용자 브라우저", ["채팅 화면 · 도구 실행(계산기·시각·날씨)", "대화 내용은 브라우저에만 보관"]),
      arrow("HTTPS (비밀번호 로그인)", true),
      box("Azure App Service (한국 중부)", ["이 웹앱: 로그인 · 대기열 · 스트리밍 중계",
        "GPU 켜기/끄기 · 리전 선택 · 평가 실행 관리"], "app"),
      arrow("WebSocket: GPU 노드가 웹앱으로 먼저 연결 (GPU 쪽에 공개 포트 없음)", true),
      el("div", { class: "arch-cluster" },
         el("div", { class: "small muted", text: "Azure ML 컴퓨트 클러스터 (영국·이탈리아·프랑스 중 GPU를 먼저 확보한 리전)" }),
         el("div", { class: "arch-row" }, node(0, "0~30"),
            arrow("InfiniBand 200Gb/s × 8: 파이프라인 병렬 2 (활성값 전달)"), node(1, "31~60"))),
      el("p", { class: "small muted", text: "모델 61개 층을 앞뒤 절반으로 나눠 노드마다 하나씩 맡기고(파이프라인 병렬), 각 노드 안에서는 " +
        "한 층의 계산을 GPU 8장이 나눠 합니다(텐서 병렬). 가중치가 694GB라 GPU 8장(640GB)에는 다 들어가지 않기 때문입니다." }));
  }

  const DIFF = [
    ["가중치·설정·토크나이저·채팅 템플릿", "SKT 공개 체크포인트", "같은 파일, 수정 없음", "없음"],
    ["행렬곱(선형층·MoE 전문가)", "FP8 텐서코어에서 FP8×FP8", "A100엔 FP8 텐서코어가 없어 FP8 가중치를 커널 안에서 BF16으로 풀어 계산 (Marlin W8A16)",
     "가중치 메모리는 같음. 계산이 많은 구간에서 느림. 결과가 비트 단위로 같지는 않음"],
    ["희소 어텐션(DSA) 인덱서", "DeepGEMM FP8 커널", "같은 FP8 입력을 Triton 커널로 계산 (vLLM PR #38476 포팅)", "같은 규칙, 다른 커널"],
    ["희소 MLA 어텐션(선택된 2,048개 키)", "FlashMLA / FlashInfer", "Triton 희소 MLA 커널 (같은 PR, 업스트림 미병합)", "같은 계산, 더 느림"],
    ["KV 캐시", "BF16 기본, FP8 선택 가능", "BF16만 가능", "FP8 KV로 메모리를 아낄 수 없음. 16장 합계 673,792 토큰"],
    ["엔진 코드", "vLLM 0.23 + SKT 포크", "같음 + 위 포팅 + A100용 2줄 수정(mla_attention.py)", "수정 없이는 A100에서 서버가 멈춤"],
    ["병렬화", "1노드, 텐서 병렬 4 또는 8", "2노드, 텐서 병렬 8 × 파이프라인 병렬 2 (Ray)", "노드 간 통신이 한 번 늘고, 노드 하나가 빠지면 서비스가 멈춤"],
    ["노드 간 네트워크", "해당 없음", "NCCL over InfiniBand (GPU 쌍당 20.6 GB/s, TCP는 0.35 GB/s)", "파이프라인 병렬 추론에는 영향 없음"],
    ["도구 호출", "--tool-call-parser hermes", "같은 파서 + --enable-auto-tool-choice", "9/9 정상. 이 옵션이 없으면 도구 호출이 본문 텍스트로 나옴"],
    ["컨텍스트 길이", "256K", "262,144 토큰, 252,728 토큰 문서에서 정보 찾기 9/9", "같음"],
    ["결정성(batch-invariant)", "기본은 꺼짐", "A100에서는 켤 수 없음", "같은 요청도 답이 조금씩 달라질 수 있음"],
    ["추측 디코딩(EAGLE3)", "+23~30% 속도 (기술 보고서 Fig. 8)", "파이프라인 병렬과 함께 못 쓰고, A100 MLA 커널이 지원하지 않음", "속도 향상 옵션 없음"],
    ["속도", "B200 1노드 (기술 보고서 Fig. 7)", "같은 벤치마크로 B200 1노드의 0.43~0.51배 (입력 1K~8K)", "GPU 1장당 약 1/4. 긴 입력에서는 더 느림"],
  ];

  function renderDiff() {
    AX.table($("diff"), ["항목", "레퍼런스 (B200/H100급 1노드)", "이 데모 (A100 16장)", "영향"], DIFF);
  }

  // ----------------------------------------------------------------------------------------- results
  const REASONS = { scope: "이번 검증 범위 밖", gated: "데이터셋 접근 승인 필요", external: "Artificial Analysis 자체 채점",
                    sandbox: "코드 실행·웹 검색 환경 필요", unpublished: "문제 세트 미공개" };
  const RUN_STATES = { requested: "요청됨", running: "실행 중", stopping: "멈추는 중", stopped: "멈춤", done: "완료",
                       failed: "실패", rejected: "거절됨" };
  const FIGURES = { "fig7-overlay.png": "기술 보고서 Fig. 7(B200)에 A100 측정값을 겹친 그래프",
                    "fig9-overlay.png": "기술 보고서 Fig. 9(B200 NVFP4)에 A100 NVFP4 측정값을 겹친 그래프",
                    "per-gpu-throughput.png": "GPU 1장당 처리량 비교",
                    "cost-per-1m-output-tokens.png": "출력 100만 토큰당 비용 (A100, 가격 유형별)" };

  async function loadResults() {
    state.loaded = true;
    $("run-info").textContent = "불러오는 중…";
    try {
      state.results = await AX.api("/api/results");
    } catch (error) {
      state.loaded = false;
      $("run-info").textContent = error.message;
      if (error.status === 401) locked();
      return;
    }
    const data = state.results;
    const select = $("run-select");
    const keep = select.value;
    const runs = data.runs || [];
    select.replaceChildren(...runs.map((run) => el("option", { value: run.run,
      text: `${run.run} (${RUN_STATES[run.state] || run.state || "—"})` })));
    const preferred = runs.find((r) => r.run === keep) || runs.find((r) => r.run === data.current && r.summary) ||
      runs.find((r) => r.summary) || runs[0];
    if (preferred) select.value = preferred.run;
    AX.show(select, runs.length > 0);
    renderRun();
    renderThroughput();
    renderFigures();
  }

  function currentRun() {
    const runs = (state.results && state.results.runs) || [];
    return runs.find((r) => r.run === $("run-select").value) || null;
  }

  function score(entry, digits) {
    if (!entry || entry.score === null || entry.score === undefined) return null;
    const half = entry.ci95_half;
    return `${AX.num(entry.score * 100, digits)}${half ? ` ± ${AX.num(half * 100, digits)}` : ""}`;
  }

  function renderRun() {
    const run = currentRun();
    const summary = (run && run.summary) || {};
    const plan = (run && run.plan) || {};
    if (!run) {
      $("run-info").textContent = "아직 이 데모에서 실행한 평가가 없습니다. SKT 공개값만 표시합니다.";
    } else {
      const done = Object.values(summary).reduce((sum, s) => sum + (s.n_generations || 0), 0);
      const expected = Object.values(summary).reduce((sum, s) => sum + (s.generations_expected || 0), 0);
      $("run-info").textContent = `실행 ${run.run}: ${RUN_STATES[run.state] || run.state || "—"}` +
        `${expected ? `, 생성 ${AX.num(done)} / ${AX.num(expected)}건` : ""}` +
        `${run.seconds ? `, ${AX.duration(run.seconds)}` : ""}` +
        `${run.state !== "done" ? " (진행 중인 값은 중간 집계입니다)" : ""}`;
    }
    const published = (state.results && state.results.published && state.results.published.benchmarks) || { rows: [] };
    const rows = published.rows.map((row) => {
      const entry = row.suite ? summary[row.suite] : null;
      let ours = "—";
      let note = "";
      if (!row.suite) {
        note = REASONS[row.not_run] || row.not_run || "";
      } else if (entry) {
        ours = score(entry, 1) || "—";
        note = `${AX.num(entry.n_items)}/${AX.num(entry.items_expected)}문항 × ${AX.num(entry.repeats)}회`;
        if (entry.hit_limit) note += `, 길이 초과 ${AX.num(entry.hit_limit)}건`;
        if (row.suite === "ifbench" && entry.prompt_strict !== undefined) {
          note += `, strict ${AX.num(entry.prompt_strict * 100, 1)}`;
        }
      } else {
        note = plan[row.suite] ? "실행 중" : "이 실행에 포함되지 않음";
      }
      const skt = Number.isInteger(row.skt) && row.skt > 100 ? AX.num(row.skt) : AX.num(row.skt, 1);
      return [row.domain, row.benchmark, skt, el("td", { class: row.suite ? "strong" : "muted", text: ours }), note];
    });
    if (published.niah) {
      const entry = summary.niah;
      rows.push(["Long context", "NIAH (8K~256K)", AX.num(published.niah.skt, 1),
                 el("td", { class: "strong", text: (entry && score(entry, 1)) || "—" }),
                 entry ? `${AX.num(entry.n_items)}/${AX.num(entry.items_expected)}칸` : "아래 표 참고"]);
    }
    AX.table($("bench"), ["분야", "벤치마크", "SKT 공개값", "이 데모 (A100)", "비고"], rows);
    renderNiah(summary.niah);
  }

  function renderNiah(entry) {
    const grid = entry && entry.grid;
    const host = $("niah");
    if (!grid || !Object.keys(grid).length) {
      host.replaceChildren(el("p", { class: "muted small", text: "NIAH 결과가 아직 없습니다. 이전 검증에서는 SKT 예제 그대로 " +
        "32K·128K·256K 토큰 문서에서 9/9를 찾았습니다." }));
      return;
    }
    const lengths = Object.keys(grid).sort((a, b) => Number(a) - Number(b));
    const depths = [...new Set(lengths.flatMap((l) => Object.keys(grid[l])))].sort((a, b) => Number(a) - Number(b));
    const table = el("table", { class: "table niah" });
    AX.table(table, ["문서 길이 \\ 위치", ...depths.map((d) => `${Math.round(Number(d) * 100)}%`)],
      lengths.map((length) => [AX.tokens(Number(length)), ...depths.map((depth) => {
        const hit = grid[length][depth];
        return el("td", { class: hit === undefined ? "cell" : hit ? "cell ok" : "cell fail",
                          text: hit === undefined ? "" : hit ? "✓" : "✗",
                          title: `${AX.tokens(Number(length))} 토큰, 위치 ${Math.round(Number(depth) * 100)}%` });
      })]));
    host.replaceChildren(el("div", { class: "scroll" }, table),
      el("p", { class: "muted small", text: "가로: 문서 안에서 정답 문장의 위치, 세로: 문서 길이(토큰). ✓는 찾음, ✗는 못 찾음입니다." }));
  }

  const SERIES = [
    ["b200_fp8_k2", "B200 × 8 FP8 (SKT)"], ["a100_fp8_native", "A100 × 16 FP8 (이 데모)"],
    ["a100_fp8_dense", "A100 × 16 FP8 dense"], ["b200_nvfp4_k2_fig9", "B200 × 4 NVFP4 (SKT)"],
    ["a100_nvfp4", "A100 × 8 NVFP4"]];

  function value(point, metric) {
    if (!point) return null;
    if (metric === "output") return point.output_tok_s !== undefined ? point.output_tok_s : point.output_throughput;
    return point.total_tok_s !== undefined ? point.total_tok_s : point.total_token_throughput;
  }

  function renderThroughput() {
    const series = (state.results && state.results.throughput && state.results.throughput.series) || {};
    const metric = $("tp-metric").value;
    const present = SERIES.filter(([key]) => series[key]);
    const inputs = [...new Set(present.flatMap(([key]) => series[key].points.map((p) => p.input_tokens)))]
      .sort((a, b) => a - b);
    const at = (key, input) => value((series[key] ? series[key].points : []).find((p) => p.input_tokens === input), metric);
    const ratio = (a, b) => (a && b ? `${AX.num(a / b, 2)}배` : "—");
    AX.table($("throughput"), ["입력 토큰", ...present.map(([, label]) => label),
                               "FP8 비율 (A100÷B200)", "NVFP4 비율 (A100÷B200)"],
      inputs.map((input) => [AX.tokens(input), ...present.map(([key]) => AX.num(at(key, input))),
        ratio(at("a100_fp8_native", input), at("b200_fp8_k2", input)),
        ratio(at("a100_nvfp4", input), at("b200_nvfp4_k2_fig9", input))]),
      "처리량 데이터가 없습니다");
  }

  function renderFigures() {
    const names = (state.results && state.results.figures) || [];
    $("figures").replaceChildren(...(names.length ? names.map((name) => {
      const url = `/api/results/figure/${encodeURIComponent(name)}`;
      return el("figure", null,
                el("a", { href: url, target: "_blank", rel: "noopener" },
                   el("img", { src: url, alt: FIGURES[name] || name, loading: "lazy" })),
                el("figcaption", { class: "small muted", text: FIGURES[name] || name }));
    }) : [el("p", { class: "muted small", text: "그래프가 없습니다." })]));
  }

  $("run-select").addEventListener("change", renderRun);
  $("tp-metric").addEventListener("change", renderThroughput);
  $("results-refresh").addEventListener("click", loadResults);

  renderArch();
  renderDiff();
  checkLogin();
})();
