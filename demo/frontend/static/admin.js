"use strict";
// The admin page: power, jobs, nodes, cost, evals, password rotation and the event log, polled every 10 s.
(function () {
  const AX = window.AX;
  const el = AX.el;
  const $ = (id) => document.getElementById(id);
  const SUITES = { aime: "AIME26 (수학)", kobalt: "KoBALT (한국어)", click: "CLIcK (한국어)", ifbench: "IFBench (지시 이행)",
                   niah: "NIAH (긴 문서)" };
  const EVAL = { requested: "요청됨", running: "실행 중", stopping: "멈추는 중", stopped: "멈춤", done: "완료",
                 failed: "실패", rejected: "거절됨" };
  let poll = null;

  function message(text, error) {
    const node = $("message");
    node.textContent = text || "";
    node.classList.toggle("error", Boolean(error));
    AX.show(node, Boolean(text));
  }

  async function call(path, body, confirmText) {
    if (confirmText && !window.confirm(confirmText)) return null;
    try {
      const state = await AX.api(path, { body: body || {} });
      message("");
      if (state && state.status) render(state);
      return state;
    } catch (error) {
      if (error.status === 401) return locked();
      message(error.message, true);
      return null;
    }
  }

  function locked() {
    AX.show($("login"), true);
    $("login-password").focus();
    return null;
  }

  async function refresh() {
    if (!$("login").classList.contains("hidden")) return;
    try {
      render(await AX.api("/api/admin/state"));
    } catch (error) {
      if (error.status === 401) locked();
      else message(error.message, true);
    }
  }

  function render(data) {
    const status = data.status || {};
    const badge = $("power");
    badge.textContent = AX.POWER[status.power] || status.power;
    badge.className = `badge ${AX.powerClass(status.power)}`;
    const cooldown = Object.entries(data.cooldown || {});
    AX.kv($("summary"), [
      ["상태", `${AX.POWER[status.power] || status.power} (희망: ${status.desired === "on" ? "켜짐" : "꺼짐"})`],
      ["서빙 리전", AX.region(status.region)],
      ["단계", status.phase ? `${AX.PHASES[status.phase] || status.phase}${status.note ? ` · ${status.note}` : ""}` : "—"],
      ["켜진 시간", status.active_since ? AX.duration(data.t - status.active_since) : "—"],
      ["모드", `${data.mode || "—"}${data.mode_since ? ` (${AX.ago(data.mode_since)})` : ""}`],
      ["대기 중인 리전", cooldown.length ? cooldown.map(([r, s]) => `${r} ${AX.duration(s)}`).join(", ") : "없음"],
      ["처리 중 / 대기", `${AX.num((status.queue || {}).active)} / ${AX.num((status.queue || {}).waiting)}`],
      ["링크 연결", AX.num(data.links)],
    ]);
    $("power-on").disabled = status.desired === "on";
    $("power-off").disabled = status.desired !== "on" && status.power === "off";
    $("restart").disabled = status.desired !== "on" || !status.region;

    const cost = data.cost || { regions: {} };
    AX.kv($("cost"), [["합계", `$${AX.num(cost.usd, 2)}`],
      ...Object.entries(cost.regions).map(([region, c]) =>
        [AX.region(region), `${AX.num(c.node_hours, 2)} 노드시간 × $${AX.num(c.price, 2)} = $${AX.num(c.usd, 2)}`])]);

    AX.table($("jobs"), ["ID", "리전", "작업 이름", "AML 상태", "제출", "링크", "단계", "처리 중", "서빙"],
      (data.jobs || []).map((job) => [job.id, job.region, job.name || "—", AX.job(job.status), AX.ago(job.submitted),
        job.link ? (job.ready ? "준비됨" : "연결됨") : "—", AX.PHASES[job.phase] || job.phase || "—",
        AX.num(job.inflight), job.active ? el("span", { class: "badge ok", text: "활성" }) : "—"]), "작업 없음");
    AX.table($("nodes"), ["리전", "할당", "사용 가능", "갱신"],
      Object.entries(data.nodes || {}).map(([region, n]) => [AX.region(region), AX.num((n || {}).total),
        AX.num((n || {}).usable), AX.ago((n || {}).t)]), "할당된 노드 없음");

    const active = (data.jobs || []).find((job) => job.active) || {};
    const current = active.eval || {};
    AX.kv($("eval-state"), [
      ["요청", data.eval_wish ? `${data.eval_wish.run} (${data.eval_wish.suites})` : "없음"],
      ["실행 중", current.run ? `${current.run}: ${EVAL[current.state] || current.state}` : "없음"],
      ["평가 패키지", active.eval_packages || "—"],
      current.tail ? ["마지막 로그", el("pre", { class: "small plain", text: current.tail })] : null,
    ]);
    AX.table($("eval-runs"), ["실행", "상태", "스위트", "반복", "제한", "생성"],
      (data.eval_runs || []).map((run) => [run.run, EVAL[run.state] || run.state || "—", run.suites || "—",
        run.repeats || "기본", run.limit || "전체", AX.time(run.created)]), "평가 실행 없음");

    AX.kv($("passwords"), Object.entries(data.passwords || {}).map(([role, p]) =>
      [role === "admin" ? "관리자" : "데모", `${p.configured ? "설정됨" : "없음"}${p.rotated ? " · 교체됨" : ""} · 세대 ${p.generation}`]));
    const config = data.config || {};
    AX.kv($("config"), [["리전 순서", (config.regions || []).join(", ")], ["컴퓨트", config.compute],
      ["작업당 노드", AX.num(config.nodes)], ["기본 단가", `$${AX.num(config.price, 3)}/노드시간`],
      ["리전별 단가", Object.entries(config.prices || {}).map(([r, p]) => `${r} $${p}`).join(", ") || "—"],
      ["동시 / 대기 한도", `${AX.num(config.max_active)} / ${AX.num(config.max_queue)}`],
      ["링크 URL", config.link_url || "—"]]);
    AX.table($("events"), ["시각", "종류", "내용"],
      (data.events || []).map((e) => [AX.time(e.t), e.kind, e.message]), "이벤트 없음");
  }

  // ------------------------------------------------------------------------------------------- wiring
  $("login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("login-error").textContent = "";
    try {
      await AX.api("/api/admin/login", { body: { password: $("login-password").value } });
      $("login-password").value = "";
      AX.show($("login"), false);
      if (poll) poll();
      else poll = AX.every(10, refresh);
    } catch (error) {
      $("login-error").textContent = error.message;
    }
  });
  $("logout").addEventListener("click", async () => {
    await AX.api("/api/logout", { body: {} }).catch(() => null);
    locked();
  });
  $("power-on").addEventListener("click", () => call("/api/admin/power", { state: "on" },
    "GPU 클러스터를 켤까요? 노드가 할당되는 순간부터 노드시간 요금이 나갑니다."));
  $("power-off").addEventListener("click", () => call("/api/admin/power", { state: "off" },
    "GPU 클러스터를 끌까요? 진행 중인 대화와 평가가 모두 끊깁니다."));
  $("restart").addEventListener("click", () => call("/api/admin/restart", {},
    "현재 작업을 취소하고 다시 제출할까요? 모델을 다시 올리는 동안 서비스가 멈춥니다."));

  $("suites").replaceChildren(...Object.entries(SUITES).map(([name, label]) =>
    el("label", { class: "toggle" }, el("input", { type: "checkbox", value: name, checked: true }), label)));
  $("eval-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const suites = AX.$$("#suites input:checked").map((input) => input.value);
    if (!suites.length) return message("스위트를 하나 이상 고르세요.", true);
    const limit = Number($("eval-limit").value || 0);
    return call("/api/admin/eval", { action: "start", suites: suites.join(","), repeats: $("eval-repeats").value.trim(),
                                     limit: Number.isInteger(limit) ? limit : 0, run: $("eval-run").value.trim() || undefined });
  });
  $("eval-stop").addEventListener("click", () => call("/api/admin/eval", { action: "stop" }, "진행 중인 평가를 멈출까요?"));

  for (const button of AX.$$("[data-rotate]")) {
    button.addEventListener("click", async () => {
      const role = button.dataset.rotate;
      const result = await call("/api/admin/password", { role },
        `${role === "admin" ? "관리자" : "데모"} 비밀번호를 새로 만들까요? 기존 비밀번호로 로그인한 사람은 모두 로그아웃됩니다.`);
      if (!result) return;
      const shown = $("new-password");
      shown.replaceChildren(`새 ${role === "admin" ? "관리자" : "데모"} 비밀번호: `,
                            el("span", { class: "secret", text: result.password }));
      AX.show(shown, true);
      refresh();
    });
  }

  (async () => {
    const me = await AX.api("/api/me").catch(() => ({ admin: false }));
    if (me.admin) poll = AX.every(10, refresh);
    else locked();
  })();
})();
