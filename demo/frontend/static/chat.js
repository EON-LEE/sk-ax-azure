"use strict";
// Chat tab: streams /api/chat (server-sent events read through fetch), shows the reasoning and the answer,
// MAF executes tools on the server; this tab renders genuine model deltas and middleware action events.
(function () {
  const AX = window.AX;
  const { el } = AX;

  const BODY_LIMIT = 4 * 1024 * 1024;  // the server's request size limit
  const FILE_BYTES = 8 * 1024 * 1024;

  const EXAMPLES = [
    { label: "코드 작성", text: "파이썬으로 이진 탐색 함수를 작성하고 시간 복잡도를 설명해 줘.", tools: true },
    { label: "도구로 정확히 계산", text: "2의 64제곱에서 1을 뺀 값을 계산기로 정확히 구해 줘.", tools: true },
    { label: "문서·데이터 분석", text: "첨부한 문서·CSV를 읽고 핵심 내용과 합계를 정리해 줘. 파일이 없으면 먼저 첨부를 요청해 줘.", tools: true },
    { label: "파일 수정·미리보기", text: "간단한 소개 페이지 index.html을 만들고 다운로드와 미리보기를 제공해 줘.", tools: true },
    { label: "Web IQ · 웹 검색", text: "Web IQ 웹 검색으로 서울의 산책하기 좋은 공원 두 곳을 찾고 실제 출처 링크를 알려 줘.", tools: true },
    { label: "Web IQ · 최신 뉴스", text: "Web IQ 뉴스 도구로 최신 AI 뉴스 두 건을 찾아 기사 시각과 실제 출처 링크를 알려 줘.", tools: true },
    { label: "Web IQ · 삼성전자 주가", text: "Web IQ 금융 도구로 오늘 삼성전자 005930 주가를 조회하고 출처·시세 기준 시각을 알려 줘. 지연 여부가 미제공이면 미확인으로 표시해 줘.", tools: true },
    { label: "Web IQ · 주변 장소", text: "Web IQ 장소 도구로 서울 시청 근처 카페 두 곳을 찾아 실제 주소와 출처 링크를 알려 줘.", tools: true },
  ];

  const SVG = "http://www.w3.org/2000/svg";
  const ICONS = {
    calculator: "M7 3h10a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM8 7h8M8 12h.01M12 12h.01M16 12h.01M8 16h.01M12 16h.01M16 16h.01",
    get_current_time: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
    tool: "M14.7 6.3a4 4 0 0 0-5.4 5.2L3 17.8V21h3.2l6.3-6.3a4 4 0 0 0 5.2-5.4l-2.6 2.6-2.4-.6-.6-2.4z",
    check: "M5 12l5 5L20 7",
    cross: "M6 6l12 12M18 6L6 18",
    copy: "M9 9h10v10H9zM5 15V5h10",
  };
  function icon(name) {
    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = document.createElementNS(SVG, "path");
    path.setAttribute("d", ICONS[name] || ICONS.tool);
    svg.append(path);
    return svg;
  }

  const ui = {};
  const profilePrefix = model => model === "fp8" ? "" : "/models/nvfp4";
  const session = mode => ({ mode, model: "fp8", prefix: "", history: [], busy: false, controller: null,
    attachments: [], power: "unknown", epoch: 0, workspace: null, workspacePromise: null, uploading: false,
    draft: "", scroll: 0, tools: mode === "chat", thinking: mode === "chat", previous: null });
  const sessions = { chat: session("chat"), compare: session("compare") };
  let state = sessions.compare;
  let models = [];
  const isCurrent = owner => owner === state;
  const LABELS = { calculator: "정확한 계산", get_current_time: "현재 시각", list_files: "파일 목록",
    read_file: "문서 읽기", read_pdf: "PDF 페이지 읽기", ocr_pdf: "PDF OCR (미구성)",
    search_files: "파일 검색", write_file: "파일 수정", diff_file: "변경 비교",
    analyze_table: "표 분석", chart_table: "차트", preview_html: "HTML 미리보기",
    web_iq_search: "Web IQ 검색", web_iq_web: "웹 검색", web_iq_news: "뉴스 검색",
    web_iq_finance: "금융 데이터", web_iq_places: "장소 검색", web_iq_browse: "공개 페이지 읽기",
    web_iq_images: "이미지 검색", web_iq_videos: "동영상 검색", web_iq_sports: "스포츠 검색",
    web_iq_sonic: "통합 검색", web_iq_autosuggest: "검색어 제안", run_tests: "실행 샌드박스 (미구성)" };

  async function workspace(owner = state) {
    const state = owner;
    if (state.workspace) return state.workspace;
    if (!state.workspacePromise) {
      const epoch = state.epoch;
      const prefix = state.prefix;
      state.workspacePromise = (async () => {
        const response = await fetch(prefix + "/api/workspace", { method: "POST" });
        if (!response.ok) throw new Error((await AX.problem(response)).message);
        const info = await response.json();
        if (epoch !== state.epoch) {
          releaseWorkspace("close", info.token, state, prefix);
          throw new DOMException("대화가 변경되었습니다.", "AbortError");
        }
        state.workspace = info.token;
        return info.token;
      })().finally(() => { if (epoch === state.epoch) state.workspacePromise = null; });
    }
    return state.workspacePromise;
  }

  function releaseWorkspace(action, token = state.workspace, owner = state, prefix = owner.prefix) {
    if (!token) return;
    fetch(`${prefix}/api/workspace/${action}`, { method: "POST", credentials: "same-origin",
      headers: { "X-AX-Workspace": token } }).then(response => {
      if (!response.ok && response.status !== 410) throw new Error(`HTTP ${response.status}`);
    }).catch(() => flash("이전 작업 종료 요청에 실패했습니다. 브라우저 응답 연결은 중단했습니다.", owner));
  }

  class TurnError extends Error {
    constructor(code, message, detail) {
      super(message);
      this.code = code;
      this.detail = detail || "";
    }
  }

  const nearBottom = () => ui.messages.scrollHeight - ui.messages.scrollTop - ui.messages.clientHeight < 120;
  const toBottom = () => {
    ui.messages.scrollTop = ui.messages.scrollHeight;
  };
  const secs = (ms) => `${AX.num(ms / 1000, 1)}초`;
  const aborted = (error) => !!error && error.name === "AbortError";

  // ----------------------------------------------------------------------------------- controls and hint
  let flashTimer = null;

  function readyHint() {
    clearTimeout(flashTimer);
    ui.hint.classList.remove("error");
    if (state.busy || state.power === "ready" || state.power === "unknown") ui.hint.textContent = "";
    else ui.hint.textContent = "모델 서버가 준비되면 보낼 수 있습니다";
  }

  function flash(message, owner = state) {
    if (!isCurrent(owner)) {
      owner.notice = message;
      return;
    }
    readyHint();
    ui.hint.textContent = message;
    ui.hint.classList.add("error");
    flashTimer = setTimeout(readyHint, 6000);
  }

  function updateControls() {
    ui.send.disabled = state.busy || state.uploading || state.power !== "ready";
    ui.tools.disabled = state.busy || state.uploading;
    ui.thinking.disabled = state.busy;
    ui.file.disabled = state.busy || state.uploading;
    AX.show(ui.send, !state.busy);
    AX.show(ui.stop, state.busy);
    readyHint();
  }

  // ---------------------------------------------------------------------------------------- attachments
  async function addFiles(list, owner = state) {
    const state = owner;
    if (state.busy || state.uploading) return;
    state.uploading = true;
    updateControls();
    const epoch = state.epoch;
    const prefix = state.prefix;
    try {
    for (const file of Array.from(list || [])) {
      if (file.size > FILE_BYTES) {
        flash(`${file.name}: 파일이 너무 큽니다.`, owner);
        continue;
      }
      try {
        const token = await workspace(state);
        if (epoch !== state.epoch) return;
        const response = await fetch(prefix + "/api/workspace/upload?name=" + encodeURIComponent(file.name),
          { method: "POST", headers: { "X-AX-Workspace": token }, body: file });
        if (!response.ok) throw new Error((await AX.problem(response)).message);
        const result = await response.json();
        if (epoch !== state.epoch) return;
        state.attachments.push({ name: file.name, text: "", size: file.size, files: result.files });
        state.tools = true;
        if (isCurrent(owner)) ui.tools.checked = true;
      } catch (error) {
        if (epoch !== state.epoch) return;
        flash(`${file.name}: ${error.message}`, owner);
        continue;
      }
    }
    renderAttachments(state);
    } finally {
      if (epoch === state.epoch) {
        state.uploading = false;
        if (isCurrent(owner)) updateControls();
      }
    }
  }

  function renderAttachments(owner = state) {
    if (!isCurrent(owner)) return;
    const state = owner;
    ui.attachments.replaceChildren(...state.attachments.map((item, index) => el("span", { class: "chip file" },
      `${item.name} · ${AX.num(item.size)} bytes`,
      el("button", { type: "button", class: "remove", title: "첨부 취소", "aria-label": `${item.name} 첨부 취소`,
                     text: "×", onclick: async () => {
                       if (state.busy || state.uploading) return;
                       const epoch = state.epoch;
                       try {
                         const response = await fetch(state.prefix + "/api/workspace/remove", { method: "POST",
                           headers: { "Content-Type": "application/json", "X-AX-Workspace": state.workspace },
                           body: JSON.stringify({ files: item.files }) });
                         if (!response.ok) throw new Error((await AX.problem(response)).message);
                         if (epoch !== state.epoch) return;
                         state.attachments.splice(index, 1);
                         renderAttachments(state);
                       } catch (error) {
                         if (epoch === state.epoch) flash(error.message, owner);
                       }
                     } }))));
    AX.show(ui.attachments, state.attachments.length > 0);
  }

  // -------------------------------------------------------------------------------------------- bubbles
  function userBubble(text, files, parent = state.panel) {
    const node = el("div", { class: "msg user" }, el("div", { class: "bubble" },
      files.length ? el("div", { class: "chips" }, files.map((file) =>
        el("span", { class: "chip file", text: `${file.name} · ${AX.num(file.size)} bytes` }))) : null,
      el("div", { class: "plain", text })));
    parent.append(node);
    return node;
  }

  function assistantBubble(parent = state.panel, owner = state) {
    const rounds = el("div", { class: "rounds" });
    const status = el("div", { class: "status", text: "요청을 보내는 중" });
    const foot = el("div", { class: "foot" });
    const node = el("div", { class: "msg assistant" }, el("div", { class: "bubble" }, rounds, status, foot));
    parent.append(node);
    return { node, rounds, status, foot, owner };
  }

  // One model request inside a turn: its reasoning (collapsible), its answer and its tool cards.
  function roundBox(view) {
    const label = el("span", { class: "label", text: "생각하는 중" });
    const summary = el("summary", null, label);
    const thought = el("div", { class: "thought" });
    const details = el("details", { class: "thinking hidden" }, summary, thought);
    const answer = el("div", { class: "answer" });
    const tools = el("div", { class: "tool-cards" });
    view.rounds.append(details, answer, tools);
    return { details, label, thought, answer, tools, shown: "", opened: false, closed: false, began: null, took: null };
  }

  function note(view, text, isError, detail) {
    view.rounds.append(el("p", { class: isError ? "note error" : "note", text }));
    if (detail) view.rounds.append(el("p", { class: "note tiny", text: `세부 정보: ${detail}` }));
  }

  // The reasoning and the answer of one round. Splits "<think>…</think>" out of the content if the server did
  // not separate them, and shows stray reasoning as the answer when thinking is off.
  function visible(round, thinking) {
    let reasoning = round.reasoning;
    let content = round.content;
    if (!reasoning && thinking) {
      const close = content.indexOf("</think>");
      if (close >= 0) {
        reasoning = content.slice(0, close).replace(/^\s*<think>/, "");
        content = content.slice(close + "</think>".length);
      } else if (/^\s*<think>/.test(content)) {
        reasoning = content.replace(/^\s*<think>/, "");
        content = "";
      }
    }
    if (!thinking && reasoning && !content.trim()) {
      content = reasoning;
      reasoning = "";
    }
    return { reasoning: reasoning.trim(), content: content.replace(/^\s+/, "") };
  }

  function paint(view, box, round, thinking) {
    const stick = isCurrent(view.owner) && nearBottom();
    const { reasoning, content } = visible(round, thinking);
    if (reasoning) {
      const settled = !!(content || round.calls.length || round.finish);
      box.details.classList.remove("hidden");
      if (box.thought.textContent !== reasoning) box.thought.textContent = reasoning;
      const now = performance.now();
      if (box.began === null) box.began = now;
      if (settled && box.took === null) box.took = now - box.began;
      box.details.classList.toggle("live", !settled);
      box.label.textContent = settled ? `${AX.num(Math.max(1, Math.round(box.took / 1000)))}초 동안 생각함`
        : `생각하는 중 · ${AX.num(Math.floor((now - box.began) / 1000))}초`;
      if (!box.opened && !settled) {
        box.details.open = true;  // show the reasoning live, then fold it once the answer starts
        box.opened = true;
      } else if (box.opened && !box.closed && settled) {
        box.details.open = false;
        box.closed = true;
      }
    }
    if (content !== box.shown) {
      AX.renderMarkdown(box.answer, content);
      box.shown = content;
    }
    if (!round.finish) {
      const call = round.calls.filter(Boolean).slice(-1)[0];
      AX.show(view.status, !content && !reasoning || !!call);
      if (call) view.status.textContent = `${LABELS[call.name] || "도구"} 호출 준비 중`;
    }
    if (stick) toBottom();
  }

  // ------------------------------------------------------------------------------------------ streaming
  // Yields {event, data} from a text/event-stream response; comments (": keep-alive") are skipped.
  async function* readEvents(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        buffer = (buffer + decoder.decode(value || new Uint8Array(0), { stream: !done })).replace(/\r\n/g, "\n");
        const blocks = buffer.split("\n\n");
        buffer = done ? "" : blocks.pop();
        for (const block of blocks) {
          let event = "message";
          const data = [];
          for (const line of block.split("\n")) {
            if (!line || line.startsWith(":")) continue;
            const colon = line.indexOf(":");
            const field = colon < 0 ? line : line.slice(0, colon);
            const text = colon < 0 ? "" : line.slice(colon + 1).replace(/^ /, "");
            if (field === "event") event = text;
            else if (field === "data") data.push(text);
          }
          if (data.length || event !== "message") yield { event, data: data.join("\n") };
        }
        if (done) return;
      }
    } finally {
      reader.cancel().catch(() => {});
    }
  }

  const json = (text) => {
    try {
      return JSON.parse(text);
    } catch (error) {
      return null;
    }
  };

  // Plainer wording for the validation errors, whose server messages are meant for developers.
  const MESSAGES = {
    bad_messages: "대화 기록 형식이 올바르지 않습니다. 새 대화를 시작해 주세요.",
    bad_request: "요청 형식이 올바르지 않습니다.",
    too_large: "대화가 너무 길어졌습니다. 새 대화를 시작하거나 첨부 문서를 줄여 주세요.",
  };

  // One request to the model. Streams into `round` and repaints; throws TurnError, or AbortError on stop.
  async function streamRound(payload, view, box, round, thinking, timing, endpoint = null) {
    const state = view.owner;
    const controller = state.controller;
    const prefix = state.prefix;
    const turnEpoch = state.epoch;
    let requested = performance.now();
    view.currentBox = box;
    let response;
    try {
      const token = endpoint ? endpoint.requestToken : await workspace(state);
      view.capability = Object.freeze(endpoint
        ? { prefix: endpoint.artifactPrefix, token: endpoint.artifactToken } : { prefix, token });
      response = await fetch(endpoint ? endpoint.url : prefix + "/api/agent", { method: "POST", credentials: "same-origin", body: payload,
                                            headers: { "Content-Type": "application/json",
                                                       "X-AX-Workspace": token },
                                            signal: controller.signal });
    } catch (error) {
      if (aborted(error)) throw error;
      throw new TurnError("network", "서버에 연결할 수 없습니다. 네트워크 연결을 확인해 주세요.");
    }
    if (!response.ok) {
      const problem = await AX.problem(response);
      if (response.status === 401) window.dispatchEvent(new CustomEvent("ax:unauthorized"));
      throw new TurnError(problem.code, MESSAGES[problem.code] || problem.message,
                          MESSAGES[problem.code] ? problem.message : "");
    }
    let started = null;
    let ended = false;
    let timer = null;
    const repaint = () => {
      timer = null;
      paint(view, box, round, thinking);
    };
    try {
      for await (const { event, data } of readEvents(response)) {
        if (turnEpoch !== state.epoch) throw new DOMException("대화가 변경되었습니다.", "AbortError");
        if (event === "round") {
          const index = (json(data) || {}).index || 1;
          if (index > 1) {
            round.finish = "tool_calls";
            paint(view, box, round, thinking);
            account(timing, round);
            box = roundBox(view);
            view.currentBox = box;
            requested = performance.now();
            Object.assign(round, { reasoning: "", content: "", calls: [], finish: null,
                                   usage: null, first: null, last: null });
          }
          timing.calls = index;
        } else if (event === "metrics") {
          view.metrics = json(data) || {};
        } else if (event === "action") {
          actionCard(view, box, json(data) || {});
        } else if (event === "notice") {
          note(view, (json(data) || {}).message || "실행 상태를 확인할 수 없습니다.", false);
        } else if (event === "policy") {
          const policy = json(data) || {};
          if (policy.reason === "user_prohibition" || policy.reason === "search_prohibition") {
            note(view, policy.tools ? "이번 턴은 사용자 요청에 따라 웹 검색 없이 답합니다." : "이번 턴은 사용자 요청에 따라 도구 없이 답합니다.", false);
          }
        } else if (event === "queue") {
          const info = json(data) || {};
          view.status.textContent = info.position ? `대기열 ${AX.num(info.position)}번째 · 곧 시작합니다` : "대기 중";
        } else if (event === "start") {
          started = performance.now();
          timing.wait += started - requested;
          view.status.textContent = "입력을 읽는 중";
        } else if (event === "end") {
          if ((json(data) || {}).state === "cancelled") throw new DOMException("실행이 취소되었습니다.", "AbortError");
          ended = true;
        } else if (event === "error") {
          const info = json(data) || {};
          throw new TurnError(info.code || "upstream", info.message || "모델 서버 오류가 발생했습니다.", info.detail);
        } else if (event === "message" && data !== "[DONE]") {
          const chunk = json(data);
          if (!chunk) continue;
          if (chunk.error || chunk.object === "error") {
            const problem = chunk.error || chunk;
            throw new TurnError("upstream", "모델 서버가 답변 도중 오류를 보냈습니다.",
                                typeof problem === "string" ? problem : problem.message);
          }
          if (chunk.usage) round.usage = chunk.usage;
          for (const choice of chunk.choices || []) {
            const delta = choice.delta || {};
            const reasoning = delta.reasoning_content || delta.reasoning || "";
            if (reasoning || delta.content || (delta.tool_calls && delta.tool_calls.length)) {
              const now = performance.now();
              if (round.first === null) round.first = now;
              if (timing.ttft === null && started !== null) timing.ttft = now - started;
              round.last = now;
            }
            round.reasoning += reasoning;
            round.content += delta.content || "";
            for (const part of delta.tool_calls || []) {
              const index = Number.isInteger(part.index) ? part.index : round.calls.length;
              const call = round.calls[index] || (round.calls[index] = { id: "", name: "", arguments: "" });
              const fn = part.function || {};
              if (part.id) call.id = part.id;
              if (fn.name && !call.name) call.name = fn.name;
              if (typeof fn.arguments === "string") call.arguments += fn.arguments;
            }
            if (choice.finish_reason) round.finish = choice.finish_reason;
          }
          if (timer === null) {
            timer = setTimeout(repaint, Math.min(400, 80 + (round.reasoning.length + round.content.length) / 400));
          }
        }
      }
    } finally {
      clearTimeout(timer);
    }
    paint(view, box, round, thinking);
    if (!ended) throw new TurnError("link", "응답이 중간에 끊겼습니다. 다시 시도해 주세요.");
    return box;
  }

  // ------------------------------------------------------------------------------------------- tool calls
  const clip = (text, size) => (text.length > size ? text.slice(0, size - 1) + "…" : text);
  const pretty = (text) => {
    const value = json(text);
    return value === null ? String(text || "") : JSON.stringify(value, null, 2);
  };

  function actionCard(view, box, action) {
    if (!action.id) return;
    if (!view.actions) view.actions = new Map();
    let card = view.actions.get(action.id);
    if (!card) {
      card = el("details", { class: "tool-card" });
      box.tools.append(card);
      view.actions.set(action.id, card);
    }
    const labels = { pending: "대기", running: "실행 중", success: "완료", error: "오류", cancelled: "중단됨" };
    card.dataset.state = action.state;
    card.classList.toggle("error", action.state === "error");
    const body = el("div", { class: "tool-body" });
    if (action.arguments && Object.keys(action.arguments).length) {
      body.append(el("div", { class: "tiny", text: "입력" }),
        el("pre", { text: clip(pretty(JSON.stringify(action.arguments)), 250000) }));
    }
    if (action.result !== undefined) {
      const items = action.result && action.result.items;
      if (Array.isArray(items)) {
        body.append(el("p", { class: "tiny", text: `조회 시각: ${action.result.retrieved_at || "알 수 없음"} · 자료/시세 기준 시각과 다를 수 있습니다.` }));
        if (!items.length) body.append(el("p", { text: "표시할 결과가 없습니다. 검색 성공이 자료의 존재나 실시간성을 보장하지 않습니다." }));
        for (const item of items.slice(0, 10)) {
          const entry = el("div", { class: "search-result" });
          entry.append(el("strong", { text: item.title || item.kind }));
          if (item.snippet) entry.append(el("p", { text: clip(item.snippet, 700) }));
          if (item.quote) {
            const q = item.quote;
            entry.append(el("p", { text: `${q.symbol || "종목 미상"} · ${q.price ?? "가격 미상"} ${q.currency || ""} · 거래소: ${q.exchange || "미제공"}` }),
              el("p", { class: "tiny", text: `기준: ${q.lastTradedAt || "미제공"} (${q.exchangeTimeZone || "시간대 미상"}) · 출처: ${q.primaryDataProvider || "미제공"} · 지연: ${q.isDelayed === true ? "지연" : q.isDelayed === false ? "비지연(제공자 표시)" : "미확인"}` }));
          }
          if (item.location) entry.append(el("p", { text: ["addressLine", "city", "region", "country"].map(k => item.location[k]).filter(Boolean).join(", ") || "주소 미제공" }));
          const date = item.publishedAt || item.datePublished || item.lastUpdatedAt;
          if (date) entry.append(el("p", { class: "tiny", text: `자료 시각: ${date}` }));
          if (item.media_notice) entry.append(el("p", { class: "tiny", text: "원본 출처 링크만 제공합니다. 이미지·동영상 사용권은 별도 확인하세요." }));
          if (typeof item.url === "string" && /^https?:\/\//i.test(item.url)) entry.append(el("a", { href: item.url, target: "_blank", rel: "noopener noreferrer", text: "출처 열기" }));
          body.append(entry);
        }
      }
      const resultText = typeof action.result === "string" ? action.result : JSON.stringify(action.result, null, 2);
      if (resultText && resultText.trim()) {
        if (Array.isArray(items)) body.append(el("details", null, el("summary", { text: "실제 반환 데이터" }), el("pre", { text: clip(resultText, 30000) })));
        else body.append(el("div", { class: "tiny", text: "결과" }), el("pre", { text: clip(resultText, 30000) }));
      }
      const artifact = action.result && action.result.artifact;
      if (artifact && artifact.kind !== "download") body.append(artifactButton(artifact, view.capability));
      const citations = action.result && action.result.citations;
      if (Array.isArray(citations)) {
        for (const source of citations.slice(0, 12)) {
          if (!source || typeof source.url !== "string" || !/^https?:\/\//i.test(source.url)) continue;
          body.append(el("p", null, el("a", { href: source.url, target: "_blank", rel: "noopener noreferrer",
            text: source.title || source.url })));
        }
      }
    }
    const summary = el("summary", null,
      el("span", { class: "tool-icon" }, icon(action.name)),
      el("span", { class: "tool-name", text: LABELS[action.name] || action.name }),
      el("span", { class: "tool-preview", text: `${labels[action.state] || action.state}${action.milliseconds !== undefined
        ? " · " + secs(action.milliseconds) : ""}` }));
    const artifact = action.state === "success" && action.result && action.result.artifact;
    if (artifact && artifact.kind === "download" && typeof artifact.path === "string") {
      summary.append(el("span", { class: "artifact-name", text: artifact.path.split("/").pop() }),
        artifactButton(artifact, view.capability));
    }
    card.replaceChildren(summary, body);
    if (isCurrent(view.owner) && nearBottom()) toBottom();
  }

  function finishActions(view) {
    for (const card of (view.actions || new Map()).values()) {
      if (!["pending", "running"].includes(card.dataset.state)) continue;
      card.dataset.state = view.outcome === "cancelled" ? "cancelled" : "error";
      card.classList.add("error");
      card.querySelector(".tool-preview").textContent = view.outcome === "cancelled"
        ? "중단됨" : "실행 종료 · 완료 결과 미수신";
    }
  }

  function artifactButton(artifact, capability) {
    return el("button", { type: "button", text: artifact.kind === "download" ? "다운로드" : "미리보기 열기",
      onclick: async (event) => {
        event.preventDefault();
        event.stopPropagation();
        const button = event.currentTarget;
        try {
          let source;
          if (artifact.kind === "chart") {
            source = artifact.svg;
          } else {
            const route = artifact.kind === "preview" ? "preview" : "download";
            const response = await fetch(capability.prefix + "/api/workspace/" + route + "?path=" + encodeURIComponent(artifact.path),
              { headers: { "X-AX-Workspace": capability.token } });
            if (!response.ok) throw new Error((await AX.problem(response)).message);
            if (artifact.kind === "download") {
              const url = URL.createObjectURL(await response.blob());
              const link = el("a", { href: url, download: artifact.path.split("/").pop() });
              link.click();
              setTimeout(() => URL.revokeObjectURL(url), 10000);
              return;
            }
            source = await response.text();
          }
          const dialog = el("dialog", { class: "artifact-dialog" });
          const frame = el("iframe", { title: "격리된 미리보기", sandbox: "" });
          // An opaque sandbox plus CSP blocks scripts, forms, network, navigation, and parent cookies.
          const policy = "default-src 'none'; script-src 'none'; style-src 'unsafe-inline'; " +
            "img-src data:; font-src 'none'; connect-src 'none'; form-action 'none'; base-uri 'none'";
          frame.srcdoc = `<meta http-equiv="Content-Security-Policy" content="${policy}">` + source;
          const close = () => { dialog.close(); dialog.remove(); };
          dialog.append(el("button", { type: "button", text: "닫기", onclick: close }), frame);
          dialog.addEventListener("cancel", close);
          document.body.append(dialog);
          dialog.showModal();
        } catch (error) {
          button.after(el("p", { class: "error", text: error.message }));
        }
      } });
  }

  // ---------------------------------------------------------------------------------------- the footer
  // Decode speed = (output tokens - 1) / (last chunk - first chunk), summed over the model calls of the turn.
  function account(timing, round) {
    const tokens = Number(round.usage && round.usage.completion_tokens);
    if (!Number.isFinite(tokens)) return;
    timing.tokens += tokens;
    if (tokens > 1 && round.first !== null && round.last > round.first) {
      timing.decodeTokens += tokens - 1;
      timing.decodeMs += round.last - round.first;
    }
  }

  function footer(view, timing, answer) {
    const m = view.metrics || {};
    const value = (number, digits = 0) => Number.isFinite(number) ? AX.num(number, digits) : "미제공";
    const seconds = number => Number.isFinite(number) ? value(number, 2) + "초" : "미제공";
    const parts = [`TTFT ${seconds(m.ttft_seconds)}`, `전체 ${seconds(m.total_seconds)}`,
      `입력 ${value(m.input_tokens)} / 출력 ${value(m.output_tokens)} 토큰`,
      `출력 ${value(m.output_tokens_per_second, 1)} tok/s`,
      ({ completed: "완료", error: "오류", cancelled: "중단됨" })[view.outcome] || "종료 상태 미제공"];
    const meta = el("span", { class: "meta metrics", text: parts.join(" · "),
      title: "서버 실측. TTFT는 요청 처리 시작부터 첫 답변 텍스트까지(대기·문서 판독·추론 포함). 토큰은 모델 호출별 실제 usage 합계. 출력률은 전체 모델 호출 시간(대기·prefill 포함) 기준이며 순수 decode 속도가 아닙니다." });
    if (!answer) {
      view.foot.replaceChildren(meta);
      return;
    }
    const copy = el("button", { type: "button", class: "icon-action", title: "복사", "aria-label": "답변 복사" }, icon("copy"));
    copy.addEventListener("click", async () => {
      let ok = true;
      try {
        await navigator.clipboard.writeText(answer);
      } catch (error) {
        ok = false;
      }
      copy.replaceChildren(icon(ok ? "check" : "cross"));
      setTimeout(() => copy.replaceChildren(icon("copy")), 1500);
    });
    view.foot.replaceChildren(copy, meta);
  }

  // --------------------------------------------------------------------------------------------- a turn
  const bytes = (text) => new TextEncoder().encode(text).length;

  // One user turn; MAF owns bounded model/tool rounds on the server.
  async function send(owner = state) {
    const state = owner;
    if (state.mode === "compare") return sendComparison(state);
    if (state.busy || state.uploading) return;
    if (state.power !== "ready") {
      flash("모델 서버가 아직 준비되지 않았습니다.");
      return;
    }
    const files = state.attachments.slice();
    const typed = ui.input.value.trim();
    if (!typed && !files.length) return;
    const question = typed || "첨부한 문서를 요약해 줘.";
    const thinking = ui.thinking.checked;
    const useTools = ui.tools.checked;
    const turnStart = state.history.length;
    state.history.push({ role: "user", content: question });
    if (state.history.length > 360 ||
        bytes(JSON.stringify({ messages: state.history, thinking, tools: useTools })) > BODY_LIMIT) {
      state.history.length = turnStart;
      flash(MESSAGES.too_large);
      return;
    }
    ui.input.value = "";
    ui.grow();
    state.attachments = [];
    renderAttachments();
    AX.show(ui.welcome, false);
    const userNode = userBubble(question, files, state.panel);
    const view = assistantBubble(state.panel, state);
    toBottom();
    const epoch = state.epoch;
    state.busy = true;
    state.controller = new AbortController();
    updateControls();

    const timing = { wait: 0, ttft: null, tokens: 0, decodeTokens: 0, decodeMs: 0, calls: 0 };
    let last = null;
    let answer = "";
    let failure = null;
    try {
      const payload = JSON.stringify({ messages: state.history, thinking, tools: useTools,
                                       attachments: files.flatMap(file => file.files) });
      if (state.history.length > 400 || bytes(payload) > BODY_LIMIT) throw new TurnError("too_large", MESSAGES.too_large);
      const round = { reasoning: "", content: "", calls: [], finish: null, usage: null, first: null, last: null };
      last = { round, box: roundBox(view) };
      timing.calls += 1;
      last.box = await streamRound(payload, view, last.box, round, thinking, timing);
      if (epoch !== state.epoch) return;
      account(timing, round);
      const { content } = visible(round, thinking);
      if (!content.trim()) {
        throw new TurnError("empty", round.finish !== "length" ? "모델이 빈 답변을 보냈습니다. 다시 시도해 주세요."
          : thinking ? "생각이 길어져 답변을 쓰기 전에 출력 한도에 도달했습니다. 추론 모드를 끄고 다시 시도해 보세요."
            : "답변을 쓰기 전에 출력 한도에 도달했습니다.");
      }
      answer = content;
      state.history.push({ role: "assistant", content });
      if (round.finish === "length") note(view, "출력 길이 한도에 도달해 답변이 여기서 잘렸습니다.", false);
    } catch (error) {
      failure = error;
    }
    if (epoch !== state.epoch) return;  // "새 대화" was pressed during the turn
    state.busy = false;
    state.controller = null;
    view.status.remove();
    view.outcome = failure ? aborted(failure) ? "cancelled" : "error" : "completed";
    if (failure) {
      const stopped = aborted(failure);
      if (!stopped && !(failure instanceof TurnError)) console.error(failure);
      if (last) paint(view, view.currentBox || last.box, last.round, thinking);
      const partial = last ? visible(last.round, thinking).content : "";
      if (partial.trim()) {
        answer = partial;
        state.history.push({ role: "assistant", content: partial });
        note(view, stopped ? "(중단됨)" : `답변이 중간에 끊겼습니다. ${failure.message}`, !stopped, failure.detail);
      } else {
        state.history.push({ role: "assistant", content: stopped
          ? "이 턴은 중단됐습니다. 실제 완료된 도구 결과와 파일만 유지되며, 백그라운드 작업은 없습니다."
          : "이 턴은 오류로 종료됐습니다. 실제 완료된 도구 결과와 파일만 유지됩니다." });
        view.node.classList.add("failed");
        note(view, stopped ? "중단했습니다. 이미 완료된 결과와 파일은 현재 대화에 유지됩니다. 백그라운드 작업은 없습니다." : failure.message || "오류가 발생했습니다.",
             !stopped, failure.detail);
        if (isCurrent(owner) && !ui.input.value.trim() && !state.attachments.length) {
          ui.input.value = typed;
          ui.grow();
          state.attachments = files;
          renderAttachments();
        }
      }
    }
    finishActions(view);
    const stick = nearBottom();
    footer(view, timing, answer);
    updateControls();
    if (stick && isCurrent(owner)) toBottom();
  }

  async function sendComparison(owner) {
    if (owner.busy || owner.uploading) return;
    if (owner.power !== "ready") return flash("준비된 모델이 없습니다.");
    const typed = ui.input.value.trim();
    const files = owner.attachments.slice();
    if (!typed && !files.length) return;
    const question = typed || "첨부한 문서를 요약해 줘.";
    const thinking = ui.thinking.checked;
    const raw = { messages: [{ role: "user", content: question }], thinking, tools: ui.tools.checked,
      attachments: files.flatMap(file => file.files), previous: owner.previous,
      mode: ui.contextMode.value, context: ui.commonContext.value,
      max_tokens: Number(ui.outputLimit.value),
      generation: { temperature: Number(ui.temperature.value), top_p: Number(ui.topP.value) } };
    const epoch = owner.epoch;
    owner.busy = true;
    owner.controller = new AbortController();
    ui.input.value = "";
    ui.grow();
    AX.show(owner.welcome, false);
    userBubble(question, files, owner.panel);
    const condition = el("details", { class: "turn-condition" },
      el("summary", { text: "입력 조건 확인" }));
    const pair = el("div", { class: "compare-turn" });
    owner.panel.append(condition, pair);
    const views = new Map();
    for (const model of ["fp8", "nvfp4"]) {
      const view = assistantBubble(pair, owner);
      view.node.querySelector(".bubble").prepend(el("div", { class: "model-heading",
        text: model === "fp8" ? "A.X K2 FP8 · 16 A100" : "A.X K2 NVFP4 · 8 A100" }));
      views.set(model, view);
    }
    updateControls();
    toBottom();
    try {
      const token = await workspace(owner);
      if (epoch !== owner.epoch) return;
      const response = await fetch("/api/compare", { method: "POST", signal: owner.controller.signal,
        headers: { "Content-Type": "application/json", "X-AX-Workspace": token }, body: JSON.stringify(raw) });
      if (!response.ok) throw new Error((await AX.problem(response)).message);
      const run = await response.json();
      if (epoch !== owner.epoch) return;
      owner.previous = run.id;
      owner.attachments = [];
      renderAttachments(owner);
      condition.append(el("p", { text: run.input_difference
        ? "각자 대화 이어가기: 이전 답변·도구 결과·파일 변경이 달라 입력 문맥도 다릅니다."
        : "서버가 질문·파일 스냅샷·시각·공통 문맥·생성 조건을 양쪽에 동일하게 고정했습니다." }),
        el("p", { text: raw.tools ? "실제 에이전트 비교: 도구 호출과 결과는 모델별로 달라질 수 있습니다." : "도구 없음 · 실제 모델 응답 비교" }),
        el("pre", { text: JSON.stringify(run.snapshot, null, 2) }));
      await Promise.all(run.actors.map(async actor => {
        const view = views.get(actor.model);
        if (!view) throw new Error("서버가 지원하지 않는 비교 모델을 반환했습니다.");
        const timing = { wait: 0, ttft: null, tokens: 0, decodeTokens: 0, decodeMs: 0, calls: 0 };
        const round = { reasoning: "", content: "", calls: [], finish: null, usage: null, first: null, last: null };
        let box = roundBox(view);
        let answer = "";
        try {
          box = await streamRound("{}", view, box, round, thinking, timing, { url: actor.stream,
            requestToken: token, artifactPrefix: profilePrefix(actor.model), artifactToken: actor.workspace });
          answer = visible(round, thinking).content;
          if (!answer.trim()) throw new Error("모델이 답변 텍스트를 제공하지 않았습니다.");
          view.outcome = "completed";
          if (round.finish === "length") note(view, "출력 상한에 도달했습니다.", false);
        } catch (error) {
          view.outcome = aborted(error) ? "cancelled" : "error";
          answer = visible(round, thinking).content;
          paint(view, view.currentBox || box, round, thinking);
          note(view, aborted(error) ? "이 모델 응답은 중단됐습니다. 백그라운드 작업은 없습니다." : error.message, !aborted(error));
        } finally {
          finishActions(view);
          view.status.remove();
          footer(view, timing, answer);
        }
      }));
    } catch (error) {
      for (const view of views.values()) {
        view.outcome = aborted(error) ? "cancelled" : "error";
        view.status.remove();
        note(view, aborted(error) ? "비교 요청이 중단됐습니다." : error.message, !aborted(error));
        footer(view, {}, "");
      }
    } finally {
      if (epoch === owner.epoch) {
        owner.busy = false;
        owner.controller = null;
        updateControls();
      }
    }
  }

  function switchTab(mode) {
    state.draft = ui.input.value;
    state.tools = ui.tools.checked;
    state.thinking = ui.thinking.checked;
    state.scroll = ui.messages.scrollTop;
    state = sessions[mode];
    for (const [name, owner] of Object.entries(sessions)) {
      AX.show(owner.panel, name === mode);
      owner.welcome.removeAttribute("id");
      owner.welcome.querySelector(".suggestions").removeAttribute("id");
    }
    ui.welcome = state.welcome;
    ui.welcome.id = "welcome";
    ui.welcome.querySelector(".suggestions").id = "examples";
    ui.input.value = state.draft;
    ui.tools.checked = state.tools;
    ui.thinking.checked = state.thinking;
    ui.grow();
    ui.chatTab.setAttribute("aria-selected", String(mode === "chat"));
    ui.compareTab.setAttribute("aria-selected", String(mode === "compare"));
    AX.show(ui.modelSelect, mode === "chat");
    ui.modelScope.classList.toggle("inactive", mode !== "chat");
    AX.show(ui.conditions, mode === "compare");
    renderAttachments();
    updateControls();
    ui.messages.scrollTop = state.scroll;
    if (state.notice) {
      flash(state.notice);
      state.notice = null;
    }
    window.dispatchEvent(new CustomEvent("ax:view-change"));
  }

  function reset(owner = state) {
    const state = owner;
    state.epoch += 1;
    if (state.controller) state.controller.abort();
    releaseWorkspace("close", state.workspace, state);
    state.controller = null;
    state.busy = false;
    state.uploading = false;
    state.notice = null;
    state.history = [];
    state.workspace = null;
    state.workspacePromise = null;
    state.attachments = [];
    state.previous = null;
    state.draft = "";
    state.panel.replaceChildren(state.welcome);
    AX.show(state.welcome, true);
    if (isCurrent(owner)) {
      ui.input.value = "";
      ui.grow();
      renderAttachments(state);
    }
    updateControls();
    ui.input.focus();
  }

  function setPower(power) {
    if (power === state.power) return;
    state.power = power;
    updateControls();
  }

  function setModels(value) {
    models = value;
    const contextLimits = models.map(model => model.runtime?.max_context_tokens);
    const commonLimit = contextLimits.length === 2 && contextLimits.every(value => Number.isInteger(value) && value > 0)
      ? Math.min(...contextLimits) : null;
    document.getElementById("context-limits").textContent = models.map(model =>
      `${model.label}: ${Number.isInteger(model.runtime?.max_context_tokens) ? AX.num(model.runtime.max_context_tokens) + " tokens" : "미제공"}`
    ).join(" · ") + ` · 공통 문맥 한도: ${commonLimit === null ? "미제공" : AX.num(commonLimit) + " tokens"}`
      + " (입력·출력·도구 문맥 포함, 실제 토큰화 한도 초과는 엔진이 명시적으로 거부)";
    sessions.chat.power = (models.find(model => model.id === sessions.chat.model) || {}).status?.power || "unknown";
    sessions.compare.power = models.some(model => model.status.power === "ready") ? "ready"
      : (models.find(model => model.status.desired === "on") || models[0])?.status.power || "unknown";
    updateControls();
  }

  function status() {
    if (state.mode === "chat") return (models.find(model => model.id === state.model) || {}).status || { power: "unknown" };
    const ready = models.find(model => model.status.power === "ready");
    return (ready || models.find(model => model.status.desired === "on") || models[0])?.status || { power: "unknown" };
  }

  function init() {
    for (const id of ["messages", "welcome", "examples", "composer", "attachments", "input", "thinking", "tools", "file",
                      "new-chat", "hint", "stop", "send", "chat-tab", "compare-tab", "model-select", "model-scope", "conditions",
                      "context-mode", "common-context", "output-limit", "temperature", "top-p"]) {
      ui[id.replace(/-(\w)/g, (match, letter) => letter.toUpperCase())] = document.getElementById(id);
    }
    const originalWelcome = ui.welcome;
    for (const owner of Object.values(sessions)) {
      owner.panel = el("section", { class: "timeline" });
      owner.welcome = owner.mode === "chat" ? originalWelcome : originalWelcome.cloneNode(true);
      owner.welcome.removeAttribute("id");
      const examples = owner.welcome.querySelector(".suggestions");
      examples.removeAttribute("id");
      owner.panel.append(owner.welcome);
      ui.messages.append(owner.panel);
      examples.replaceChildren(...EXAMPLES.map((example) => el("button", {
      type: "button", class: "suggestion", onclick: () => {
        if (state.busy) return;
        ui.input.value = example.text;
        if (example.tools) ui.tools.checked = true;
        if (state.power === "ready") send();
        else ui.input.focus();
      } }, el("strong", { text: example.label }), el("span", { text: example.text }))));
    }
    const grow = () => {
      ui.input.style.height = "auto";
      ui.input.style.height = `${Math.min(ui.input.scrollHeight, 220)}px`;
    };
    ui.input.addEventListener("input", grow);
    ui.grow = grow;
    ui.composer.addEventListener("submit", (event) => {
      event.preventDefault();
      send();
    });
    ui.input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
        event.preventDefault();
        send();
      }
    });
    ui.stop.addEventListener("click", () => {
      releaseWorkspace("cancel");
      if (state.controller) state.controller.abort();
    });
    ui.newChat.addEventListener("click", () => reset());
    ui.chatTab.addEventListener("click", () => switchTab("chat"));
    ui.compareTab.addEventListener("click", () => switchTab("compare"));
    ui.modelSelect.addEventListener("change", () => {
      reset(sessions.chat);
      sessions.chat.model = ui.modelSelect.value;
      sessions.chat.prefix = profilePrefix(sessions.chat.model);
      setModels(models);
      window.dispatchEvent(new CustomEvent("ax:view-change"));
    });
    ui.contextMode.addEventListener("change", () => {
      document.getElementById("condition-note").textContent = ui.contextMode.value === "continue"
        ? "각자 이어가기는 두 번째 턴부터 각 모델의 실제 답변·도구 결과로 입력 문맥이 달라집니다."
        : "독립 질문과 고정 공통 문맥은 이전 모델 답변을 넣지 않습니다. 실제 도구 결과는 달라질 수 있습니다.";
    });
    ui.file.addEventListener("change", () => {
      addFiles(ui.file.files).finally(() => {
        ui.file.value = "";
      });
    });
    ui.composer.addEventListener("dragover", (event) => {
      event.preventDefault();
      ui.composer.classList.add("drop");
    });
    ui.composer.addEventListener("dragleave", () => ui.composer.classList.remove("drop"));
    ui.composer.addEventListener("drop", (event) => {
      event.preventDefault();
      ui.composer.classList.remove("drop");
      addFiles(event.dataTransfer && event.dataTransfer.files);
    });
    renderAttachments();
    updateControls();
    ui.tools.checked = state.tools;
    ui.thinking.checked = state.thinking;
    switchTab("compare");
    fetch("/api/capabilities").then(async (response) => {
      if (!response.ok) throw new Error((await AX.problem(response)).message);
      const capabilities = await response.json();
      ui.tools.title = capabilities.web_iq.state === "ready"
        ? `문서·파일·데이터 도구 / Web IQ: ${(capabilities.web_iq.tools || []).join(", ")} · 공개 검색 (첨부 내용 자동 전송 금지)`
        : "문서·파일·데이터 도구 / Web IQ 미구성: 기업용 엔드포인트·계약·접근 설정 필요";
    }).catch((error) => {
      ui.tools.title = "도구 구성 조회 실패: " + error.message;
    });
  }

  AX.chat = { init, setPower, setModels, status, reset, busy: () => state.busy };
  init();  // scripts are deferred, so the page is parsed
})();
