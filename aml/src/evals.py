"""Accuracy suites for the demo cluster: the public datasets of the tech report's Table 3, plus a NIAH grid.

aime     MathArena/aime_2026, 30 problems x 8 samples. The tech report's Fig. 10 prompt; the last
         \\boxed{} of the final answer is compared with the integer answer.
kobalt   snunlp/KoBALT-700, 700 items. The prompt of the dataset's evaluation_protocol.md, verbatim;
         the last "정답은 X입니다" is the prediction.
click    EunsuKim/CLIcK, 1,995 items in 26 files. lm-evaluation-harness's CLIcK prompts, answered
         generatively; the CSAT files have five choices, so their prompts list A-E.
ifbench  allenai/IFBench test, 300 prompts. The official verifier at a pinned commit scores the final
         answer: strict and loose, prompt and instruction level (prompt-level loose is the headline).
niah     Needle grid, 8K-256K tokens x depth 0-100 % in 10 % steps, with the needle, filler and
         question of SKT's examples/vllm/niah_test.py (verify_suites.build_niah). Greedy, thinking off.

Thinking is on and sampling follows the model's generation_config.json (temperature 0.6, top_p 0.95),
except for NIAH. Only the final answer (after the reasoning parser) is scored, and a generation that
reaches max_tokens counts as wrong (reported as hit_limit). Requests carry a lower vLLM priority than
the demo's chat traffic. How many are in flight follows the server's /metrics: admission pauses while
requests queue or the KV cache is nearly full, the limit shrinks after preemptions and while demo users
are active, and it grows back slowly otherwise.

Each finished generation becomes one record: ids, extracted and gold answers, token counts and timings;
never the prompt or the full response. Records are appended to <out-dir>/evals/<run>/records.jsonl and, when
AXK2_LINK_HTTP is set, uploaded to the demo frontend, which serves them back so that a resubmitted job
resumes where a preempted one stopped. Summaries go to the frontend and to the job's MLflow tags.
"""
import argparse
import concurrent.futures as futures
import hashlib
import http.client
import json
import math
import os
import random
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPORT = HERE / "report.py"
MODEL = "axk2"
HF = "https://huggingface.co/datasets/{repo}/resolve/{rev}/{path}"
GITHUB = "https://raw.githubusercontent.com/{repo}/{rev}/{path}"
SAMPLING = {"temperature": 0.6, "top_p": 0.95}  # skt/A.X-K2 generation_config.json
GREEDY = {"temperature": 0.0}

AIME = {"repo": "MathArena/aime_2026", "rev": "d2de22f3c656b4f56cf8981212186377d1e23bc3",
        "path": "data/train-00000-of-00001.parquet",
        "sha256": "d91db799651b4cc1f0734f52792a695c9cc60dac342524b3d8e5b2ff31c3e957"}
KOBALT = {"repo": "snunlp/KoBALT-700", "rev": "30c30a431066508e6bef77cfa6d6059b85b12f0d", "path": "data/train.jsonl",
          "sha256": "ecc68805e17d1c87b63cbb70ec3ba101eabcbd2c489e813ba229f272f20c092f"}
# CLIcK files at the pinned revision with their git blob ids (the Hub's tree listing reports them).
CLICK = {"repo": "EunsuKim/CLIcK", "rev": "d61627859645b5e6edc03fd9f835735d8226fa4e", "files": {
    "Culture/Korean Economy/Economy_KIIP": "289b886eaac68ad01355ab0f8c55f492ba149f15",
    "Culture/Korean Economy/Economy_Kedu": "6fd44a6a701eb534aa94b331087b1ae73c981903",
    "Culture/Korean Geography/Geography_CSAT": "3045811ea04d3d490873ce596bd52716929786ae",
    "Culture/Korean Geography/Geography_KIIP": "079acf7dfa2619897a954abab78aed009667c4eb",
    "Culture/Korean Geography/Geography_Kedu": "11876b32a6e23afa8c973353c6fa8c7b41af8a12",
    "Culture/Korean History/History_KHB": "d87a09df028deaff440edafadfc3389daf12f00e",
    "Culture/Korean History/History_Kedu": "39d9f48d47ed6c65834c39259b4d1b27bac32711",
    "Culture/Korean History/History_PSE": "66e3a584c12897f8ff247cf121b17ec13b8893ae",
    "Culture/Korean Law/Law_KIIP": "0f35aacd88b2c7fd8f701782c13b8e9440f84ead",
    "Culture/Korean Law/Law_PSAT": "aa328d5f1d71518b9a90b062c1a09abcb5b55fad",
    "Culture/Korean Politics/Politics_KIIP": "44a4674b761c4a99422cbdf33b07027cbfc69922",
    "Culture/Korean Politics/Politics_Kedu": "8fd18658a3e8e1dec2227260dd9a9eee0b4e4130",
    "Culture/Korean Popular/Popular_KIIP": "3ee0cf31050dcdd0f79742ad16c4f89179607872",
    "Culture/Korean Popular/Popular_Kedu": "e4b7267c7359cf575fd13cbeee95839c666b5efc",
    "Culture/Korean Society/Society_KIIP": "d1941b1a39de0498784dbc8c6966024088ccb7b3",
    "Culture/Korean Society/Society_Kedu": "c8391b3875cca837d026f22ac7988d2445a1dd1b",
    "Culture/Korean Tradition/Tradition_KIIP": "03a4ed4bcd45c3d178a2b084bff2d10999392b70",
    "Culture/Korean Tradition/Tradition_Kedu": "6ca6c948923f79388f055f497830e256f70a90ea",
    "Language/Functional/Functional_CSAT": "ad1cd51f3c884452c7851f2260cb97c1dcc52c62",
    "Language/Functional/Functional_Kedu": "7720e20038241d2f30b2a8a1320fb92409c6f3fd",
    "Language/Functional/Functional_PSE": "7bb962f3d8ff6634db0f72d97a835ea3b480d235",
    "Language/Grammar/Grammar_CSAT": "deedb34356cc0a5bd53c76e1cf188addbcf70bb7",
    "Language/Grammar/Grammar_Kedu": "c33d8f8fb3c14f547404bada7a60cec328fd58a0",
    "Language/Grammar/Grammar_TOPIK": "803da14b33ed1529679da202d71f11e90643c6ac",
    "Language/Textual/Textual_CSAT": "4d93c1eec4fd5a4da2fc42e3fbb37dc90b6440b1",
    "Language/Textual/Textual_TOPIK": "8166378b309efde5a79920744e006ae18843903a"}}
