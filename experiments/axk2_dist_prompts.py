"""Add a bounded set of real embedding rows for synthetic Korean/English text."""
import concurrent.futures
import hashlib
import json

from transformers import AutoTokenizer
from axk2_dist_weights import ROOT, REVISION, get_json, read_range

TEXT = """
Azure에서 대규모 언어 모델을 여러 GPU에 나누어 실행하는 방법을 설명해 주세요.
첫 번째 서버는 앞쪽 레이어를 계산하고, 중간 활성값을 private network로 다음 서버에 전달합니다.
두 번째 서버는 이어지는 레이어와 mixture-of-experts 연산을 수행합니다.
동일한 입력을 단일 GPU에서 순서대로 계산한 결과와 비교해 수치 오차를 확인합니다.
H100을 구하기 어려운 환경에서 A100을 사용하는 경우 메모리, 네트워크와 커널 호환성을 검토해야 합니다.
Model weights must be partitioned across nodes rather than duplicated as independent replicas.
Pipeline parallelism sends intermediate hidden states between consecutive decoder stages.
Tensor parallelism and expert parallelism have different communication patterns.
A correctness experiment is not a production throughput benchmark or an answer-quality evaluation.
The cache retains prior attention keys and values while each new input token is processed.
오늘의 실험은 학습을 수행하지 않고 공개된 사전학습 가중치를 그대로 사용합니다.
"""


def main():
    tokenizer = AutoTokenizer.from_pretrained("skt/A.X-K2", revision=REVISION, trust_remote_code=False)
    token_ids = tokenizer(TEXT, add_special_tokens=True)["input_ids"]
    unique = sorted(set(token_ids))
    assert 1 < len(unique) < 2048
    index = get_json("model.safetensors.index.json")["weight_map"]
    name = "model.embed_tokens.weight"
    shard = index[name]
    header_size = int.from_bytes(read_range(shard, 0, 8), "little")
    assert header_size < 4 * 1024**2
    header = json.loads(read_range(shard, 8, header_size))
    metadata = header[name]
    assert metadata["dtype"] == "BF16" and metadata["shape"] == [163840, 7168]
    row_bytes = 7168 * 2
    base = 8 + header_size + metadata["data_offsets"][0]
    groups = []
    for token in unique:
        if not groups or (token - groups[-1][-1]) * row_bytes > 256 * 1024:
            groups.append([])
        groups[-1].append(token)
    transfer_bytes = sum((group[-1] - group[0] + 1) * row_bytes for group in groups)
    assert transfer_bytes < 128 * 1024**2

    def fetch(group):
        data = read_range(shard, base + group[0] * row_bytes, (group[-1] - group[0] + 1) * row_bytes)
        return {
            token: data[(token - group[0]) * row_bytes:(token - group[0] + 1) * row_bytes]
            for token in group
        }

    rows = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        for result in pool.map(fetch, groups):
            rows.update(result)
    data = b"".join(rows[token] for token in unique)
    (ROOT / "prompt-embeddings.bin").write_bytes(data)
    lookup = {token: offset for offset, token in enumerate(unique)}
    result = {
        "scope": "Synthetic bilingual text repeated to context length; not generated answers.",
        "revision": REVISION,
        "text": TEXT,
        "text_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
        "token_ids": token_ids,
        "unique_token_ids": unique,
        "embedding_offsets": [lookup[token] for token in token_ids],
        "shape": [len(unique), 7168],
        "dtype": "BF16",
        "embedding_sha256": hashlib.sha256(data).hexdigest(),
        "download_bytes": transfer_bytes,
    }
    (ROOT / "prompt-data.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({key: value for key, value in result.items()
                      if key not in ("text", "token_ids", "unique_token_ids", "embedding_offsets")}))


if __name__ == "__main__":
    main()
