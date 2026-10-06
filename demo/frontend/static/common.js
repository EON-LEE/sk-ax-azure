"use strict";
// Shared helpers for the demo and admin pages. Everything is built with DOM calls (no innerHTML), so text from
// the model or the server can never become markup; the CSP also forbids inline scripts and styles.
(function () {
  const AX = (window.AX = {});

  class ApiError extends Error {
    constructor(status, code, message, data) {
      super(message);
      this.status = status;
      this.code = code;
      this.data = data || null;
    }
  }
  AX.ApiError = ApiError;

  AX.problem = async function (response) {
    let data = null;
    try {
      data = await response.json();
    } catch (error) {
      data = null;
    }
    const message = (data && typeof data.message === "string" && data.message) ||
      `요청이 실패했습니다 (HTTP ${response.status}).`;
    return new ApiError(response.status, (data && data.error) || "http", message, data);
  };

  // JSON in, JSON out; a non-2xx answer becomes an ApiError carrying the server's {error, message}.
  AX.api = async function (path, options) {
    options = options || {};
    const init = { method: options.method || (options.body === undefined ? "GET" : "POST"),
                   credentials: "same-origin", headers: {}, signal: options.signal };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      if (error && error.name === "AbortError") throw error;
      throw new ApiError(0, "network", "서버에 연결할 수 없습니다. 네트워크 연결을 확인해 주세요.");
    }
    if (!response.ok) throw await AX.problem(response);
    return response.json();
  };

  const PROPS = new Set(["value", "checked", "disabled", "selected", "open", "hidden", "type", "htmlFor"]);

  // el("div", {class: "x", onclick: fn, text: "..."}, child, "text", [more children])
  AX.el = function (tag, attrs) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = String(value);
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else if (PROPS.has(key)) node[key] = value;
      else node.setAttribute(key, value === true ? "" : String(value));
    }
    const children = Array.prototype.slice.call(arguments, 2).flat(Infinity);
    for (const child of children) {
      if (child === undefined || child === null || child === false) continue;
      node.append(child instanceof Node ? child : String(child));
    }
    return node;
  };

  AX.$ = (selector, root) => (root || document).querySelector(selector);
  AX.$$ = (selector, root) => Array.from((root || document).querySelectorAll(selector));
  AX.show = (node, visible) => node.classList.toggle("hidden", !visible);

  // ------------------------------------------------------------------------------------------ formatting
  const formats = {};
  AX.num = function (value, digits) {
    if (value === null || value === undefined || value === "" || !Number.isFinite(Number(value))) return "—";
    digits = digits || 0;
    formats[digits] = formats[digits] || new Intl.NumberFormat("ko-KR", { minimumFractionDigits: digits,
                                                                          maximumFractionDigits: digits });
    return formats[digits].format(Number(value));
  };
  AX.pct = (fraction, digits) => (fraction === null || fraction === undefined || !Number.isFinite(Number(fraction))
    ? "—" : AX.num(Number(fraction) * 100, digits === undefined ? 1 : digits) + "%");
  AX.tokens = function (count) {
    count = Number(count);
    if (!Number.isFinite(count)) return "—";
    if (count >= 1024 && count % 1024 === 0) return `${count / 1024}K`;
    return AX.num(count);
  };
  AX.duration = function (seconds) {
    seconds = Math.max(0, Math.round(Number(seconds) || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    if (hours) return `${hours}시간 ${minutes}분`;
    if (minutes) return `${minutes}분 ${rest}초`;
    return `${rest}초`;
  };
  const clock = new Intl.DateTimeFormat("ko-KR", { month: "2-digit", day: "2-digit", hour: "2-digit",
                                                    minute: "2-digit", second: "2-digit", hour12: false });
  AX.time = (t) => (t ? clock.format(new Date(Number(t) * 1000)) : "—");
  AX.ago = (t) => (t ? `${AX.duration(Date.now() / 1000 - Number(t))} 전` : "—");

  AX.POWER = { off: "꺼짐", stopping: "끄는 중", waiting_capacity: "GPU 확보 대기", starting: "시작 중",
               booting: "준비 중", ready: "사용 가능" };
  AX.powerClass = (power) => (power === "ready" ? "ok" : power === "off" ? "off" : "busy");
  AX.PHASES = { boot: "노드 부팅", overlay: "엔진 패치 적용", ib_probe: "InfiniBand 점검",
                download: "가중치 내려받기", ray: "Ray 클러스터 구성", loading: "모델 로딩", ready: "서빙 중",
                restarting: "서버 재시작", failed: "실패" };
  AX.LOAD_STEPS = { "Loading safetensors checkpoint shards": "가중치 로딩", "Capturing CUDA graphs": "CUDA 그래프 캡처" };
  AX.loadStep = function (step) {
    for (const [prefix, label] of Object.entries(AX.LOAD_STEPS)) {
      if (String(step || "").startsWith(prefix)) return label;
    }
    return step || "";
  };
  AX.REGIONS = { uksouth: "영국 남부", italynorth: "이탈리아 북부", francecentral: "프랑스 중부" };
  AX.region = (name) => (name ? `${AX.REGIONS[name] || name} (${name})` : "—");
  AX.JOB = { Submitting: "제출 중", NotStarted: "시작 전", Starting: "시작 중", Queued: "대기 (GPU 확보 중)",
             Preparing: "준비 중", Provisioning: "할당 중", Running: "실행 중", Finalizing: "마무리 중",
             CancelRequested: "취소 요청됨", Canceled: "취소됨", Completed: "완료", Failed: "실패",
             NotResponding: "응답 없음" };
  AX.job = (status) => (status ? AX.JOB[status] || status : "—");

  AX.bar = function (fraction, label) {
    const inner = AX.el("div", { class: "fill" });
    inner.style.width = `${Math.max(0, Math.min(1, Number(fraction) || 0)) * 100}%`;
    return AX.el("div", { class: "progress" }, AX.el("div", { class: "track" }, inner),
                 AX.el("span", { class: "small muted", text: label }));
  };

  // A definition list from [label, value] pairs; a value may be a node.
  AX.kv = function (node, rows) {
    node.replaceChildren(...rows.filter(Boolean).flatMap(([label, value]) =>
      [AX.el("dt", { text: label }), AX.el("dd", null, value === undefined || value === null || value === "" ? "—" : value)]));
  };

  AX.table = function (node, head, rows, empty) {
    const body = rows.length ? rows.map((cells) => AX.el("tr", null, cells.map((cell) =>
      (cell instanceof Node && cell.tagName === "TD") ? cell : AX.el("td", null, cell)))) :
      [AX.el("tr", null, AX.el("td", { class: "muted", colspan: head.length, text: empty || "없음" }))];
    node.replaceChildren(AX.el("thead", null, AX.el("tr", null, head.map((h) => AX.el("th", { text: h })))),
                         AX.el("tbody", null, body));
  };

  // Runs fn now and every `seconds` while the page is visible; returns a function that runs it again at once.
  AX.every = function (seconds, fn) {
    let timer = null;
    let running = false;
    const tick = async () => {
      clearTimeout(timer);
      if (document.visibilityState !== "visible") return;
      if (!running) {
        running = true;
        try {
          await fn();
        } catch (error) {
          console.warn(error);
        } finally {
          running = false;
        }
      }
      timer = setTimeout(tick, seconds * 1000);
    };
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") tick();
      else clearTimeout(timer);
    });
    tick();
    return tick;
  };
})();