IFBENCH = {"repo": "allenai/IFBench", "rev": "1c40f0c10d9b5c5c2f10a175a28007ebb64f7f4d", "files": {
    "evaluation_lib.py": "e681a4b03a6a9dfb540fbd9ef6b963de503a5ffac557ed611c326ee385850cf6",
    "ifbench/__init__.py": "2a806107f115facaa3e7e6046ca97247237b4b113b514bcdd763b2530141f694",
    "ifbench/instructions.py": "a76a3b300b531b95200e53427c6b1fbb72b671cf6d358796797c0fa4b6c7c44f",
    "ifbench/instructions_registry.py": "5119f2919261c77566f698f2f367883aff56cf89443bac5a56b6e30ddbd5aef9",
    "ifbench/instructions_util.py": "6996a6efaf6e3c92a2e926881eadc8c0262fb8d06c8cb56394ef7322f69e1930",
    "ifbench/classic_instructions.py": "d26e642f2e71e4f57218e5e05f098a22e36311485779baac401161d3228c9973",
    "data/IFBench_test.jsonl": "d2ada7da94a38cfe406351614c4e686846ed2da6d1b339db95fa5ead19554a4a"}}
# The verifier's imports, pip-installed into AXK2_EVAL_PKGS by entry.sh (pyarrow reads the AIME parquet).
EVAL_PACKAGES = ["pyarrow==25.0.1", "nltk==3.10.3", "langdetect==1.0.9", "immutabledict==4.3.1", "emoji==2.16.0",
                 "syllapy==0.8.0", "absl-py==2.5.0"]

FIG10 = ("Solve the following math problem efficiently and clearly. The last line of your response should be of "
         "the following format: 'Therefore, the final answer is: $\\boxed{ANSWER}$. I hope it is correct' "
         "(without quotes) where ANSWER is just the final number or expression that solves the problem. "
         "Think step by step before answering.")
KOBALT_SYSTEM = "당신은 문제를 해결하는 전문가입니다."
KOBALT_USER = ("다음 문제에 대해서 충분히 생각하고 추론하여, 10개의 보기(A, B, C, D, E, F, G, H, I, J) 중 정답을 고르세요.\n\n"
               "<QUESTION>\n\n답변은 반드시 다음 형식을 엄격히 지켜야 합니다: \"정답은 [정답 보기]입니다.\"로 끝나야 하고, "
               "[정답 보기]는 A, B, C, D, E, F, G, H, I, J 중 하나여야 합니다.\n정답: 문제를 풀기 위해, 한 번 천천히 생각해봅시다.")
KOBALT_ANSWER = re.compile(r"정답은\s*\**\s*\[?\(?([A-J])\)?\]?\s*\**\s*입니다")
CLICK_ANSWER = re.compile(r"(?:정답|답|(?i:answer))\s*(?:은|는|(?i:is))?\s*[:：]?\s*\**\s*[(\[]?\s*([A-E])(?![A-Za-z])")
LETTER_ONLY = re.compile(r"[\s*(\[]*([A-E])[\s*)\].:]*")
STANDALONE = re.compile(r"(?<![A-Za-z])([A-E])(?![A-Za-z])")

SUITE_DEFAULTS = {  # max_tokens, repeats
    "aime": (131072, 8), "kobalt": (65536, 1), "click": (32768, 1), "ifbench": (32768, 1), "niah": (32, 1)}
NIAH_DEPTHS = [round(i / 10, 1) for i in range(11)]
NIAH_LENGTHS = [8192, 16384, 32768, 65536, 131072, 262144]
RETRY_BACKOFF = 5.0  # seconds per attempt, after /health is back


# ---------------------------------------------------------------------------------------------- data
def sha256_is(digest):
    return lambda data: hashlib.sha256(data).hexdigest() == digest


def git_blob_is(digest):
    return lambda data: hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == digest


