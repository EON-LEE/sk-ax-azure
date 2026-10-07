"use strict";
// The demo page shell: the optional login, and the /api/status poll (status dot and banner).
(function () {
  const AX = window.AX;
  const $ = (id) => document.getElementById(id);
  const state = { poll: null };

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

  window.addEventListener("ax:unauthorized", locked);

  // ------------------------------------------------------------------------------------------ status
  function start() {
    if (state.poll) {
      state.poll();
      return;
    }
    state.poll = AX.every(5, poll);
    $("input").focus();
  }

  async function poll() {
    if (!$("login").classList.contains("hidden")) return;
    let status;
    try {
      const profiles = await AX.api("/api/models");
      AX.chat.setModels(profiles.models);
      status = AX.chat.status();
    } catch (error) {
      if (error.status === 401) locked();
      else showBanner("서버에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.", true);
      return;
    }
    render(status);
  }

  function showBanner(text, error) {
    const banner = $("banner");
    banner.textContent = text || "";
    banner.classList.toggle("error", Boolean(error));
    AX.show(banner, Boolean(text));
  }

  const LABELS = { ready: "사용 가능", off: "서비스 준비 중", stopping: "서비스 종료 중" };

  function render(status) {
    const power = status.power;
    $("power").className = `dot ${AX.powerClass(power)}`;
    $("power").title = LABELS[power] || "서비스 시작 중";
    AX.chat.setPower(power);
    const queue = status.queue || {};
    let text = "";
    if (power === "off") text = "지금은 모델 서버가 꺼져 있습니다. 담당자에게 문의해 주세요.";
    else if (power === "stopping") text = "모델 서버를 종료하는 중입니다.";
    else if (power === "unknown") text = "모델 상태를 확인하는 중입니다.";
    else if (power === "configuration_error") text = "실제 체크포인트·실행 구성을 확인하지 못해 이 모델의 요청을 차단했습니다. 담당자에게 문의해 주세요.";
    else if (power !== "ready") text = "모델 서버를 시작하는 중입니다. 준비되면 바로 대화할 수 있습니다 (수십 분 걸릴 수 있습니다).";
    else if (queue.waiting > 0) text = `요청이 많아 ${AX.num(queue.waiting)}건이 대기 중입니다. 보내면 순서대로 처리합니다.`;
    showBanner(text, false);
  }

  checkLogin();
  window.addEventListener("ax:view-change", () => poll());
})();
