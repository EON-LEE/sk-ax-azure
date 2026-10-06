"""Runs the demo's Markdown renderer (demo/frontend/static/markdown.js) under Node with a stub DOM."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r"""
const fs = require("fs");
global.window = { AX: { el: (tag, attrs, ...kids) => ({ tag, cls: attrs && attrs.class, text: attrs && attrs.text, kids: kids.flat(9) }) } };
eval(fs.readFileSync(process.argv[1], "utf8"));
const flat = (n) => typeof n === "string" ? n : n.tag === "br" ? "\n" :
  (n.cls && n.cls.startsWith("math") ? `[${n.cls}:${n.text}]` : (n.text || "") + (n.kids || []).map(flat).join(""));
const node = { replaceChildren(...k) { this.k = k; } };
window.AX.renderMarkdown(node, JSON.parse(process.argv[2]));
process.stdout.write(JSON.stringify(node.k.map(flat)));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class MathTests(unittest.TestCase):
    def render(self, text):
        out = subprocess.run(["node", "-e", HARNESS, str(ROOT / "demo" / "frontend" / "static" / "markdown.js"),
                              json.dumps(text)], capture_output=True, text=True, encoding="utf-8", check=True)
        return json.loads(out.stdout)

    def test_inline_and_display_math(self):
        text = ("개수를 \\( x \\) 개라 하면\n\\[\nx + (x + 3) = 17\n\\]\n"
                "\\[ 2x = 14 \\Rightarrow x = \\frac{14}{2} = 7 \\]\n답: \\(\\boxed{7}\\), 비용은 $5 와 $10.\n"
                "$$\\sqrt{x^2+1} \\le 10^{3} \\times 2$$")
        self.assertEqual(self.render(text), [
            "개수를 [math:x] 개라 하면", "[math-block:x + (x + 3) = 17]", "[math-block:2x = 14 ⇒ x = 14/2 = 7]",
            "답: [math:7], 비용은 $5 와 $10.", "[math-block:√(x²+1) ≤ 10³ × 2]"])

    def test_unclosed_block_while_streaming(self):
        self.assertEqual(self.render("풀이\n\\[ 17 \\times 23"), ["풀이", "[math-block:17 × 23]"])


if __name__ == "__main__":
    unittest.main()