def fetch(url, dest, check, attempts=4):
    """Download once into the cache; every copy, cached or fresh, must pass its pinned hash."""
    dest = Path(dest)
    if dest.exists() and check(dest.read_bytes()):
        return dest.read_bytes()
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "axk2-evals"})
            data = urllib.request.urlopen(request, timeout=180).read()
            break
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(5 * (attempt + 1))
    if not check(data):
        raise ValueError(f"pinned hash mismatch: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return data


def json_records(text):
    """KoBALT's train.jsonl is a JSON array; accept JSON Lines as well."""
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    except ValueError:
        return [json.loads(line) for line in text.splitlines() if line.strip()]


class Suite:
    def __init__(self, name, items, score, max_tokens, repeats, thinking=True, sampling=SAMPLING,
                 length_is_wrong=True, breakdown=()):
        self.name, self.items, self.score = name, items, score
        self.max_tokens, self.repeats, self.thinking, self.sampling = max_tokens, repeats, thinking, sampling
        self.length_is_wrong, self.breakdown = length_is_wrong, breakdown


def aime_messages(problem):
    return [{"role": "user", "content": FIG10 + "\n\n" + problem}]


def kobalt_messages(question):
    return [{"role": "system", "content": KOBALT_SYSTEM},
            {"role": "user", "content": KOBALT_USER.replace("<QUESTION>", question)}]


def load_aime(data_dir):
    import pyarrow.parquet as parquet

    path = Path(data_dir) / "aime_2026.parquet"
    fetch(HF.format(**AIME), path, sha256_is(AIME["sha256"]))
    rows = parquet.read_table(path).to_pylist()
    return [{"key": str(row["problem_idx"]), "gold": int(row["answer"]), "messages": aime_messages(row["problem"]),
             "meta": {}} for row in rows]


def load_kobalt(data_dir):
    data = fetch(HF.format(**KOBALT), Path(data_dir) / "kobalt_700.json", sha256_is(KOBALT["sha256"]))
    return [{"key": row["ID"], "gold": row["Answer"].strip(), "messages": kobalt_messages(row["Question"]),
             "meta": {"class": row["Class"], "subclass": row.get("Subclass"), "level": row["Level"],
                      "sampling_yn": row.get("Sampling_YN")}}
            for row in json_records(data.decode("utf-8"))]


def click_prompt(row):
    letters = "ABCDE"[:len(row["choices"])]
    options = ", ".join(f"{letter}:{choice}" if index == 0 else f"{letter}: {choice}"
                        for index, (letter, choice) in enumerate(zip(letters, row["choices"])))
    listed = ", ".join(letters)
    if row.get("paragraph"):
        return (f"주어진 맥락을 천천히 읽고, 질문에 대한 적절한 정답을 {listed} 중에 골라 알파벳 하나로 답하시오.\n\n"
                f"맥락: {row['paragraph']}\n질문: {row['question']}\n보기:\n{options}\n정답:")
    return (f"주어진 질문을 천천히 읽고, 적절한 정답을 {listed} 중에 골라 알파벳 하나로 답하시오.\n\n"
            f"질문: {row['question']}\n보기:\n{options}\n정답:")


def click_items(name, rows):
    """Keyed by file and position: the dataset repeats 8 ids across its 1,995 items."""
    group, category, stem = name.split("/")
    items = []
    for index, row in enumerate(rows):
        choices = row["choices"]
        items.append({"key": f"{stem}:{index}", "gold": "ABCDE"[choices.index(row["answer"])],
                      "messages": [{"role": "user", "content": click_prompt(row)}],
                      "meta": {"file": stem, "group": group, "category": category.replace("Korean ", ""),
                               "id": row.get("id"), "n_choices": len(choices),
                               "duplicate_choices": len(set(choices)) != len(choices)}})
    return items


def load_click(data_dir):
    items = []
    for name, blob in CLICK["files"].items():
        path = "Dataset/" + urllib.parse.quote(name) + ".json"
        url = HF.format(repo=CLICK["repo"], rev=CLICK["rev"], path=path)
        data = fetch(url, Path(data_dir) / "click" / (name.replace("/", "__") + ".json"), git_blob_is(blob))
        items.extend(click_items(name, json.loads(data.decode("utf-8"))))
    return items


def ifbench_root(data_dir):
    root = Path(data_dir) / f"ifbench-{IFBENCH['rev'][:12]}"
    for rel, digest in IFBENCH["files"].items():
        fetch(GITHUB.format(repo=IFBENCH["repo"], rev=IFBENCH["rev"], path=rel), root / rel, sha256_is(digest))
    return root


class IFBenchVerifier:
    """allenai/IFBench evaluation_lib, run as its run_eval does: strict, then loose on the same input (strict
    drops None kwargs in place and loose relies on that). Seeded per prompt for reproducibility."""

    def __init__(self, root):
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        import langdetect

        langdetect.DetectorFactory.seed = 0
        import evaluation_lib

        self.lib, self.lock = evaluation_lib, threading.Lock()

    def score(self, example, response):
        inp = self.lib.InputExample(key=example["key"], instruction_id_list=list(example["instruction_id_list"]),
                                    prompt=example["prompt"], kwargs=[dict(k) for k in example["kwargs"]])
        mapping = {inp.prompt: response}
        with self.lock:
            random.seed(f"ifbench-{example['key']}")
            strict = self.lib.test_instruction_following_strict(inp, mapping)
            loose = self.lib.test_instruction_following_loose(inp, mapping)
        return list(strict.follow_instruction_list), list(loose.follow_instruction_list)


def load_ifbench(data_dir):
    root = ifbench_root(data_dir)
    rows = json_records((root / "data" / "IFBench_test.jsonl").read_text(encoding="utf-8"))
    return root, [{"key": str(row["key"]), "gold": None, "example": row,
                   "messages": [{"role": "user", "content": row["prompt"]}],
                   "meta": {"instruction_ids": row["instruction_id_list"]}} for row in rows]


def niah_items(lengths, max_model_len):
    items = []
    for length in lengths:
        target = min(length, max_model_len - 48)
        for index, depth in enumerate(NIAH_DEPTHS):
            code = f"KX-{length // 1024}K-{index:02d}{(index * 37 + length // 1024) % 100:02d}7"
            items.append({"key": f"{length}@{depth}", "gold": code, "messages": None, "target": target, "depth": depth,
                          "meta": {"length": length, "target_tokens": target, "depth": depth}})
    return items


# ------------------------------------------------------------------------------------------- scoring
def final_answer(content):
    """Score what follows the reasoning even if a server runs without the reasoning parser."""
    return content.rsplit("</think>", 1)[1] if "</think>" in content else content


def last_boxed(text):
    start = text.rfind("\\boxed")
    while start >= 0:
        index = start + len("\\boxed")
        while index < len(text) and text[index] == " ":
            index += 1
        if index < len(text) and text[index] == "{":
            depth = 0
            for end in range(index, len(text)):
                depth += {"{": 1, "}": -1}.get(text[end], 0)
                if depth == 0:
                    return text[index + 1:end]
        start = text.rfind("\\boxed", 0, start)
    return None


def aime_value(boxed):
    if boxed is None:
        return None
    text = re.sub(r"\\(?:text|textbf|mathrm|mathbf)\{([^{}]*)\}", r"\1", boxed)
    for token in ("\\!", "\\,", "\\;", "{,}", "$", " ", "\\left", "\\right"):
        text = text.replace(token, "")
    text = text.rsplit("=", 1)[-1]  # "m+n=277"
    text = re.sub(r"\^\{?\\circ\}?$", "", text).rstrip(".")
    match = re.fullmatch(r"[+-]?\d+(?:\.0*)?", text.replace(",", ""))
    return int(match.group(0).replace(",", "").split(".")[0]) if match else None


def score_aime(item, content):
    pred = aime_value(last_boxed(content))
    return {"correct": pred is not None and pred == item["gold"], "pred": pred}


def score_kobalt(item, content):
    found = KOBALT_ANSWER.findall(content)
    pred = found[-1] if found else None
    return {"correct": pred == item["gold"], "pred": pred}


def click_letter(text, n_choices):
    letters = "ABCDE"[:n_choices]
    text = text.strip()
    match = LETTER_ONLY.fullmatch(text)
    if match and match.group(1) in letters:
        return match.group(1)
    found = [letter for letter in CLICK_ANSWER.findall(text) if letter in letters]
    if found:
        return found[-1]
    standalone = {letter for letter in STANDALONE.findall(text) if letter in letters}
    return standalone.pop() if len(standalone) == 1 else None


def score_click(item, content):
    pred = click_letter(content, item["meta"]["n_choices"])
    return {"correct": pred == item["gold"], "pred": pred}


def score_niah(item, content):
    return {"correct": item["gold"] in content, "pred": content.strip()[:40]}


def ifbench_scorer(verifier):
    def score(item, content):
        strict, loose = verifier.score(item["example"], content)
        return {"correct": all(loose), "pred": None, "strict": strict, "loose": loose}
    return score


# -------------------------------------------------------------------------------------------- client
def call(base, path, body=None, timeout=600):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def stream_chat(base, body, timeout=3600, stop=None):
    """One streamed chat completion; the reasoning arrives as reasoning_content (or reasoning) deltas.
    Setting `stop` abandons the stream at the next chunk; closing the connection makes vLLM abort the request."""
    request = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    content, reasoning, finish, usage, first = [], [], None, None, None
    began = time.time()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        for raw in response:
            if stop is not None and stop.is_set():
                raise RuntimeError("stopped")
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise RuntimeError(f"stream error: {json.dumps(chunk['error'])[:400]}")
            usage = chunk.get("usage") or usage
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                piece, thought = delta.get("content"), delta.get("reasoning_content") or delta.get("reasoning")
                if (piece or thought) and first is None:
                    first = time.time() - began
                if piece:
                    content.append(piece)
                if thought:
                    reasoning.append(thought)
                finish = choice.get("finish_reason") or finish
    return {"content": "".join(content), "reasoning_chars": sum(map(len, reasoning)), "finish_reason": finish,
            "usage": usage or {}, "ttft": None if first is None else round(first, 3),
            "seconds": round(time.time() - began, 3)}


def healthy(base):
    try:
        with urllib.request.urlopen(base + "/health", timeout=10) as response:
            return response.status == 200
    except Exception:
        return False


def wait_healthy(base, limit=1800, stop=None):
    began = time.time()
    while not healthy(base):
        if time.time() - began > limit or (stop is not None and stop.is_set()):
            return False
        time.sleep(10)
    return True


def priority_accepted(base, priority):
    """vLLM rejects a non-zero priority unless the server runs --scheduling-policy priority."""
    body = {"model": MODEL, "messages": [{"role": "user", "content": "1+1=?"}], "max_tokens": 1,
            "priority": priority, "chat_template_kwargs": {"enable_thinking": False}}
    try:
        call(base, "/v1/chat/completions", body)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code == 400 and "priority" in exc.read().decode("utf-8", "replace").lower():
            return False
        raise


# ----------------------------------------------------------------------------------------- admission
METRICS = {"running": "vllm:num_requests_running", "waiting": "vllm:num_requests_waiting",
           "kv": "vllm:kv_cache_usage_perc", "preemptions": "vllm:num_preemptions_total"}


def parse_metrics(text):
    values = {key: 0.0 for key in METRICS}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        for key, name in METRICS.items():
            if line.startswith(name + "{") or line.startswith(name + " "):
                value = float(line.rsplit(" ", 1)[1])
                values[key] = max(values[key], value) if key == "kv" else values[key] + value
    return values


class Admission:
    """How many eval requests may be in flight, from the server's own load (additive increase,
    multiplicative decrease on preemption). Demo users count as everything running or waiting
    that is not ours."""

    def __init__(self, base, start=40, cap=56, demo_cap=16, floor=4, kv_pause=0.90, interval=3.0):
        self.base, self.limit, self.cap, self.demo_cap, self.floor = base, start, cap, demo_cap, floor
        self.kv_pause, self.interval = kv_pause, interval
        self.cond, self.in_flight, self.paused, self.demo = threading.Condition(), 0, True, 0
        self.preemptions, self.last_change, self.last = None, None, {}
        self.stop = threading.Event()

    def update(self, metrics, now):
        with self.cond:
            if self.last_change is None:
                self.last_change = now
            self.demo = max(0, int(metrics["running"] + metrics["waiting"]) - self.in_flight)
            if self.preemptions is not None and metrics["preemptions"] > self.preemptions:
                self.limit, self.last_change = max(self.floor, int(self.limit * 0.75)), now
            elif (metrics["kv"] < 0.75 and metrics["waiting"] == 0 and self.limit < self.cap
                  and now - self.last_change >= 60):
                self.limit, self.last_change = min(self.cap, self.limit + 2), now
            self.preemptions = metrics["preemptions"]
            self.paused = metrics["waiting"] > 0 or metrics["kv"] > self.kv_pause
            self.last = dict(metrics, limit=self.limit, demo=self.demo, in_flight=self.in_flight)
            self.cond.notify_all()

    def effective(self):
        return min(self.limit, self.demo_cap) if self.demo else self.limit

    def run(self):
        while not self.stop.is_set():
            try:
                with urllib.request.urlopen(self.base + "/metrics", timeout=10) as response:
                    self.update(parse_metrics(response.read().decode()), time.time())
            except Exception:
                with self.cond:
                    self.paused = True
            self.stop.wait(self.interval)

    def acquire(self, give_up=lambda: False):
        """Take a slot. False, without one, once stopped or when give_up() turns true while waiting."""
        with self.cond:
            while self.paused or self.in_flight >= self.effective():
                if self.stop.is_set() or give_up():
                    return False
                self.cond.wait(5)
            self.in_flight += 1
            return True

    def release(self):
        with self.cond:
            self.in_flight -= 1
            self.cond.notify_all()


# -------------------------------------------------------------------------------------------- record
class Sink:
    """records.jsonl, plus batched uploads to the demo frontend (kept and retried while it is unreachable)."""

    def __init__(self, path, run, link="", token=""):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path, self.run, self.link, self.token = Path(path), run, link.rstrip("/"), token
        self.lock, self.pending = threading.Lock(), []
        self.handle = open(self.path, "a", encoding="utf-8")

    def link_call(self, path, body=None, timeout=60):
        request = urllib.request.Request(self.link + path, data=None if body is None else json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json",
                                                  "Authorization": f"Bearer {self.token}"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")

    def previous(self):
        records = []
        if self.path.exists():
            records += [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if self.link:
            for attempt in range(5):
                try:
                    records += self.link_call("/api/link/evals?run=" + urllib.parse.quote(self.run)).get("records", [])
                    break
                except Exception as exc:
                    print(f"AXK2_EVALS_WARN resume fetch failed ({exc}); retrying", flush=True)
                    time.sleep(10 * (attempt + 1))
        return records

    def add(self, record):
        with self.lock:
            self.handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.handle.flush()
            self.pending.append(record)

    def flush(self):
        if not self.link:
            return True
        with self.lock:
            batch, self.pending = self.pending, []
        if not batch:
            return True
        try:
            self.link_call("/api/link/evals", {"run": self.run, "records": batch})
            return True
        except Exception as exc:
            print(f"AXK2_EVALS_WARN upload of {len(batch)} records failed: {exc}", flush=True)
            with self.lock:
                self.pending = batch + self.pending
            return False

    def summary(self, data):
        if self.link:
            try:
                self.link_call("/api/link/evals/summary", dict(data, run=self.run))
            except Exception as exc:
                print(f"AXK2_EVALS_WARN summary upload failed: {exc}", flush=True)

    def close(self):
        with self.lock:
            self.handle.close()


def unit_id(suite, key, rep):
    return f"{suite}|{key}|{rep}"


def order(units, seed):
    """Repetition-major (every item once before any item twice), shuffled reproducibly within a repetition.
    NIAH goes last: its 256K-token prefills stretch every scheduler step to seconds, which stalls the
    other suites' decoding (about 40 tok/s for the whole cluster instead of about 400) and demo chats."""
    def rank(unit):
        suite, item, rep = unit
        digest = hashlib.sha256(f"{seed}|{suite.name}|{item['key']}|{rep}".encode()).hexdigest()
        return suite.name == "niah", rep, digest
    return sorted(units, key=rank)


def completed(records):
    return {unit_id(r["suite"], r["key"], r["rep"]) for r in records if not r.get("error")}


# ------------------------------------------------------------------------------------------- summary
def latest_records(records):
    best = {}
    for record in records:
        uid = unit_id(record["suite"], record["key"], record["rep"])
        if uid in best and record.get("error") and not best[uid].get("error"):
            continue
        best[uid] = record
    return list(best.values())


def mean_ci(values):
    if not values:
        return None, None
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, None
    sd = math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))
    return mean, 1.96 * sd / math.sqrt(len(values))


def per_item_means(records):
    by_item = {}
    for record in records:
        by_item.setdefault(record["key"], []).append(1.0 if record["correct"] else 0.0)
    return {key: sum(v) / len(v) for key, v in by_item.items()}


def summarize_suite(records, n_items, repeats, breakdown=()):
    scored = [r for r in records if not r.get("error")]
    items = per_item_means(scored)
    mean, half = mean_ci(list(items.values()))
    tokens = [r["completion_tokens"] for r in scored if r.get("completion_tokens") is not None]
    entry = {"n_items": len(items), "items_expected": n_items, "repeats": repeats, "n_generations": len(scored),
             "generations_expected": n_items * repeats, "score": mean, "ci95_half": half,
             "ci95": None if half is None else [mean - half, mean + half],
             "hit_limit": sum(1 for r in scored if r.get("hit_limit")),
             "score_errors": sum(1 for r in scored if r.get("score_error")),
             "errors": sum(1 for r in records if r.get("error")),
             "missing": n_items * repeats - len(records),
             "mean_completion_tokens": round(sum(tokens) / len(tokens), 1) if tokens else None,
             "mean_seconds": round(sum(r["seconds"] for r in scored) / len(scored), 1) if scored else None}
    for field in breakdown:
        groups = {}
        for record in scored:
            groups.setdefault(str(record["meta"].get(field)), []).append(record)
        entry[f"by_{field}"] = {name: {"n_items": len(per_item_means(group)),
                                       "score": mean_ci(list(per_item_means(group).values()))[0]}
                                for name, group in sorted(groups.items())}
    if repeats > 1:
        reps = {}
        for record in scored:
            reps.setdefault(record["rep"], []).append(1.0 if record["correct"] else 0.0)
        entry["by_rep"] = {str(rep): sum(v) / len(v) for rep, v in sorted(reps.items())}
    return entry


def ifbench_levels(records):
    scored = [r for r in records if not r.get("error") and "strict" in r]
    if not scored:
        return {}
    flat = {name: [x for r in scored for x in r[name]] for name in ("strict", "loose")}
    prompt = {name: [1.0 if all(r[name]) else 0.0 for r in scored] for name in ("strict", "loose")}
    return {"prompt_strict": mean_ci(prompt["strict"])[0], "prompt_loose": mean_ci(prompt["loose"])[0],
            "instruction_strict": sum(flat["strict"]) / len(flat["strict"]),
            "instruction_loose": sum(flat["loose"]) / len(flat["loose"]), "n_instructions": len(flat["strict"])}


def niah_grid(records):
    grid = {}
    for record in records:
        if not record.get("error"):
            grid.setdefault(str(record["meta"]["length"]), {})[str(record["meta"]["depth"])] = bool(record["correct"])
    return grid


def summarize(records, plan):
    records = latest_records(records)
    out = {}
    for name, spec in plan.items():
        mine = [r for r in records if r["suite"] == name]
        entry = summarize_suite(mine, spec["items"], spec["repeats"], spec.get("breakdown", ()))
        if name == "ifbench":
            entry.update(ifbench_levels(mine))
        if name == "niah":
            entry["grid"] = niah_grid(mine)
        out[name] = entry
    return out


# ------------------------------------------------------------------------------------------- running
def build_suites(args):
    names = [s for s in args.suites.split(",") if s]
    repeats = parse_repeats(args.repeats, names)
    suites = []
    for name in names:
        max_tokens, default_repeats = SUITE_DEFAULTS[name]
        rep = repeats.get(name, default_repeats)
        if name == "aime":
            suites.append(Suite(name, load_aime(args.data_dir), score_aime, max_tokens, rep))
        elif name == "kobalt":
            suites.append(Suite(name, load_kobalt(args.data_dir), score_kobalt, max_tokens, rep,
                                breakdown=("class", "level")))
        elif name == "click":
            suites.append(Suite(name, load_click(args.data_dir), score_click, max_tokens, rep,
                                breakdown=("group", "category")))
        elif name == "ifbench":
            root, items = load_ifbench(args.data_dir)
            suites.append(Suite(name, items, ifbench_scorer(IFBenchVerifier(root)), max_tokens, rep))
        elif name == "niah":
            lengths = [int(x) for x in args.niah_lengths.split(",")]
            suites.append(Suite(name, niah_items(lengths, args.max_model_len), score_niah, max_tokens, rep,
                                thinking=False, sampling=GREEDY, length_is_wrong=False, breakdown=("length",)))
        else:
            raise SystemExit(f"unknown suite {name}")
        if args.limit:
            suites[-1].items = suites[-1].items[:args.limit]
    return suites


def parse_repeats(text, names):
    if not text:
        return {}
    if "=" not in text:
        return {name: int(text) for name in names}
    return {key: int(value) for key, value in (part.split("=") for part in text.split(",") if part)}


def default_run(suites, args):
    config = {"suites": {s.name: [len(s.items), s.repeats, s.max_tokens] for s in suites}, "seed": args.seed,
              "sampling": SAMPLING, "pins": [AIME["rev"], KOBALT["rev"], CLICK["rev"], IFBENCH["rev"]]}
    return "ev-" + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:10]


