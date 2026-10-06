"use strict";
// A small, safe Markdown renderer for model output: every node is created with DOM calls, links are limited
// to http(s), and nested lists are flattened into items with explicit markers and a depth class.
(function () {
  const AX = window.AX;
  const el = AX.el;

  const FENCE = /^(\s*)(`{3,}|~{3,})\s*([^`\s]*)[^`]*$/;
  const HEADING = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  const HR = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
  const QUOTE = /^ {0,3}>/;
  const LIST = /^(\s*)([-*+•]|\d{1,3}[.)])\s+(.*)$/;
  const DELIM = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;
  const INLINE = new RegExp([
    "(`+)([^\\n]*?[^`\\n])\\1(?!`)",                 // 1, 2: code span
    "\\*\\*(?=\\S)([^\\n]*?\\S)\\*\\*",               // 3: bold
    "~~(?=\\S)([^\\n]*?\\S)~~",                       // 4: strike
    "\\*(?=[^\\s*])([^\\n]*?[^\\s*])\\*(?!\\*)",      // 5: italic
    "\\[([^\\]\\n]{1,500})\\]\\((https?:\\/\\/[^\\s)]{1,2000})\\)",  // 6, 7: link
    "(https?:\\/\\/[A-Za-z0-9\\-._~:/?#@!$&*+,;=%]+)",  // 8: bare URL
  ].join("|"), "g");
  const WORD = /[\p{L}\p{N}]/u;

  function link(href, children) {
    let url;
    try {
      url = new URL(href);
    } catch (error) {
      return children;
    }
    if (url.protocol !== "https:" && url.protocol !== "http:") return children;
    return el("a", { href: url.href, target: "_blank", rel: "noopener noreferrer nofollow" }, children);
  }

  function inline(text, depth) {
    if (depth > 6) return [text];
    const out = [];
    const re = new RegExp(INLINE.source, "g");
    let last = 0;
    let m;
    while ((m = re.exec(text))) {
      const start = m.index;
      let node;
      if (m[1] !== undefined) {
        node = el("code", { text: m[2].length > 2 && m[2].startsWith(" ") && m[2].endsWith(" ") ? m[2].slice(1, -1) : m[2] });
      } else if (m[3] !== undefined) {
        node = el("strong", null, inline(m[3], depth + 1));
      } else if (m[4] !== undefined) {
        node = el("del", null, inline(m[4], depth + 1));
      } else if (m[5] !== undefined) {
        if (start > 0 && WORD.test(text[start - 1])) {  // 2*3*4 is arithmetic, not emphasis
          re.lastIndex = start + 1;
          continue;
        }
        node = el("em", null, inline(m[5], depth + 1));
      } else if (m[6] !== undefined) {
        node = link(m[7], inline(m[6], depth + 1));
      } else {
        const url = m[8].replace(/[.,;:!?*]+$/, "");
        re.lastIndex = start + url.length;
        node = link(url, [url]);
      }
      if (start > last) out.push(text.slice(last, start));
      out.push(node);
      last = re.lastIndex;
    }
    if (last < text.length) out.push(text.slice(last));
    return out;
  }

  function lines(parts) {
    const out = [];
    parts.forEach((part, index) => {
      if (index) out.push(el("br"));
      out.push(inline(part, 0));
    });
    return out;
  }

  function copyButton(getText) {
    const button = el("button", { type: "button", class: "ghost tiny", text: "복사" });
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(getText());
        button.textContent = "복사됨";
      } catch (error) {
        button.textContent = "복사 실패";
      }
      setTimeout(() => { button.textContent = "복사"; }, 1500);
    });
    return button;
  }

  function code(body, lang) {
    return el("div", { class: "code" },
      el("div", { class: "code-head" }, el("span", { text: lang || "code" }), copyButton(() => body)),
      el("pre", null, el("code", { text: body })));
  }

  function cells(row) {
    let s = row.trim();
    if (s.startsWith("|")) s = s.slice(1);
    if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
    const out = [];
    let cur = "";
    let inCode = false;
    for (let k = 0; k < s.length; k++) {
      const ch = s[k];
      if (ch === "\\" && s[k + 1] === "|") {
        cur += "|";
        k++;
        continue;
      }
      if (ch === "`") inCode = !inCode;
      if (ch === "|" && !inCode) {
        out.push(cur.trim());
        cur = "";
        continue;
      }
      cur += ch;
    }
    out.push(cur.trim());
    return out;
  }

  function table(rows, delimiter) {
    const head = cells(rows[0]);
    const align = cells(delimiter).map((d) => (/^:-+:$/.test(d) ? "center" : /-:$/.test(d) ? "right" : ""));
    const cell = (tag, text, index) => el(tag, { class: align[index] ? `align-${align[index]}` : null }, inline(text, 0));
    const body = rows.slice(1).map((row) => {
      const values = cells(row);
      return el("tr", null, head.map((_, index) => cell("td", values[index] || "", index)));
    });
    return el("div", { class: "scroll" }, el("table", { class: "table md" },
      el("thead", null, el("tr", null, head.map((text, index) => cell("th", text, index)))),
      el("tbody", null, body)));
  }

  function startsBlock(all, i) {
    const line = all[i];
    return FENCE.test(line) || HEADING.test(line) || HR.test(line) || QUOTE.test(line) || LIST.test(line) ||
      (line.includes("|") && i + 1 < all.length && all[i + 1].includes("|") && DELIM.test(all[i + 1]));
  }

  function list(all, i) {
    const items = [];
    while (i < all.length) {
      const line = all[i];
      const m = LIST.exec(line);
      if (m && !HR.test(line)) {
        items.push({ indent: m[1].replace(/\t/g, "    ").length, marker: m[2], lines: [m[3]] });
        i++;
      } else if (!line.trim()) {
        let j = i + 1;
        while (j < all.length && !all[j].trim()) j++;
        if (j < all.length && LIST.test(all[j]) && !HR.test(all[j])) i = j;
        else break;
      } else if (!startsBlock(all, i)) {
        items[items.length - 1].lines.push(line.trim());
        i++;
      } else {
        break;
      }
    }
    const stack = [];
    const nodes = items.map((item) => {
      while (stack.length && item.indent < stack[stack.length - 1]) stack.pop();
      if (!stack.length || item.indent > stack[stack.length - 1]) stack.push(item.indent);
      const depth = Math.min(stack.length - 1, 5);
      const marker = /\d/.test(item.marker) ? item.marker : depth % 2 ? "◦" : "•";
      return el("li", { class: `depth-${depth}` }, el("span", { class: "marker", text: marker }),
                el("div", { class: "item" }, lines(item.lines)));
    });
    return [el("ul", { class: "md-list" }, nodes), i];
  }

  function blocks(text, depth) {
    const all = text.split("\n");
    const out = [];
    let i = 0;
    while (i < all.length) {
      const line = all[i];
      let m;
      if (!line.trim()) {
        i++;
      } else if ((m = FENCE.exec(line))) {
        const indent = m[1].length;
        const close = new RegExp(`^\\s*${m[2][0] === "`" ? "`" : "~"}{${m[2].length},}\\s*$`);
        const body = [];
        i++;
        while (i < all.length && !close.test(all[i])) {
          body.push(all[i].replace(new RegExp(`^ {0,${indent}}`), ""));
          i++;
        }
        i++;
        out.push(code(body.join("\n"), m[3]));
      } else if ((m = HEADING.exec(line))) {
        out.push(el(`h${Math.min(m[1].length + 2, 6)}`, null, inline(m[2], 0)));
        i++;
      } else if (HR.test(line)) {
        out.push(el("hr"));
        i++;
      } else if (QUOTE.test(line)) {
        const body = [];
        while (i < all.length && QUOTE.test(all[i])) body.push(all[i++].replace(/^ {0,3}> ?/, ""));
        out.push(el("blockquote", null, depth < 4 ? blocks(body.join("\n"), depth + 1) : lines(body)));
      } else if (line.includes("|") && i + 1 < all.length && all[i + 1].includes("|") && DELIM.test(all[i + 1])) {
        const rows = [line];
        const delimiter = all[i + 1];
        i += 2;
        while (i < all.length && all[i].trim() && all[i].includes("|")) rows.push(all[i++]);
        out.push(table(rows, delimiter));
      } else if (LIST.test(line)) {
        const [node, next] = list(all, i);
        out.push(node);
        i = next;
      } else {
        const para = [line.trim()];
        i++;
        while (i < all.length && all[i].trim() && !startsBlock(all, i)) para.push(all[i++].trim());
        out.push(el("p", null, lines(para)));
      }
    }
    return out;
  }

  // Renders Markdown into `node`, replacing what it held.
  AX.renderMarkdown = function (node, text) {
    node.replaceChildren(...blocks(String(text || "").replace(/\r\n?/g, "\n"), 0));
  };
})();
