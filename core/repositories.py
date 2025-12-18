# core/repositories.py
from __future__ import annotations

from typing import Any, Dict, List, Optional
from datetime import datetime

from .database import get_col

# coleções
_state  = lambda: get_col("state_data")
_hist   = lambda: get_col("history")
_events = lambda: get_col("events")


# ---------- helpers internos ----------
def _delete_dotted(root: Dict[str, Any], dotted_key: str) -> bool:
    """
    Remove a chave `dotted_key` (ex.: "perfil.endereco.rua") de um dict aninhado,
    modificando `root` in-place. Faz prune de dicts vazios no caminho.
    Retorna True se removeu algo.
    """
    if not dotted_key:
        return False

    parts = [p for p in dotted_key.split(".") if p]
    if not parts:
        return False

    stack: List[tuple[Dict[str, Any], str]] = []
    cur: Any = root

    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur:
            return False
        stack.append((cur, p))
        cur = cur[p]

    leaf = parts[-1]
    if not isinstance(cur, dict) or leaf not in cur:
        return False

    # remove a folha
    del cur[leaf]

    # prune dicts vazios, de baixo pra cima
    while stack:
        parent, key = stack.pop()
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            del parent[key]
        else:
            break

    return True


# ---------- Fatos ----------
def get_facts(usuario: str) -> Dict[str, Any]:
    d = _state().find_one({"usuario": usuario})
    return d.get("fatos", {}) if d else {}


def get_fact(usuario: str, key: str, default: Any = None) -> Any:
    d = _state().find_one({"usuario": usuario})
    if not d:
        return default
    cur: Any = d.get("fatos", {})
    for part in (key or "").split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


from datetime import datetime

def set_fact(usuario: str, key: str, value: Any, meta: Optional[Dict[str, Any]] = None) -> None:
    """
    Seta um fact (chave pontilhada vira 'fatos.<key>').
    Meta: guarda por chave em 'meta.<key>' e atualiza meta.updated_at.
    """
    meta = meta or {}
    _state().update_one(
        {"usuario": usuario},
        {"$set": {
            "usuario": usuario,
            f"fatos.{key}": value,
            f"meta.{key}": meta,
            "meta.updated_at": datetime.utcnow(),
        }},
        upsert=True
    )


def delete_fact(usuario: str, key: str) -> bool:
    """
    Remove uma memória canônica (suporta chave pontilhada).
    Atualiza o bloco 'fatos' inteiro (mais robusto que depender de $unset).
    """
    doc = _state().find_one({"usuario": usuario})
    if not doc:
        return False

    facts = dict(doc.get("fatos", {}) or {})
    if not _delete_dotted(facts, key):
        return False

    _state().update_one({"usuario": usuario}, {"$set": {"fatos": facts}}, upsert=True)
    return True


# ---------- Histórico ----------
def save_interaction(usuario: str, mensagem_usuario: str, resposta_mary: str, model_tag: str) -> None:
    """
    Salva um turno de conversa. Mantém o campo legado 'resposta_mary' (UI depende dele).
    """
    _hist().insert_one({
        "usuario": usuario,
        "mensagem_usuario": mensagem_usuario,
        "resposta_mary": resposta_mary,
        "model": model_tag,
        "ts": datetime.utcnow(),  # ordenação estável
    })


def get_history_docs(usuario: str, limit: int = 400) -> List[Dict[str, Any]]:
    """
    Ordena por ts asc; fallback _id asc.
    Robustez: docs legados sem ts continuam ordenando por _id.
    """
    cur = _hist().find(
        {"usuario": usuario},
        sort=[("ts", 1), ("_id", 1)],
        limit=limit,
    )
    return list(cur)


def get_history_docs_multi(
    users_or_keys: List[str],
    limit: int = 400,
    limit_per_key: int = 400,
) -> List[Dict[str, Any]]:
    """
    Histórico unificado para várias chaves (ex.: ["Janio::mary", "Janio"]).

    - Busca por key separadamente (evita que uma key "roube" todo o limit).
    - Faz merge + sort por ts asc (fallback _id asc).
    - Retorna no máximo `limit` docs finais.
    """
    keys = [k for k in (users_or_keys or []) if k]
    if not keys:
        return []

    all_docs: List[Dict[str, Any]] = []
    for k in keys:
        cur = _hist().find(
            {"usuario": k},
            sort=[("ts", 1), ("_id", 1)],
            limit=limit_per_key,
        )
        all_docs.extend(list(cur))

    def _sort_key(d: Dict[str, Any]):
        ts = d.get("ts")
        # ts ideal é datetime; legado pode não ter ts
        if not isinstance(ts, datetime):
            ts = datetime.min
        return (ts, d.get("_id"))

    all_docs.sort(key=_sort_key)

    if limit and len(all_docs) > limit:
        return all_docs[-limit:]
    return all_docs


def delete_user_history(usuario: str) -> int:
    r = _hist().delete_many({"usuario": usuario})
    return int(getattr(r, "deleted_count", 0))


def delete_last_interaction(usuario: str) -> bool:
    """
    Remove o último turno (maior ts; fallback _id).
    Robusto para docs legados sem 'ts'.
    """
    last = _hist().find_one({"usuario": usuario}, sort=[("ts", -1), ("_id", -1)])
    if not last:
        last = _hist().find_one({"usuario": usuario}, sort=[("_id", -1)])

    if not last:
        return False

    r = _hist().delete_one({"_id": last["_id"]})

    if isinstance(r, dict):
        return int(r.get("deleted_count", 0) or 0) > 0

    return int(getattr(r, "deleted_count", 0) or 0) > 0




# ---------- Eventos ----------
def register_event(
    usuario: str,
    tipo: str,
    descricao: str,
    local: Optional[str],
    extra: Optional[Dict[str, Any]] = None
) -> None:
    _events().insert_one({
        "usuario": usuario,
        "tipo": tipo,
        "descricao": descricao,
        "local": local,
        "extra": extra or {},
        "ts": datetime.utcnow(),
    })


def list_events(usuario: str, limit: int = 5) -> List[Dict[str, Any]]:
    cur = _events().find(
        {"usuario": usuario},
        sort=[("ts", -1), ("_id", -1)],
        limit=limit,
    )
    return list(cur)


# ---------- Utilidades ----------
def last_event(usuario: str, tipo: str) -> Optional[Dict[str, Any]]:
    return _events().find_one(
        {"usuario": usuario, "tipo": tipo},
        sort=[("ts", -1), ("_id", -1)]
    )


def _safe_create_index(col_obj, keys):
    """
    Tenta criar índice tanto em coleções pymongo puras (create_index)
    quanto em wrappers (obj._col.create_index).
    """
    try:
        if hasattr(col_obj, "create_index"):
            col_obj.create_index(keys)
            return True
    except Exception:
        pass

    try:
        inner = getattr(col_obj, "_col", None)
        if inner is not None and hasattr(inner, "create_index"):
            inner.create_index(keys)
            return True
    except Exception:
        pass

    return False


def ensure_indexes() -> None:
    """
    Garante índices essenciais.
    Só roda quando backend for mongo.
    """
    try:
        from .database import get_backend
        if get_backend() != "mongo":
            return

        # History: busca por usuario, ordenado por data
        _safe_create_index(_hist(), [("usuario", 1), ("ts", 1), ("_id", 1)])

        # State: busca por usuario
        _safe_create_index(_state(), [("usuario", 1)])

        # Events: busca por usuario, mais recentes
        _safe_create_index(_events(), [("usuario", 1), ("ts", -1), ("_id", -1)])

    except Exception:
        pass