def plan_of(suites):
    return {s.name: {"items": len(s.items), "repeats": s.repeats, "max_tokens": s.max_tokens, "thinking": s.thinking,
                     "sampling": s.sampling, "breakdown": list(s.breakdown)} for s in suites}


class Runner:
    def __init__(self, args, run, sink, admission, priority):
        self.args, self.run_id, self.sink, self.admission, self.priority = args, run, sink, admission, priority
        self.job = os.environ.get("AZUREML_RUN_ID", "")
        self.stop = threading.Event()

    def body(self, suite, item, rep, messages):
        body = {"model": MODEL, "messages": messages, "max_tokens": suite.max_tokens, "stream": True,
                "stream_options": {"include_usage": True},
                "chat_template_kwargs": {"enable_thinking": suite.thinking},
                "seed": int(hashlib.sha256(unit_id(suite.name, item["key"], rep).encode()).hexdigest()[:7], 16)}
        body.update(suite.sampling)
        if self.priority:
            body["priority"] = self.priority
        return body

    def messages(self, suite, item):
        if item["messages"] is not None:
            return item["messages"]
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        from verify_suites import build_niah

        content, built = build_niah(self.args.base, item["target"], item["depth"], item["gold"])
        item["meta"]["built_tokens"] = built
        return [{"role": "user", "content": content}]

    def generate(self, suite, item, rep):
        """(reply, error, attempts); connection failures wait for /health and retry, a 400 does not."""
        error, attempt = None, 0
        while attempt < self.args.retries and not self.stop.is_set():
            attempt += 1
            try:
                return stream_chat(self.args.base, self.body(suite, item, rep, self.messages(suite, item)),
                                   stop=self.stop), None, attempt
            except urllib.error.HTTPError as exc:
                error = f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}"
                if exc.code == 400:
                    break
            except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, RuntimeError) as exc:
                error = f"{type(exc).__name__}: {exc}"[:400]
            if self.stop.is_set() or attempt == self.args.retries:
                break
            print(f"AXK2_EVALS_WARN {unit_id(suite.name, item['key'], rep)} attempt {attempt}: {error}", flush=True)
            if not wait_healthy(self.args.base, stop=self.stop):
                break
            time.sleep(min(60.0, RETRY_BACKOFF * attempt))
        return None, error or "stopped", attempt

    def one(self, suite, item, rep):
        record = {"run": self.run_id, "job": self.job, "suite": suite.name, "key": item["key"], "rep": rep,
                  "gold": item["gold"], "meta": item["meta"]}
        reply, error, attempts = self.generate(suite, item, rep)
        record.update(attempts=attempts, t=round(time.time(), 1))
        if reply is None:
            record.update(error=error, correct=None)
            return record
        content = final_answer(reply["content"])
        try:
            record.update(suite.score(item, content))
        except Exception as exc:  # a verifier failure on one response must not stop the run; reported separately
            record.update(correct=False, pred=None, score_error=f"{type(exc).__name__}: {exc}"[:300])
        hit_limit = reply["finish_reason"] == "length"
        record.update(finish_reason=reply["finish_reason"], hit_limit=hit_limit,
                      prompt_tokens=reply["usage"].get("prompt_tokens"),
                      completion_tokens=reply["usage"].get("completion_tokens"),
                      reasoning_chars=reply["reasoning_chars"], content_chars=len(content), ttft=reply["ttft"],
                      seconds=reply["seconds"])
        if hit_limit and suite.length_is_wrong:
            record["correct"] = False
        return record

    def worker(self, queue, lock):
        while not self.stop.is_set() and self.admission.acquire(lambda: self.stop.is_set() or not queue):
            try:
                with lock:
                    if self.stop.is_set() or not queue:
                        return
                    suite, item, rep = queue.popleft()
                record = self.one(suite, item, rep)
                if record.get("error") and self.stop.is_set():
                    return  # interrupted, not failed: the next job redoes it
                self.sink.add(record)
                mark = "x" if record.get("error") else ("o" if record["correct"] else "-")
                print(f"AXK2_EVAL {mark} {unit_id(suite.name, item['key'], rep)} {record.get('finish_reason')} "
                      f"{record.get('completion_tokens')} tok {record.get('seconds')} s", flush=True)
            finally:
                self.admission.release()

    def run(self, units):
        """NIAH prompts (up to 256K tokens) get their own few workers so they never hold the others' slots."""
        lock = threading.Lock()
        pools = [(deque(u for u in units if u[0].name != "niah"), self.args.cap),
                 (deque(u for u in units if u[0].name == "niah"), max(1, self.args.niah_parallel))]
        pools = [(queue, n) for queue, n in pools if queue]
        with futures.ThreadPoolExecutor(max(1, sum(n for _, n in pools))) as pool:
            jobs = [pool.submit(self.worker, queue, lock) for queue, n in pools for _ in range(n)]
            for job in jobs:
                job.result()


