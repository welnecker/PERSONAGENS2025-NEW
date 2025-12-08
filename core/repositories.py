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
def _delete_dotted(d: Dict[str, Any], dotted: str) -> bool:
    """
    Remove a chave 'a.b.c' de um dict aninhado, limpando dicts vazios no caminho.
    Retorna True se removeu algo.
    """
    parts = [p for p in (dotted or "").split(".") if p]
    if not parts:
        return False

    stack = [d]
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur, dict) or p not in cur or not isinstance(cur[p], dict):
            return False
        cur = cur[p]
        stack.append(cur)

    last = parts[-1]
    if not isinstance(cur, dict) or last not in cur:
        return False

    # remove a folha
    del cur[last]

    # cleanup: remove dicts vazios do fim para o início (exceto a raiz)
    for i in range(len(stack) - 1, 0, -1):
        node = stack[i]
        parent = stack[i - 1]
        # nome da chave deste node dentro do parent
        for k, v in list(parent.items()):
            if v is node and isinstance(v, dict) and not v:
                del parent[k]
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
    cur = d.get("fatos", {})
    # acesso raso ou pontilhado
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_fact(usuario: str, key: str, value: Any, meta: Optional[Dict[str, Any]] = None) -> None:
    meta = meta or {}
    _state().update_one(
        {"usuario": usuario},
        {"$set": {f"usuario": usuario, f"fatos.{key}": value, "meta": meta}},
        upsert=True
    )


def _delete_dotted(root: Dict[str, Any], dotted_key: str) -> bool:
    """
    Remove a chave `dotted_key` (ex.: "perfil.endereco.rua") de um dict aninhado,
    modificando `root` in-place. Faz prune de dicts vazios no caminho.
    Retorna True se removeu algo.
    """
    if not dotted_key:
        return False
    parts = dotted_key.split(".")
    stack = []
    cur = root
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


def delete_fact(usuario: str, key: str) -> bool:
    """
    Remove uma memória canônica (suporta chave pontilhada).
    Compatível com backend de memória e Mongo.
    """
    doc = _state().find_one({"usuario": usuario})
    if not doc:
        return False

    facts = dict(doc.get("fatos", {}) or {})
    if not _delete_dotted(facts, key):
        return False

    # Atualiza 'fatos' como bloco (evita depender de $unset no backend memória)
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
    Histórico por uma única chave de usuário/personagem.
    Ordena por ts asc; fallback _id asc.
    """
    return list(_hist().find(
        {"usuario": usuario},
        sort=[("ts", 1), ("_id", 1)],
        limit=limit
    ))


def get_history_docs_multi(users_or_keys: List[str], limit: int = 400) -> List[Dict[str, Any]]:
    """
    Histórico unificado para várias chaves (ex.: ["Janio::laura", "Janio"]).
    Útil para Mary (legado) + chave nova por persona.
    """
    keys = [k for k in (users_or_keys or []) if k]
    if not keys:
        return []
    return list(_hist().find(
        {"usuario": {"$in": keys}},
        sort=[("ts", 1), ("_id", 1)],
        limit=limit
    ))


def delete_user_history(usuario: str) -> int:
    return _hist().delete_many({"usuario": usuario})


def delete_last_interaction(usuario: str) -> bool:
    """
    Remove o último turno (maior ts; fallback _id).
    """
    last = _hist().find_one({"usuario": usuario}, sort=[("ts", -1), ("_id", -1)])
    if not last:
        return False
    deleted = _hist().delete_many({"_id": last["_id"]})
    return deleted > 0


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
    return list(_events().find(
        {"usuario": usuario},
        sort=[("ts", -1), ("_id", -1)],
        limit=limit
    ))


# ---------- Utilidades ----------
def last_event(usuario: str, tipo: str) -> Optional[Dict[str, Any]]:
    return _events().find_one(
        {"usuario": usuario, "tipo": tipo},
        sort=[("ts", -1), ("_id", -1)]
    )


    return {
        "hist": _hist().delete_many({"usuario": usuario}),
        "state": _state().delete_many({"usuario": usuario}),
        "eventos": _events().delete_many({"usuario": usuario}),
        "perfil": 0,
    }


def ensure_indexes() -> None:
    """
    Garante que os índices essenciais existam (Melhora performance e evita full-scan).
    """
    try:
        from .database import get_backend
        if get_backend() != "mongo":
            return

        # History: busca por usuario, ordenado por data
        _hist()._col.create_index([("usuario", 1), ("ts", 1), ("_id", 1)])
        
        # State: busca por usuario (chave única conceitual)
        _state()._col.create_index([("usuario", 1)])
        
        # Events: busca por usuario, mais recentes
        _events()._col.create_index([("usuario", 1), ("ts", -1)])
        
    except Exception:
        # Silencia erros de indexação para não quebrar o app
        pass
