"use strict";
// The three demo tools run here in the browser, never on the server: an arithmetic parser (no eval; exact
// with BigInt when the expression is integer-only), the current time from Intl, and a deterministic fake
// weather report.
(function () {
  const AX = window.AX;

  // ---------------------------------------------------------------------------------------- calculator
  const FUNCS = { sqrt: Math.sqrt, abs: Math.abs, sin: Math.sin, cos: Math.cos, tan: Math.tan, asin: Math.asin,
                  acos: Math.acos, atan: Math.atan, log: Math.log10, log10: Math.log10, log2: Math.log2,
                  ln: Math.log, exp: Math.exp, floor: Math.floor, ceil: Math.ceil, round: Math.round };
  const CONSTS = { pi: Math.PI, e: Math.E };
  const has = (object, key) => Object.prototype.hasOwnProperty.call(object, key);
  const NOT_EXACT = new Error("not exact");
  const MAX_BITS = 3400;  // about 1,000 decimal digits

  function tokenize(text) {
    const src = text.replace(/\*\*/g, "^").replace(/[×✕✖·]/g, "*").replace(/÷/g, "/").replace(/[−–]/g, "-")
      .replace(/π/g, "pi").replace(/√/g, "sqrt");
    const re = /\s+|(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+\.?\d*|\.\d+)([eE][+-]?\d+)?|([A-Za-z_][A-Za-z_0-9]*)|([-+*/%^()!])/y;
    const tokens = [];
    let i = 0;
    while (i < src.length) {
      re.lastIndex = i;
      const m = re.exec(src);
      if (!m) throw new Error(`식에 쓸 수 없는 문자가 있습니다: ${src[i]}`);
      i = re.lastIndex;
      if (m[1] !== undefined) {
        const raw = m[1].replace(/,/g, "") + (m[2] || "");
        tokens.push({ type: "num", value: Number(raw), raw });
      } else if (m[3] !== undefined) {
        tokens.push({ type: "name", value: m[3].toLowerCase() });
      } else if (m[4] !== undefined) {
        tokens.push({ type: "op", value: m[4] });
      }
    }
    return tokens;
  }

  // expr := term (+|- term)*; term := unary (*|/|% unary | implicit *)*; unary := -unary | power;
  // power := postfix (^ unary)?  (so -2^2 = -4 and 2^3^2 = 512); postfix := primary !*
  function parse(text) {
    const tokens = tokenize(text);
    let pos = 0;
    let depth = 0;
    const peek = () => tokens[pos];
    const isOp = (value) => !!peek() && peek().type === "op" && peek().value === value;
    const take = (value) => {
      if (!isOp(value)) throw new Error(`'${value}'가 필요합니다.`);
      pos++;
    };
    const nest = (fn) => {
      if (++depth > 200) throw new Error("식이 너무 깊게 중첩되어 있습니다.");
      const node = fn();
      depth--;
      return node;
    };
    const expr = () => nest(() => {
      let node = term();
      while (isOp("+") || isOp("-")) node = { k: "bin", op: tokens[pos++].value, a: node, b: term() };
      return node;
    });
    const term = () => {
      let node = unary();
      for (;;) {
        if (isOp("*") || isOp("/") || isOp("%")) node = { k: "bin", op: tokens[pos++].value, a: node, b: unary() };
        else if (isOp("(") || (peek() && peek().type === "name")) node = { k: "bin", op: "*", a: node, b: unary() };
        else return node;
      }
    };
    const unary = () => nest(() => {
      if (isOp("-")) {
        pos++;
        return { k: "neg", a: unary() };
      }
      if (isOp("+")) {
        pos++;
        return unary();
      }
      const base = postfix();
      if (!isOp("^")) return base;
      pos++;
      return { k: "bin", op: "^", a: base, b: unary() };
    });
    const postfix = () => {
      let node = primary();
      while (isOp("!")) {
        pos++;
        node = { k: "fact", a: node };
      }
      return node;
    };
    const primary = () => {
      const token = peek();
      if (!token) throw new Error("식이 중간에 끝났습니다.");
      pos++;
      if (token.type === "num") return { k: "num", v: token.value, raw: token.raw };
      if (token.type === "op" && token.value === "(") {
        const node = expr();
        take(")");
        return node;
      }
      if (token.type === "name" && has(FUNCS, token.value)) {
        if (!isOp("(")) throw new Error(`${token.value} 뒤에는 괄호가 필요합니다. 예: ${token.value}(2)`);
        pos++;
        const node = { k: "fn", name: token.value, a: expr() };
        take(")");
        return node;
      }
      if (token.type === "name" && has(CONSTS, token.value)) return { k: "const", name: token.value };
      if (token.type === "name") throw new Error(`알 수 없는 이름입니다: ${token.value}`);
      throw new Error(`예상하지 못한 기호입니다: ${token.value}`);
    };
    const node = expr();
    if (pos < tokens.length) throw new Error(`예상하지 못한 기호입니다: ${tokens[pos].value}`);
    return node;
  }

  function factorialOf(n, one) {
    let out = one;
    for (let k = one + one; k <= n; k++) out *= k;
    return out;
  }

  function floatValue(node) {
    switch (node.k) {
      case "num": return node.v;
      case "const": return CONSTS[node.name];
      case "neg": return -floatValue(node.a);
      case "fn": return FUNCS[node.name](floatValue(node.a));
      case "fact": {
        const n = floatValue(node.a);
        if (!Number.isInteger(n) || n < 0) throw new Error("계승(!)은 0 이상의 정수에만 쓸 수 있습니다.");
        if (n > 170) throw new Error("계승(!) 값이 너무 큽니다.");
        return factorialOf(n, 1);
      }
      default: {
        const a = floatValue(node.a);
        const b = floatValue(node.b);
        if ((node.op === "/" || node.op === "%") && b === 0) throw new Error("0으로 나눌 수 없습니다.");
        return node.op === "+" ? a + b : node.op === "-" ? a - b : node.op === "*" ? a * b :
          node.op === "/" ? a / b : node.op === "%" ? a % b : Math.pow(a, b);
      }
    }
  }

  const bits = (x) => (x < 0n ? -x : x).toString(16).length * 4;
  const fit = (x) => {
    if (bits(x) > MAX_BITS) throw NOT_EXACT;
    return x;
  };

  // Integer-only arithmetic in BigInt; anything else throws NOT_EXACT and the float result is used instead.
  function exactValue(node) {
    switch (node.k) {
      case "num":
        if (!/^\d+$/.test(node.raw)) throw NOT_EXACT;
        return BigInt(node.raw);
      case "neg": return -exactValue(node.a);
      case "fact": {
        const n = exactValue(node.a);
        if (n < 0n) throw new Error("계승(!)은 0 이상의 정수에만 쓸 수 있습니다.");
        if (n > 450n) throw NOT_EXACT;
        return fit(factorialOf(n, 1n));
      }
      case "bin": {
        const a = exactValue(node.a);
        const b = exactValue(node.b);
        if (node.op === "+") return fit(a + b);
        if (node.op === "-") return fit(a - b);
        if (node.op === "*") return fit(a * b);
        if (node.op === "%") {
          if (b === 0n) throw new Error("0으로 나눌 수 없습니다.");
          return a % b;
        }
        if (node.op === "^") {
          if (b < 0n) throw NOT_EXACT;
          if (a === 0n || a === 1n || b === 0n) return a ** b;
          if (a === -1n) return b % 2n ? -1n : 1n;
          if (BigInt(bits(a) - 3) * b > BigInt(MAX_BITS)) throw NOT_EXACT;
          return fit(a ** b);
        }
        throw NOT_EXACT;  // division
      }
      default: throw NOT_EXACT;
    }
  }

  function formatFloat(x) {
    if (Object.is(x, -0)) return "0";
    const abs = Math.abs(x);
    if (abs !== 0 && (abs >= 1e15 || abs < 1e-6)) return x.toPrecision(12).replace(/\.?0+e/, "e");
    return String(Number(x.toPrecision(12)));
  }

  function evaluate(expression) {
    if (typeof expression === "number") expression = String(expression);
    if (typeof expression !== "string" || !expression.trim()) throw new Error("expression 인자가 필요합니다.");
    if (expression.length > 500) throw new Error("식이 너무 깁니다 (최대 500자).");
    const tree = parse(expression);
    try {
      return { expression, result: exactValue(tree).toString(), exact: true };
    } catch (error) {
      if (error !== NOT_EXACT) throw error;
    }
    const value = floatValue(tree);
    if (!Number.isFinite(value)) throw new Error("결과가 유한한 수가 아닙니다 (너무 크거나 정의되지 않음).");
    return { expression, result: formatFloat(value), exact: false,
             note: "부동소수점 계산 결과이며 유효숫자 12자리로 반올림했습니다. 삼각함수는 라디안 기준입니다." };
  }

  // -------------------------------------------------------------------------------------- current time
  const ZONES = { kst: "Asia/Seoul", seoul: "Asia/Seoul", korea: "Asia/Seoul", 서울: "Asia/Seoul", 한국: "Asia/Seoul",
                  jst: "Asia/Tokyo", tokyo: "Asia/Tokyo", 도쿄: "Asia/Tokyo", london: "Europe/London",
                  런던: "Europe/London", paris: "Europe/Paris", 파리: "Europe/Paris", "new york": "America/New_York",
                  뉴욕: "America/New_York", est: "America/New_York", pst: "America/Los_Angeles", gmt: "UTC", utc: "UTC" };
  const WEEKDAYS = ["일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일"];
  const pad = (n) => String(n).padStart(2, "0");

  function currentTime(args) {
    const asked = typeof args.timezone === "string" && args.timezone.trim() ? args.timezone.trim() : "Asia/Seoul";
    const zone = ZONES[asked.toLowerCase()] || asked;
    let format;
    try {
      format = new Intl.DateTimeFormat("en-US", { timeZone: zone, year: "numeric", month: "2-digit", day: "2-digit",
                                                  hour: "2-digit", minute: "2-digit", second: "2-digit",
                                                  hourCycle: "h23", weekday: "long" });
    } catch (error) {
      return { error: `알 수 없는 시간대입니다: ${asked}. Asia/Seoul, Europe/London 같은 IANA 이름을 쓰세요.` };
    }
    const now = new Date();
    now.setMilliseconds(0);
    const parts = Object.fromEntries(format.formatToParts(now).map((part) => [part.type, part.value]));
    const [y, mo, d, h, mi, s] = [parts.year, parts.month, parts.day, parts.hour % 24, parts.minute, parts.second].map(Number);
    const offset = Math.round((Date.UTC(y, mo - 1, d, h, mi, s) - now.getTime()) / 60000);
    const utcOffset = `${offset < 0 ? "-" : "+"}${pad(Math.floor(Math.abs(offset) / 60))}:${pad(Math.abs(offset) % 60)}`;
    const date = `${y}-${pad(mo)}-${pad(d)}`;
    const time = `${pad(h)}:${pad(mi)}:${pad(s)}`;
    return { timezone: format.resolvedOptions().timeZone, iso: `${date}T${time}${utcOffset}`, date, time,
             weekday: parts.weekday, weekday_ko: WEEKDAYS[new Date(Date.UTC(y, mo - 1, d)).getUTCDay()],
             utc_offset: utcOffset };
  }

  // ------------------------------------------------------------------------------------- fake weather
  const CONDITIONS = [["맑음", "clear"], ["구름 조금", "partly cloudy"], ["흐림", "cloudy"], ["비", "rain"], ["안개", "fog"]];

  function weather(args) {
    const city = typeof args.city === "string" ? args.city.trim().slice(0, 100) : "";
    if (!city) return { error: "city 인자가 필요합니다." };
    let h = 2166136261;
    for (const ch of city.toLowerCase()) h = Math.imul(h ^ ch.codePointAt(0), 16777619) >>> 0;
    const temperature = (h % 36) - 5;
    let [condition, conditionEn] = CONDITIONS[(h >>> 8) % CONDITIONS.length];
    if (conditionEn === "rain" && temperature <= 0) [condition, conditionEn] = ["눈", "snow"];
    return { city, fake: true, note: "데모용 가짜 데이터입니다. 실제 날씨가 아니라는 점을 답변에 밝혀 주세요.",
             condition, condition_en: conditionEn, temperature_c: temperature,
             humidity_pct: 30 + ((h >>> 16) % 61), wind_mps: ((h >>> 24) % 100) / 10 };
  }

  // ------------------------------------------------------------------------------------------- runner
  const RUNNERS = { calculator: (args) => evaluate(args.expression), get_current_time: currentTime,
                    get_weather: weather };

  AX.tools = {
    names: new Set(Object.keys(RUNNERS)),
    labels: { calculator: "계산기", get_current_time: "현재 시각", get_weather: "날씨 (가짜 데이터)" },
    // Returns {ok, result}; result is a plain object the chat sends back as the tool message (JSON text).
    run(name, argumentsText) {
      if (!has(RUNNERS, name)) return { ok: false, result: { error: `알 수 없는 도구입니다: ${name}` } };
      let args;
      try {
        args = argumentsText && argumentsText.trim() ? JSON.parse(argumentsText) : {};
      } catch (error) {
        return { ok: false, result: { error: "도구 인자(JSON)를 해석하지 못했습니다." } };
      }
      if (!args || typeof args !== "object" || Array.isArray(args)) {
        return { ok: false, result: { error: "도구 인자는 JSON 객체여야 합니다." } };
      }
      let result;
      try {
        result = RUNNERS[name](args);
      } catch (error) {
        result = { error: String(error && error.message || error) };
      }
      return { ok: !result.error, result };
    },
    evaluate,
  };
})();