def report(out_dir, key, data, series=False):
    path = Path(out_dir) / f"{key}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    if os.environ.get("AXK2_REPORT_PKGS"):
        env["PYTHONPATH"] = os.environ["AXK2_REPORT_PKGS"]
    command = [sys.executable, str(REPORT), key, "--file", str(path)] + (["--series"] if series else [])
    subprocess.run(command, env=env, check=False, timeout=900)


def compact(records):
    keys = ("suite", "key", "rep", "correct", "pred", "gold", "finish_reason", "prompt_tokens", "completion_tokens",
            "seconds", "error")
    return [[r.get(k) for k in keys] for r in latest_records(records)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--suites", default="aime,kobalt,click,ifbench,niah")
    parser.add_argument("--repeats", default="", help="e.g. aime=4, or one number for every suite")
    parser.add_argument("--limit", type=int, default=0, help="first N items of each suite (smoke tests)")
    parser.add_argument("--run", default=os.environ.get("AXK2_EVAL_RUN", ""), help="default: hash of the config")
    parser.add_argument("--out-dir", default=os.environ.get("OUT", "outputs"))
    parser.add_argument("--data-dir", default="/tmp/axk2-evaldata")
    parser.add_argument("--priority", type=int, default=10, help="vLLM priority; the demo's chat uses 0")
    parser.add_argument("--start", type=int, default=40)
    parser.add_argument("--cap", type=int, default=56, help="keep below the server's --max-num-seqs (64) so "
                                                             "demo users always find a free sequence slot")
    parser.add_argument("--demo-cap", type=int, default=16, help="limit while demo users are active")
    parser.add_argument("--niah-parallel", type=int, default=2)
    parser.add_argument("--retries", type=int, default=4)
    parser.add_argument("--max-model-len", type=int, default=262144)
    parser.add_argument("--niah-lengths", default=",".join(map(str, NIAH_LENGTHS)))
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--link", default=os.environ.get("AXK2_LINK_HTTP", ""))
    args = parser.parse_args(argv)

    for path in filter(None, os.environ.get("AXK2_EVAL_PKGS", "").split(os.pathsep)):
        sys.path.insert(0, path)
    sys.path.insert(0, str(HERE))
    if not wait_healthy(args.base, limit=7200):
        print("AXK2_EVALS_FAIL server never became healthy", flush=True)
        return 2
    priority = 0
    if args.priority:
        try:
            priority = args.priority if priority_accepted(args.base, args.priority) else 0
        except Exception as exc:
            print(f"AXK2_EVALS_WARN priority preflight failed ({exc}); sending priority 0", flush=True)
    if not priority:
        print("AXK2_EVALS_WARN server lacks --scheduling-policy priority; evals compete with demo users", flush=True)
    suites = build_suites(args)
    run = args.run or default_run(suites, args)
    out = Path(args.out_dir) / "evals" / run
    sink = Sink(out / "records.jsonl", run, args.link, os.environ.get("AXK2_LINK_TOKEN", ""))
    plan = plan_of(suites)
    records = sink.previous()
    done = completed(records)
    units = [(s, item, rep) for s in suites for rep in range(s.repeats) for item in s.items
             if unit_id(s.name, item["key"], rep) not in done]
    queue = order(units, args.seed)
    header = {"run": run, "plan": plan, "priority": priority, "resumed": len(done), "todo": len(queue),
              "pins": {"aime": AIME["rev"], "kobalt": KOBALT["rev"], "click": CLICK["rev"],
                       "ifbench": IFBENCH["rev"]}}
    print("AXK2_EVALS_START " + json.dumps(header), flush=True)
    sink.summary({"plan": plan, "summary": summarize(records, plan), "state": "running"})

    admission = Admission(args.base, args.start, args.cap, args.demo_cap)
    runner = Runner(args, run, sink, admission, priority)
    threading.Thread(target=admission.run, daemon=True).start()

    def on_term(*_):
        print("AXK2_EVALS_STOP SIGTERM: uploading what is finished", flush=True)
        runner.stop.set()
        admission.stop.set()
        threading.Thread(target=sink.flush, daemon=True).start()
    signal.signal(signal.SIGTERM, on_term)

    def periodic():
        last_summary = last_report = time.time()
        while not runner.stop.wait(20):
            sink.flush()
            now = time.time()
            if now - last_summary >= 600 or now - last_report >= 3600:
                current = {"plan": plan, "summary": summarize(records + read_jsonl(sink.path), plan),
                           "state": "running", "admission": admission.last}
                if now - last_summary >= 600:
                    last_summary = now
                    sink.summary(current)
                if now - last_report >= 3600:
                    last_report = now
                    report(out, f"evals.{run}.summary", current)
    threading.Thread(target=periodic, daemon=True).start()

    began = time.time()
    runner.run(queue)
    stopped = runner.stop.is_set()  # SIGTERM: unfinished units stay open for the next job to resume
    runner.stop.set()
    admission.stop.set()
    for _ in range(10):
        if sink.flush():
            break
        time.sleep(15)
    sink.close()
    final = records + read_jsonl(sink.path)
    result = {"plan": plan, "summary": summarize(final, plan), "state": "stopped" if stopped else "done",
              "seconds": round(time.time() - began, 1), "priority": priority}
    sink.summary(result)
    report(out, f"evals.{run}.summary", result)
    report(out, f"evals.{run}.records", compact(final), series=True)
    print(("AXK2_EVALS_STOPPED " if stopped else "AXK2_EVALS_DONE ")
          + json.dumps(result["summary"], ensure_ascii=False)[:3000], flush=True)
    return 3 if stopped else 0


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


if __name__ == "__main__":
    sys.exit(main())
