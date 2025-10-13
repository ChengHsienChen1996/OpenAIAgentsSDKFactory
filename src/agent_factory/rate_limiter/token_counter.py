import tiktoken

from typing import Optional

from tiktoken.core import Encoding


def _get_encoder(model=None) -> Optional[Encoding]:
    try:
        if model:
            try: return tiktoken.encoding_for_model(model)
            except Exception: pass
        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        return None


def count_tokens(text: str, model: str | None = None) -> int:
    enc = _get_encoder(model)
    return len(enc.encode(text or "")) if enc else max(1, (len(text or "") + 3)//4)
