# tokens.py
from __future__ import annotations

from typing import Optional

# Cache simples do encoder
_TIKTOKEN_ENCODER = None

_TIKTOKEN_ENCODER = None

def _get_encoder(model: str | None = None):
    global _TIKTOKEN_ENCODER
    try:
        import tiktoken
        if model:
            try:
                return tiktoken.encoding_for_model(model)
            except Exception:
                pass
        if _TIKTOKEN_ENCODER is None:
            _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
        return _TIKTOKEN_ENCODER
    except Exception:
        return None


def toklen(txt: str, model: str | None = None) -> int:
    t = (txt or "")
    if not t.strip():
        return 1

    enc = _get_encoder(model)
    if enc is not None:
        try:
            return max(1, len(enc.encode(t)))
        except Exception:
            pass

    # fallback robusto
    chars_est = int(len(t) / 4.0)
    words_est = int(len(t.split()) * 1.3)
    return max(1, max(chars_est, words_est))


def _get_encoder(model: Optional[str] = None):
    """
    Retorna um encoder do tiktoken.
    - Tenta encoding_for_model(model) quando disponível.
    - Cai para cl100k_base (bom padrão) se não souber.
    """
    global _TIKTOKEN_ENCODER
    try:
        import tiktoken  # type: ignore

        if model:
            try:
                return tiktoken.encoding_for_model(model)
            except Exception:
                pass

        if _TIKTOKEN_ENCODER is None:
            _TIKTOKEN_ENCODER = tiktoken.get_encoding("cl100k_base")
        return _TIKTOKEN_ENCODER
    except Exception:
        return None


def toklen(txt: str, model: Optional[str] = None) -> int:
    """
    Conta/estima tokens de forma robusta.

    Preferência:
    1) tiktoken (real)
    2) fallback heurístico: max(chars/4, words*1.3)
    """
    t = (txt or "")
    if not t.strip():
        return 1

    enc = _get_encoder(model)
    if enc is not None:
        try:
            return max(1, len(enc.encode(t)))
        except Exception:
            pass

    # Fallback robusto (mais estável que split puro)
    # - chars/4 aproxima tokenização BPE (varia, mas é melhor do que palavras)
    # - words*1.3 evita subestimar textos com muitas palavras curtas
    chars_est = int(len(t) / 4.0)
    words_est = int(len(t.split()) * 1.3)
    return max(1, max(chars_est, words_est))


def toklen_messages(messages: list[dict], model: Optional[str] = None) -> int:
    """
    Útil se você quiser medir o prompt inteiro (lista de messages).
    Considera apenas 'content' + uma pequena folga por estrutura.
    """
    total = 0
    for m in (messages or []):
        c = (m.get("content") or "")
        total += toklen(c, model=model)
        # overhead estrutural por message (aprox.)
        total += 4
    return max(1, total)
