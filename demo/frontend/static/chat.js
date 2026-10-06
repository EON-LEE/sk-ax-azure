"use strict";
// Chat tab: streams /api/chat (server-sent events read through fetch), shows the reasoning and the answer,
// runs the tools in the browser and keeps the conversation only in this tab's memory.
(function () {
  const AX = window.AX;
  const { el } = AX;

  const MAX_TOOL_ROUNDS = 4;           // after these, one last request without tools forces an answer
  const BODY_LIMIT = 4 * 1024 * 1024;  // the server's request size limit
  const FILE_BYTES = 8 * 1024 * 1024;
  const FILE_CHARS = 800000;
  const TOTAL_CHARS = 1000000;
  const REASONING_LIMIT = 200000;
  const ARGS_LIMIT = 4096;
  const TOOL_ID = /^[A-Za-z0-9_.:-]{1,64}$/;

  const EXAMPLES = [
    { label: "모델 소개", text: "A.X K2가 어떤 모델인지 세 문장으로 소개해 줘." },
    { label: "추론 문제 풀기", text: "철수는 영희보다 사과를 3개 더 갖고 있고, 둘이 가진 사과는 모두 17개야. 각자 몇 개씩 갖고 있을까?" },
    { label: "코드 작성", text: "파이썬으로 이진 탐색 함수를 작성하고 시간 복잡도를 설명해 줘." },
    { label: "도구로 정확히 계산", text: "2의 64제곱에서 1을 뺀 값을 계산기로 정확히 구해 줘.", tools: true },
    { label: "세계 시각 확인", text: "지금 서울과 런던은 각각 몇 시야?", tools: true },
    { label: "날씨와 옷차림", text: "부산 날씨를 알려 주고 옷차림을 추천해 줘. (날씨는 데모용 가짜 데이터)", tools: true },
  ];

  const SVG = "http://www.w3.org/2000/svg";
  const ICONS = {
    calculator: "M7 3h10a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2zM8 7h8M8 12h.01M12 12h.01M16 12h.01M8 16h.01M12 16h.01M16 16h.01",
    get_current_time: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
    get_weather: "M17.5 19H7a5 5 0 1 1 1-9.9A6 6 0 0 1 19.5 11 4 4 0 0 1 17.5 19z",
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
  const state = { history: [], busy: false, controller: null, attachments: [], power: "unknown", epoch: 0 };

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
  const randomId = () => "call_" + Array.from(crypto.getRandomValues(new Uint8Array(8)),
    (byte) => byte.toString(16).padStart(2, "0")).join("");

  // ----------------------------------------------------------------------------------- controls and hint
  let flashTimer = null;

  function readyHint() {
    clearTimeout(flashTimer);
    ui.hint.classList.remove("error");
    if (state.busy || state.power === "ready" || state.power === "unknown") ui.hint.textContent = "";
    else ui.hint.textContent = "모델 서버가 준비되면 보낼 수 있습니다";
  }

  function flash(message) {
    readyHint();
    ui.hint.textContent = message;
    ui.hint.classList.add("error");
    flashTimer = setTimeout(readyHint, 6000);
  }

  function updateControls() {
    ui.send.disabled = state.busy || state.power !== "ready";
    AX.show(ui.send, !state.busy);
    AX.show(ui.stop, state.busy);
    readyHint();
  }

  // ---------------------------------------------------------------------------------------- attachments
  async function decode(file) {
    const buffer = await file.arrayBuffer();
    let text = new TextDecoder("utf-8").decode(buffer);
    if (text.includes("\uFFFD")) {
      try {
        const korean = new TextDecoder("euc-kr").decode(buffer);
        if (!korean.includes("\uFFFD")) text = korean;
      } catch (error) {
        // keep the UTF-8 reading
      }
    }
    return text.replace(/\r\n?/g, "\n");
  }

  async function addFiles(list) {
    for (const file of Array.from(list || [])) {
      if (!/\.(txt|md|markdown)$/i.test(file.name)) {
        flash(`${file.name}: .txt 또는 .md 파일만 첨부할 수 있습니다.`);
        continue;
      }
      if (file.size > FILE_BYTES) {
        flash(`${file.name}: 파일이 너무 큽니다.`);
        continue;
      }
      let text;
      try {
        text = await decode(file);
      } catch (error) {
        flash(`${file.name}: 파일을 읽지 못했습니다.`);
        continue;
      }
      const used = state.attachments.reduce((sum, item) => sum + item.text.length, 0);
      if (!text.trim()) flash(`${file.name}: 빈 파일입니다.`);
      else if (text.length > FILE_CHARS) flash(`${file.name}: 파일 하나는 ${AX.num(FILE_CHARS)}자까지 첨부할 수 있습니다.`);
      else if (used + text.length > TOTAL_CHARS) flash(`첨부 문서는 모두 합쳐 ${AX.num(TOTAL_CHARS)}자까지입니다.`);
      else state.attachments.push({ name: file.name, text });
    }
    renderAttachments();
  }

  function renderAttachments() {
    ui.attachments.replaceChildren(...state.attachments.map((item, index) => el("span", { class: "chip file" },
      `${item.name} · ${AX.num(item.text.length)}자`,
      el("button", { type: "button", class: "remove", title: "첨부 취소", "aria-label": `${item.name} 첨부 취소`,
                     text: "×", onclick: () => {
                       state.attachments.splice(index, 1);
                       renderAttachments();
                     } }))));
    AX.show(ui.attachments, state.attachments.length > 0);
  }

  // -------------------------------------------------------------------------------------------- bubbles
  function userBubble(text, files) {
    const node = el("div", { class: "msg user" }, el("div", { class: "bubble" },
      files.length ? el("div", { class: "chips" }, files.map((file) =>
        el("span", { class: "chip file", text: `${file.name} · ${AX.num(file.text.length)}자` }))) : null,
      el("div", { class: "plain", text })));
    ui.messages.append(node);
    return node;
  }

  function assistantBubble() {
    const rounds = el("div", { class: "rounds" });
    const status = el("div", { class: "status", text: "요청을 보내는 중" });
    const foot = el("div", { class: "foot" });
    const node = el("div", { class: "msg assistant" }, el("div", { class: "bubble" }, rounds, status, foot));
    ui.messages.append(node);
    return { node, rounds, status, foot };
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
    const stick = nearBottom();
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
      if (call) view.status.textContent = `${AX.tools.labels[call.name] || "도구"} 호출 준비 중`;
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
  async function streamRound(payload, view, box, round, thinking, timing) {
    const requested = performance.now();
    let response;
    try {
      response = await fetch("/api/chat", { method: "POST", credentials: "same-origin", body: payload,
                                            headers: { "Content-Type": "application/json" },
                                            signal: state.controller.signal });
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
        if (event === "queue") {
          const info = json(data) || {};
          view.status.textContent = info.position ? `대기열 ${AX.num(info.position)}번째 · 곧 시작합니다` : "대기 중";
        } else if (event === "start") {
          started = performance.now();
          timing.wait += started - requested;
          view.status.textContent = "입력을 읽는 중";
        } else if (event === "end") {
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
  }

  // ------------------------------------------------------------------------------------------- tool calls
  // The calls that can go back to the server: known names, unique valid ids, at most 8 per message.
  function sanitizeCalls(round) {
    const calls = [];
    const rejected = [];
    const seen = new Set();
    for (const call of round.calls.filter(Boolean)) {
      if (!AX.tools.names.has(call.name)) {
        rejected.push({ call, reason: `알 수 없는 도구입니다: ${call.name || "(이름 없음)"}` });
      } else if (calls.length >= 8) {
        rejected.push({ call, reason: "한 번에 실행할 수 있는 도구는 8개까지입니다." });
      } else {
        const id = TOOL_ID.test(call.id) && !seen.has(call.id) ? call.id : randomId();
        seen.add(id);
        calls.push({ id, name: call.name, arguments: call.arguments });
      }
    }
    return { calls, rejected };
  }

  const clip = (text, size) => (text.length > size ? text.slice(0, size - 1) + "…" : text);
  const pretty = (text) => {
    const value = json(text);
    return value === null ? String(text || "") : JSON.stringify(value, null, 2);
  };

  function preview(name, result) {
    if (!result || result.error) return `오류: ${result ? result.error : "결과 없음"}`;
    if (name === "calculator") return `${result.expression} = ${result.result}`;
    if (name === "get_current_time") return `${result.timezone} ${result.date} ${result.time} (${result.weekday_ko})`;
    if (name === "get_weather") return `${result.city}: ${result.condition}, ${result.temperature_c}°C (가짜 데이터)`;
    return "";
  }

  function toolCard(box, name, args, result, ok) {
    box.tools.append(el("details", { class: ok ? "tool-card" : "tool-card error" },
      el("summary", null, el("span", { class: "tool-icon" }, icon(name)),
         el("span", { class: "tool-name", text: AX.tools.labels[name] || name || "알 수 없는 도구" }),
         el("span", { class: "tool-preview", text: clip(preview(name, result), 160) }),
         el("span", { class: "tool-state", title: ok ? "완료" : "실패" }, icon(ok ? "check" : "cross"))),
      el("div", { class: "tool-body" },
         el("div", { class: "tiny", text: "입력" }), el("pre", { text: clip(pretty(args), 5000) }),
         el("div", { class: "tiny", text: "결과" }), el("pre", { text: JSON.stringify(result, null, 2) }))));
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
    const parts = [];
    if (timing.wait >= 1000) parts.push(`대기 ${secs(timing.wait)}`);
    if (timing.ttft !== null) parts.push(`첫 토큰 ${secs(timing.ttft)}`);
    if (timing.tokens) parts.push(`출력 ${AX.num(timing.tokens)} 토큰`);
    if (timing.decodeMs > 0) parts.push(`초당 ${AX.num(timing.decodeTokens / (timing.decodeMs / 1000), 1)} 토큰`);
    if (timing.calls > 1) parts.push(`모델 호출 ${timing.calls}회`);
    const meta = el("span", { class: "meta", text: parts.join(" · ") });
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
  function compose(question, files) {
    if (!files.length) return question;
    const docs = files.map((file) => `<document name="${file.name.replace(/["<>]/g, "_")}">\n${file.text}\n</document>`);
    return `${docs.join("\n\n")}\n\n${question}`;
  }

  const bytes = (text) => new TextEncoder().encode(text).length;

  // One user turn: up to MAX_TOOL_ROUNDS model calls that may use tools, then one that must answer.
  async function send() {
    if (state.busy) return;
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
    state.history.push({ role: "user", content: compose(question, files) });
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
    const userNode = userBubble(question, files);
    const view = assistantBubble();
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
      let noTools = !useTools;
      for (let index = 0; ; index += 1) {
        const withTools = !noTools && index < MAX_TOOL_ROUNDS;
        const payload = JSON.stringify({ messages: state.history, thinking, tools: withTools });
        if (state.history.length > 400 || bytes(payload) > BODY_LIMIT) throw new TurnError("too_large", MESSAGES.too_large);
        const round = { reasoning: "", content: "", calls: [], finish: null, usage: null, first: null, last: null };
        last = { round, box: roundBox(view) };
        timing.calls += 1;
        await streamRound(payload, view, last.box, round, thinking, timing);
        if (epoch !== state.epoch) return;
        account(timing, round);
        const { reasoning, content } = visible(round, thinking);
        const { calls, rejected } = withTools ? sanitizeCalls(round) : { calls: [], rejected: [] };
        for (const { call, reason } of rejected) toolCard(last.box, call.name, call.arguments, { error: reason }, false);
        if (calls.length) {
          const message = { role: "assistant", content, tool_calls: calls.map((call) => ({
            id: call.id, type: "function",
            function: { name: call.name, arguments: call.arguments.length > ARGS_LIMIT ? "{}" : call.arguments } })) };
          if (reasoning) message.reasoning_content = reasoning.slice(0, REASONING_LIMIT);
          state.history.push(message);
          for (const call of calls) {
            const outcome = call.arguments.length > ARGS_LIMIT
              ? { ok: false, result: { error: "도구 인자가 너무 깁니다." } } : AX.tools.run(call.name, call.arguments);
            state.history.push({ role: "tool", tool_call_id: call.id, content: JSON.stringify(outcome.result) });
            toolCard(last.box, call.name, call.arguments, outcome.result, outcome.ok);
          }
          view.status.textContent = "도구 결과를 모델에 전달하는 중…";
          continue;
        }
        if (rejected.length && !content.trim()) {
          noTools = true;  // the model asked only for tools that do not exist: ask once more without tools
          continue;
        }
        if (!content.trim()) {
          throw new TurnError("empty", round.finish !== "length" ? "모델이 빈 답변을 보냈습니다. 다시 시도해 주세요."
            : thinking ? "생각이 길어져 답변을 쓰기 전에 출력 한도에 도달했습니다. 추론 모드를 끄고 다시 시도해 보세요."
              : "답변을 쓰기 전에 출력 한도에 도달했습니다.");
        }
        answer = content;
        state.history.push({ role: "assistant", content });
        if (round.finish === "length") note(view, "출력 길이 한도에 도달해 답변이 여기서 잘렸습니다.", false);
        break;
      }
    } catch (error) {
      failure = error;
    }
    if (epoch !== state.epoch) return;  // "새 대화" was pressed during the turn
    state.busy = false;
    state.controller = null;
    view.status.remove();
    if (failure) {
      const stopped = aborted(failure);
      if (!stopped && !(failure instanceof TurnError)) console.error(failure);
      if (last) paint(view, last.box, last.round, thinking);
      const partial = last ? visible(last.round, thinking).content : "";
      if (partial.trim()) {
        answer = partial;
        state.history.push({ role: "assistant", content: partial });
        note(view, stopped ? "(중단됨)" : `답변이 중간에 끊겼습니다. ${failure.message}`, !stopped, failure.detail);
      } else {
        state.history.length = turnStart;
        userNode.classList.add("failed");
        view.node.classList.add("failed");
        note(view, stopped ? "중단했습니다. 이 질문은 대화 기록에서 뺐습니다." : failure.message || "오류가 발생했습니다.",
             !stopped, failure.detail);
        if (!ui.input.value.trim() && !state.attachments.length) {
          ui.input.value = typed;
          ui.grow();
          state.attachments = files;
          renderAttachments();
        }
      }
    }
    const stick = nearBottom();
    footer(view, timing, answer);
    updateControls();
    if (stick) toBottom();
  }

  function reset() {
    state.epoch += 1;
    if (state.controller) state.controller.abort();
    state.controller = null;
    state.busy = false;
    state.history = [];
    ui.messages.replaceChildren(ui.welcome);
    AX.show(ui.welcome, true);
    updateControls();
    ui.input.focus();
  }

  function setPower(power) {
    if (power === state.power) return;
    state.power = power;
    updateControls();
  }

  function init() {
    for (const id of ["messages", "welcome", "examples", "composer", "attachments", "input", "thinking", "tools", "file",
                      "new-chat", "hint", "stop", "send"]) {
      ui[id.replace(/-(\w)/g, (match, letter) => letter.toUpperCase())] = document.getElementById(id);
    }
    ui.examples.replaceChildren(...EXAMPLES.map((example) => el("button", {
      type: "button", class: "suggestion", onclick: () => {
        if (state.busy) return;
        ui.input.value = example.text;
        if (example.tools) ui.tools.checked = true;
        if (state.power === "ready") send();
        else ui.input.focus();
      } }, el("strong", { text: example.label }), el("span", { text: example.text }))));
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
      if (state.controller) state.controller.abort();
    });
    ui.newChat.addEventListener("click", reset);
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
  }

  AX.chat = { init, setPower, reset, busy: () => state.busy };
  init();  // scripts are deferred, so the page is parsed
})();
