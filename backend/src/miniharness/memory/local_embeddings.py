"""本地 Sentence Transformers ONNX 模型，以注意力掩码做平均池化。"""

import json
import os
from functools import lru_cache
from pathlib import Path
from threading import Lock

from miniharness.runtime.errors import HarnessError

_load_lock = Lock()


@lru_cache(maxsize=1)
def _load(directory: Path):
    try:
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
    except ImportError as exc:
        raise HarnessError("本地嵌入需要安装 miniharness2[local-embeddings]。") from exc
    tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
    config = json.loads((directory / "sentence_bert_config.json").read_text(encoding="utf-8"))
    pooling = json.loads((directory / "1_Pooling" / "config.json").read_text(encoding="utf-8"))
    if not pooling.get("pooling_mode_mean_tokens") or any(
        on
        for key, on in pooling.items()
        if key.startswith("pooling_mode_") and key != "pooling_mode_mean_tokens"
    ):
        raise HarnessError("此本地嵌入适配器需要使用平均池化的句向量模型。")
    tokenizer.enable_truncation(max_length=config["max_seq_length"])
    options = ort.SessionOptions()
    options.intra_op_num_threads = min(os.cpu_count() or 1, 4)
    session = ort.InferenceSession(
        str(directory / "model.onnx"), sess_options=options, providers=["CPUExecutionProvider"]
    )
    return np, tokenizer, session


def encode(directory: str, text: str) -> tuple[list[float], int]:
    with _load_lock:
        np, tokenizer, session = _load(Path(directory).resolve())
    encoded = tokenizer.encode(text)
    fields = {
        "input_ids": encoded.ids,
        "attention_mask": encoded.attention_mask,
        "token_type_ids": encoded.type_ids,
    }
    feed = {
        item.name: np.asarray([fields[item.name]], dtype=np.int64) for item in session.get_inputs()
    }
    hidden = session.run(None, feed)[0][0]
    mask = np.asarray(encoded.attention_mask, dtype=np.float32)
    token_count = int(mask.sum())
    pooled = (hidden * mask[:, None]).sum(axis=0) / max(token_count, 1)
    norm = float(np.linalg.norm(pooled))
    if not np.isfinite(norm) or norm == 0:
        raise HarnessError("本地嵌入模型返回了无效的向量。")
    return (pooled / norm).tolist(), token_count
