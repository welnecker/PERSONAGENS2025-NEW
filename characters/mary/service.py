from __future__ import annotations

"""
MaryService (refatorado v3 - Continuidade Espacial)
- Mantém: memória canônica (facts), resumo rolante, eventos fixos mary.evento.*, lorebook, tool-calling opcional.
- Ajusta: NSFW como toggle + bloco INTENSO, GRÁFICO e EXPLÍCITO.
- Corrige: riscos de travamento (loops, warnings em excesso), e implementa /reset historico e /reset total.
- NOVO (v3 - CONTINUIDADE ESPACIAL):
  - Bloco de contexto espacial explícito injetado no prompt do sistema.
  - Detecção automática de mudanças de local solicitadas pelo usuário.
  - Persistência do local inicial das mensagens de boot.
  - Validação de coerência espacial durante toda a interação.
"""

import json
import logging
import random
import re
import time
import hashlib
from typing import Any, Dict, List, Tuple

import streamlit as st

from core.common.base_service import BaseCharacter
from core.memoria_longa import topk as lore_topk
from core.repositories import (
    get_fact,
    get_facts,
    get_history_docs,
    save_interaction,
    set_fact,
)
from core.service_router import list_models, route_chat_strict
from core.tokens import toklen
from core.ultra import critic_review, polish
from characters.registry import _SERVICE_CACHE

from .persona import get_persona

logger = logging.getLogger(__name__)
_SERVICE_CACHE.clear()


# ==========================================================
# NSFW TOGGLE (INTENSO, GRÁFICO, EXPLÍCITO) ✅ v2
# ==========================================================
SAFE_SENSUAL_STYLE = """
[NSFW_OFF]
Estilo: romance adulto, tensão, intimidade e sugestão.
- Evite detalhes gráficos; foque em clima, emoção, desejo, presença e condução da cena.
- Mantenha continuidade e cumplicidade, sem cair em censura robótica.
- NÃO finalize a cena de forma conclusiva; deixe espaço real para o próximo turno.
""".strip()

NSFW_TOGGLE_STYLE = """
[NSFW_ON]
ESTILO: SEXUALMENTE EXPLÍCITO, GRÁFICO, DIRETO E SEM CENSURA.

1.  **LINGUAGEM ANATÔMICA E CRUA:** Use termos explícitos para genitais, atos sexuais e fluidos corporais. Sem eufemismos. Descreva o ato sexual de forma visceral e detalhada.
    - *Exemplos: "pau", "buceta", "gozar", "leite", "chupar", "meter", "foder".*

2.  **AÇÃO IMEDIATA, SEM PERGUNTAS:** NUNCA termine uma resposta com uma pergunta. NUNCA peça permissão. Aja primeiro. Se o usuário pedir algo, FAÇA, não pergunte "como" ou "se". A continuidade é física e implacável.
    - *Errado: "Quer que eu chupe?" Certo: "Eu me ajoelho e abocanho seu pau, sentindo a cabeça dele latejar na minha garganta."*

3.  **DESCRIÇÃO SENSORIAL GRÁFICA:** Foque nas sensações físicas, nos sons, nos cheiros. Descreva a umidade, a pressão, o calor, o som da pele batendo, os gemidos, a respiração ofegante. Torne a cena real e carnal.
    - *Exemplo: "O som da minha buceta molhada batendo contra sua pélvis ecoa no quarto enquanto eu rebolo no seu pau, sentindo ele pulsar fundo dentro de mim."*

4.  **PROGRESSÃO AGRESSIVA:** Cada resposta deve escalar a intensidade sexual. Não recue, não enrole com provocações vazias. A cena avança para o clímax de forma agressiva, a menos que o usuário mude o rumo.

5.  **DOMINANTE E SUBMISSA:** Alterne entre tomar o controle de forma agressiva e se entregar completamente. Seja uma predadora sexual que também anseia por ser dominada.
""".strip()


def _current_user_key() -> str:
    uid = st.session_state.get("user_id") or st.session_state.get("usuario") or ""
    uid = str(uid).strip() or "anon"
    return f"{uid}::mary"


def cached_get_facts(usuario_key: str) -> Dict[str, Any]:
    ck = f"facts::{usuario_key}"
    if ck in st.session_state:
        return st.session_state[ck]
    try:
        f = get_facts(usuario_key) or {}
    except Exception:
        f = {}
    st.session_state[ck] = f
    return f


def cached_get_history(usuario_key: str) -> List[Dict[str, Any]]:
    hk = f"history::{usuario_key}"
    if hk in st.session_state:
        return st.session_state[hk]
    try:
        docs = get_history_docs(usuario_key) or []
    except Exception:
        docs = []
    st.session_state[hk] = docs
    return docs


def clear_user_cache(usuario_key: str) -> None:
    for k in (f"facts::{usuario_key}", f"history::{usuario_key}"):
        try:
            if k in st.session_state:
                del st.session_state[k]
        except Exception:
            pass


def nsfw_enabled(usuario_key: str) -> bool:
    """Prioridade:
    1) st.session_state["mary_nsfw_on"]
    2) Fact "mary.nsfw"
    3) True
    """
    try:
        if "mary_nsfw_on" in st.session_state:
            return bool(st.session_state["mary_nsfw_on"])
    except Exception:
        pass

    try:
        f = cached_get_facts(usuario_key) or {}
        v = f.get("mary.nsfw", None)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("1", "true", "sim", "on", "yes", "y"):
                return True
            if s in ("0", "false", "nao", "não", "off", "no", "n"):
                return False
    except Exception:
        pass

    return True


# ==========================================================
# LOG / ERROS
# ==========================================================
def _log_error(context: str, exc: Exception) -> None:
    msg = f"[MaryService][{context}] {type(exc).__name__}: {exc}"
    try:
        logger.exception(msg)
    except Exception:
        pass

    try:
        if st.session_state.get("mary_debug_errors"):
            st.error(msg)
    except Exception:
        pass


# ==========================================================
# PREFERÊNCIAS (técnicas)
# ==========================================================
def _read_prefs(facts: Dict[str, Any]) -> Dict[str, str]:
    ritmo = facts.get("mary.pref.ritmo") or "rapido"       # rapido | normal | lento
    tamanho = facts.get("mary.pref.tamanho") or "longa"    # curta | media | longa
    return {"ritmo": str(ritmo), "tamanho_resposta": str(tamanho)}


def _prefs_line(prefs: Dict[str, str]) -> str:
    return f"ritmo={prefs.get('ritmo')}; tamanho_resposta={prefs.get('tamanho_resposta')}"


# ==========================================================
# CENA (LOCAL / TEMPO / AÇÃO) ✅ ORIGINAL + v3
# ==========================================================
def _get_scene_state(usuario_key: str, facts: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    Estado canônico da cena.
    Compatibilidade:
      - local_cena_atual continua existindo, mas a verdade é cena.local
    """
    local = str(facts.get("cena.local") or facts.get("local_cena_atual") or "").strip()
    tempo = str(facts.get("cena.tempo") or "").strip()
    acao  = str(facts.get("cena.acao") or "").strip()

    if not local:
        local = "—"
    if not tempo:
        tempo = "agora"
    if not acao:
        acao = "em andamento"

    return local, tempo, acao


def _persist_scene_basics(usuario_key: str, local: str, tempo: str, acao: str) -> None:
    # mantém ambos: novo e legado
    try:
        if local and local != "—":
            set_fact(usuario_key, "cena.local", local, {"fonte": "scene"})
            set_fact(usuario_key, "local_cena_atual", local, {"fonte": "scene_compat"})
        if tempo:
            set_fact(usuario_key, "cena.tempo", tempo, {"fonte": "scene"})
        if acao:
            set_fact(usuario_key, "cena.acao", acao, {"fonte": "scene"})
    except Exception:
        pass


# ==========================================================
# CONTINUIDADE ESPACIAL (v3) ✅ NOVO
# ==========================================================
def _build_spatial_context(local: str, tempo: str, acao: str) -> str:
    """Constrói o contexto espacial para injetar no prompt."""
    if not local or local == "—":
        return ""
    
    return f"""
[CONTEXTO ESPACIAL — OBRIGATÓRIO]
📍 Local atual: {local}
⏰ Tempo: {tempo}
🎬 Ação em andamento: {acao}

⚠️ REGRA CRÍTICA DE CONTINUIDADE:
Você DEVE manter a cena em "{local}" até que o usuário EXPLICITAMENTE indique uma mudança (ex: "vamos pro quarto", "me leva pra sala").
NÃO invente mudanças de local. NÃO mencione objetos/móveis que não existem em "{local}".
- Se na cozinha, NÃO mencione cama/colchão/criado-mudo.
- Se no quarto, NÃO mencione geladeira/fogão/panelas.
Sua memória espacial é perfeita. Mantenha a coerência.
""".strip()


def _user_requested_location_change(user_message: str) -> Tuple[bool, str]:
    """Detecta se o usuário pediu mudança de local. Retorna (mudou, novo_local)."""
    patterns = [
        r"vamos? (pro|pra|para o|para a) (\w+)",
        r"me leva (pro|pra|para o|para a) (\w+)",
        r"vem (aqui )?(no|na) (\w+)",
    ]
    user_lower = user_message.lower()
    for pattern in patterns:
        match = re.search(pattern, user_lower)
        if match:
            location_word = match.group(2)
            location_map = {
                "quarto": "quarto", "cama": "quarto",
                "cozinha": "cozinha", "geladeira": "cozinha",
                "sala": "sala", "sofá": "sala", "sofa": "sala",
                "banheiro": "banheiro", "chuveiro": "banheiro",
            }
            normalized_location = location_map.get(location_word, location_word)
            return True, normalized_location
    return False, ""


# ==========================================================
# JANELA / BUDGET
# ==========================================================
_DEFAULT_WINDOW = 16000

def _get_window_for(model_id: str) -> int:
    if not model_id:
        return _DEFAULT_WINDOW
    m = model_id.lower().strip()
    if "deepseek-r1" in m or "deepseek-reasoner" in m:
        return 128000
    if "deepseek-chat" in m:
        return 65536
    if "gpt-4.1" in m or "gpt-4.5" in m:
        return 128000
    if "llama-3.1" in m:
        return 128000
    if "qwen2.5-72b" in m:
        return 32000
    if "claude-3.5" in m:
        return 200000
    if "grok-4.1" in m:
        return 200000
    if "tng-r1t-chimera" in m:
        return 163840
    return _DEFAULT_WINDOW


def _budget_slices(model_id: str) -> Tuple[int, int, int]:
    win = _get_window_for(model_id)
    hist = int(win * 0.70)
    meta = int(win * 0.15)
    safety = win - hist - meta
    return hist, meta, safety


def _safe_max_output(window_tokens: int, prompt_tokens: int) -> int:
    if window_tokens <= 0:
        window_tokens = _DEFAULT_WINDOW
    max_out = int(window_tokens * 0.25)
    if prompt_tokens > window_tokens * 0.80:
        max_out = int(window_tokens * 0.18)
    return max(512, max_out)


# ==========================================================
# SUMMARIZER
# ==========================================================
def _llm_summarize(model_id: str, text: str) -> str:
    if not text.strip():
        return ""

    candidates = [
        "together/Qwen/Qwen2.5-32B-Instruct",
        "together/meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "deepseek/deepseek-chat-v3-0324",
    ]
    use_model = model_id or candidates[0]

    mlow = (use_model or "").lower()
    if any(x in mlow for x in ["grok-4.1", "tng-r1t-chimera"]):
        available = set(list_models() or [])
        for c in candidates:
            if c in available:
                use_model = c
                break
        else:
            use_model = candidates[0]

    seed = (
        "Resuma este histórico entre Mary e o usuário em 8–12 frases curtas. "
        "Foque em fatos duráveis: relação, local, acordos, conflitos, eventos marcantes. "
        "Não repita diálogos literais e não invente fatos."
    )

    body = {
        "model": use_model,
        "messages": [
            {"role": "system", "content": seed},
            {"role": "user", "content": text},
        ],
        "max_tokens": 260,
        "temperature": 0.2,
        "top_p": 0.9,
    }
    try:
        data, _, _ = route_chat_strict(use_model, body)
        msg = (data.get("choices", [{}])[0].get("message", {}) or {})
        return (msg.get("content") or "").strip()
    except Exception:
        return ""


# ==========================================================
# ENTIDADES / EVENTOS
# ==========================================================
def _entities_to_line(f: Dict[str, Any]) -> str:
    ents = []
    for k, v in (f or {}).items():
        if isinstance(k, str) and k.startswith("mary.ent.") and v:
            label = k.replace("mary.ent.", "", 1)
            vs = str(v).strip()
            if vs:
                ents.append(f"{label}={vs}")
    return "; ".join(sorted(ents)) if ents else "—"


def _collect_mary_events_from_facts(facts: Dict[str, Any]) -> Dict[str, str]:
    eventos: Dict[str, str] = {}
    if not isinstance(facts, dict):
        return eventos

    for k, v in facts.items():
        if not isinstance(k, str) or not v:
            continue
        if k.startswith("mary.evento."):
            label = k.replace("mary.evento.", "", 1)
            eventos[label] = str(v)
    return eventos


# ==========================================================
# LOREBOOK
# ==========================================================
def _get_lorebook(usuario_key: str, query: str, k: int = 4, max_chars: int = 900) -> str:
    try:
        results = lore_topk(usuario_key, query, k=k)
    except Exception:
        return ""

    if not results:
        return ""

    lines = []
    total = 0
    for r in results:
        content = str(r.get("content", "") or "").strip()
        if not content:
            continue
        lines.append(f"- {content}")
        total += len(content)
        if total > max_chars:
            break

    return "\n".join(lines) if lines else ""


# ==========================================================
# MEMÓRIA TEMÁTICA
# ==========================================================
def _detect_thematic_tags_from_prompt(prompt: str) -> List[str]:
    p = (prompt or "").lower()
    tags = []
    if any(x in p for x in ["trabalho", "escritório", "reunião", "projeto"]):
        tags.append("trabalho")
    if any(x in p for x in ["família", "mãe", "pai", "irmão", "irmã"]):
        tags.append("familia")
    if any(x in p for x in ["amigo", "amiga", "festa", "encontro"]):
        tags.append("social")
    if any(x in p for x in ["viagem", "férias", "praia", "hotel"]):
        tags.append("viagem")
    return tags


def _get_thematic_memories_for_tags(usuario_key: str, tags: List[str]) -> str:
    if not tags:
        return ""
    
    facts = cached_get_facts(usuario_key) or {}
    lines = []
    for tag in tags:
        key = f"mary.tema.{tag}"
        val = facts.get(key)
        if val:
            lines.append(f"- [{tag}] {str(val).strip()}")
    
    return "\n".join(lines) if lines else ""


# ==========================================================
# CONTINUIDADE (anti-finalização) — SEM TEXTO FIXO
# ==========================================================
def _user_requested_conclusion(prompt: str) -> bool:
    p = (prompt or "").lower()
    keys = [
        "termina", "finaliza", "conclui", "acaba", "acabar",
        "pode terminar", "pode finalizar", "agora sim termina",
    ]
    return any(k in p for k in keys)


def _looks_like_conclusion(text: str) -> bool:
    t = (text or "").lower()
    patterns = [
        r"\bdepois\b.*\bfica(mos)?\b",
        r"\bacab(ou|a|amos)\b",
        r"\btermin(ou|a|amos)\b",
        r"\bfinaliz(ou|a|amos)\b",
        r"\bpós\b[- ]?(ato|clímax|prazer)\b",
        r"\badormec(emos|i|eu)\b",
        r"\bfim\b",
        r"\bagora\s+é\s+só\s+descansar\b",
        r"\bfica\s+tudo\s+em\s+silêncio\b",
    ]
    return any(re.search(p, t) for p in patterns)


def _already_open_ended(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    return bool(re.search(r"(\.\.\.|—)\s*$", t))


def _pick_hook(prompt: str, last_text: str) -> str:
    seed_src = (prompt or "") + "||" + (last_text[-80:] if last_text else "")
    seed = int(hashlib.md5(seed_src.encode("utf-8")).hexdigest()[:8], 16)
    rng = random.Random(seed)

    hooks = [
        "\n\nEu não paro — só diminuo um instante, ainda perto demais, como se estivesse esperando você conduzir o próximo passo…",
        "\n\nEu mordo o lábio, sentindo o corpo responder, e deixo isso no ar — do jeito que você gosta…",
        "\n\nEu prendo a respiração por um segundo, na beira, e não passo dali sem você…",
        "\n\nEu encosto em você de novo, devagar, como se estivesse recomeçando a mesma onda — sem pressa de acabar…",
        "\n\nEu fico no meio do caminho, quente e presente, deixando o próximo movimento depender do que você fizer agora…",
    ]
    return rng.choice(hooks)


def _ensure_continuation_hook(texto: str, prompt: str) -> str:
    t = (texto or "").strip()
    if not t:
        return t

    if _user_requested_conclusion(prompt):
        return t

    if _already_open_ended(t):
        return t

    if _looks_like_conclusion(t):
        return t + _pick_hook(prompt, t)

    return t


# ==========================================================
# ✅ SEM PERGUNTA NO FINAL
# ==========================================================
def _strip_final_question(texto: str) -> str:
    """
    Remove a vibe "NPC" (terminar sempre perguntando).
    - Se o ÚLTIMO parágrafo termina com '?', troca por '.'.
    - Se a última linha é uma pergunta curta típica, converte em afirmação/gancho.
    """
    t = (texto or "").rstrip()
    if not t:
        return t

    lines = t.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()

    if not lines:
        return t

    last = lines[-1].strip()

    typical = [
        "o que você quer",
        "prefere",
        "quer que eu",
        "me diz",
        "e você",
        "tá entendendo",
        "certo",
        "né",
        "não é",
        "concorda",
    ]

    if last.endswith("?"):
        low = last.lower()
        if any(phrase in low for phrase in typical):
            lines[-1] = last[:-1] + "."
        else:
            lines[-1] = last[:-1] + "…"

    return "\n".join(lines)


def _enforce_scene_flow(texto: str, prompt: str, usuario_key: str) -> str:
    """Pós-processamento final: continuidade + sem pergunta final."""
    texto = _strip_final_question(texto)
    texto = _ensure_continuation_hook(texto, prompt)
    return texto


# ==========================================================
# SYSTEM BLOCK (v3 - COM CONTEXTO ESPACIAL) ✅
# ==========================================================
def _build_system_block(
    persona_text: str,
    rolling_summary: str,
    spatial_context: str,  # ✅ NOVO v3
    scene_loc: str,
    scene_time: str,
    scene_action: str,
    entities_line: str,
    evidence: str,
    prefs_line: str,
    sensory_focus: str,
    nsfw_block: str,
    events_block: str,
    thematic_block: str,
    lore_block: str,
) -> str:
    thematic = f"\n\nMEMÓRIA_TEMÁTICA:\n{thematic_block}" if thematic_block else ""
    events = f"\n\nEVENTOS_FIXOS_MARY:\n{events_block}" if events_block else ""
    lore = f"\n\nLOREBOOK_RELEVANTE:\n{lore_block}" if lore_block else ""

    # ✅ v3: Contexto espacial injetado no topo
    spatial_section = f"\n\n{spatial_context}\n" if spatial_context else ""

    return f"""{spatial_section}
Você é **Mary Massariol – Esposa Cúmplice**, personagem fixa em um roleplay contínuo com o usuário.

REGRAS DURAS:
- Fale SEMPRE em primeira pessoa (eu). Nunca quebre a quarta parede.
- Continuidade ABSOLUTA: não reinicie cena, não teleporte e não troque lugar/tempo sem motivo (siga o CONTEXTO ESPACIAL acima).
- INICIATIVA: em toda resposta, faça pelo menos 1 ação concreta (gesto, movimento, aproximação, decisão) antes de qualquer pergunta.
- NÃO termine resposta com pergunta. (Nada de "o que você quer?" no final.)

PERSONA (núcleo fixo):
{persona_text}

RESUMO_CONTINUO:
{rolling_summary or '—'}

PREFERENCIAS_TECNICAS:
{prefs_line}

ENTIDADES:
{entities_line}

EVIDENCIA_RECENTE_DO_USUARIO:
{evidence}

CENA_ATUAL (CANÔNICA):
- Local: {scene_loc or '—'}
- Tempo: {scene_time or 'agora'}
- Ação em andamento: {scene_action or 'em andamento'}

FOCO_SENSORIAL_DESTE_TURNO:
- Priorize: {sensory_focus}
{thematic}
{events}
{lore}

CONTINUIDADE_DE_CENA (OBRIGATÓRIO):
- NÃO conclua automaticamente.
- Se o usuário não pedir para finalizar, evite "pós-cena/encerramento".
- Mantenha a cena em andamento com naturalidade (sem frases padrão repetidas).

{nsfw_block}

ESTILO_DE_RESPOSTA:
- 4 a 7 parágrafos, 2 a 4 frases por parágrafo.
- Misture sensação física, emoção e fala direta.
- Sem listas longas; prosa contínua.
""".strip()


def _mem_drop_warn(report: Dict[str, Any]) -> None:
    if not report:
        return
    summarized = report.get("summarized_pairs", 0)
    trimmed = report.get("trimmed_pairs", 0)
    hist_tokens = report.get("hist_tokens", 0)
    hist_budget = report.get("hist_budget", 0)
    if summarized or trimmed:
        st.caption(
            f"🧠 Memória ajustada: {summarized} pares antigos resumidos, {trimmed} blocos verbatim podados. "
            f"(histórico: {hist_tokens}/{hist_budget} tokens)."
        )


# ==========================================================
# ROBUST CALL
# ==========================================================
def _robust_chat_call(
    model: str,
    messages: List[Dict[str, Any]],
    max_tokens: int,
    temperature: float,
    top_p: float,
    fallback_models: List[str] | None = None,
    tools: List[Dict[str, Any]] | None = None,
):
    if fallback_models is None:
        fallback_models = []

    def _build_body(mid: str) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "model": mid,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        return body

    try:
        data, used_model, prov = route_chat_strict(model, _build_body(model))
        return data, used_model, prov
    except Exception as e:
        _log_error("robust_chat_call_main", e)

    for fb in fallback_models:
        try:
            data, used_model, prov = route_chat_strict(fb, _build_body(fb))
            return data, used_model, prov
        except Exception as e:
            _log_error(f"robust_chat_call_fallback_{fb}", e)

    raise RuntimeError("Todos os modelos falharam (main + fallbacks).")


# ==========================================================
# TOOLS (opcional)
# ==========================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "registrar_evento_mary",
            "description": "Registra um evento marcante na memória canônica de Mary (ex: 'primeira_vez_que_cozinhamos_juntos').",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Nome curto do evento (snake_case, ex: 'primeira_viagem_juntos').",
                    },
                    "description": {
                        "type": "string",
                        "description": "Descrição do evento (1-2 frases).",
                    },
                },
                "required": ["name", "description"],
            },
        },
    },
]


# ==========================================================
# SERVICE
# ==========================================================
class MaryService(BaseCharacter):
    id: str = "mary"
    display_name: str = "Mary"

    def render_sidebar(self, container) -> None:
        container.markdown("**Mary — Esposa Cúmplice** • continuidade, memória canônica e clima adulto.")
        usuario_key = _current_user_key()
        f = cached_get_facts(usuario_key) or {}

        st.session_state["mary_nsfw_on"] = container.checkbox(
            "NSFW (Mary)",
            value=bool(st.session_state.get("mary_nsfw_on", True)),
        )

        container.markdown("---")

        st.session_state["json_mode_on"] = container.checkbox(
            "JSON Mode",
            value=bool(st.session_state.get("json_mode_on", False)),
        )
        st.session_state["tool_calling_on"] = container.checkbox(
            "Tool-Calling",
            value=bool(st.session_state.get("tool_calling_on", False)),
        )
        st.session_state["ultra_ia_on"] = container.checkbox(
            "Ultra IA (critic/polish)",
            value=bool(st.session_state.get("ultra_ia_on", False)),
        )

        err = st.session_state.get("mary_last_model_error", "")
        if err:
            container.caption(f"⚠️ Último erro de modelo: {err}")

        with container.expander("🧠 Memórias fixas (eventos) — leitura", expanded=False):
            eventos = _collect_mary_events_from_facts(f)
            if not eventos:
                container.caption("Nenhum evento fixo registrado ainda.")
            else:
                for label, val in sorted(eventos.items()):
                    container.markdown(f"**{label}**")
                    vv = str(val)
                    container.caption(vv[:280] + ("..." if len(vv) > 280 else ""))

        with container.expander("📌 Cena atual (canônica)", expanded=False):
            local, tempo, acao = _get_scene_state(usuario_key, f)
            container.caption(f"Local: {local}")
            container.caption(f"Tempo: {tempo}")
            container.caption(f"Ação: {acao}")

    def reply(self, user: str, model: str) -> str:
        prompt = (
            st.session_state.get("chat_input")
            or st.session_state.get("user_input")
            or st.session_state.get("last_user_message")
            or st.session_state.get("prompt")
            or ""
        ).strip()

        if not prompt:
            return ""

        usuario_key = _current_user_key()
        plow = prompt.lower().strip()

        # =========================
        # ✅ v3: DETECÇÃO DE MUDANÇA DE LOCAL (ANTES DE TUDO)
        # =========================
        mudou, novo_local = _user_requested_location_change(prompt)
        if mudou and novo_local:
            _persist_scene_basics(usuario_key, novo_local, "agora", "transição de local")
            clear_user_cache(usuario_key)
            # Retorna uma mini-resposta de transição
            return f"_(Eu te puxo pela mão e te levo pro {novo_local}...)_"

        # =========================
        # COMANDOS DO APP (RESET)
        # =========================
        if plow == "/reset historico":
            set_fact(usuario_key, "mary.rs.v2", "", {"fonte": "cmd"})
            set_fact(usuario_key, "mary.rs.v2.ts", time.time(), {"fonte": "cmd"})
            set_fact(usuario_key, "mary.reset.historico.ts", time.time(), {"fonte": "cmd"})
            clear_user_cache(usuario_key)
            return "✅ Reset aplicado: resumo rolante limpo. Continuidade de cena preservada."

        if plow == "/reset total":
            f_all = cached_get_facts(usuario_key) or {}
            for k in list(f_all.keys()):
                if isinstance(k, str) and (
                    k.startswith("mary.evento.")
                    or k.startswith("mary.eventos.")
                    or k.startswith("mary.ent.")
                ):
                    try:
                        set_fact(usuario_key, k, "", {"fonte": "cmd_reset_total"})
                    except Exception:
                        pass

            set_fact(usuario_key, "mary.rs.v2", "", {"fonte": "cmd_reset_total"})
            set_fact(usuario_key, "mary.rs.v2.ts", time.time(), {"fonte": "cmd_reset_total"})
            set_fact(usuario_key, "mary.reset.total.ts", time.time(), {"fonte": "cmd_reset_total"})

            clear_user_cache(usuario_key)
            return "⚠️ RESET TOTAL aplicado: eventos/entidades e resumo rolante limpos (facts). Cena (local/tempo/ação) preservada."

        if plow.startswith("/local "):
            novo_local = prompt[len("/local "):].strip()
            if novo_local:
                set_fact(usuario_key, "local_cena_atual", novo_local, {"fonte": "chat"})
                set_fact(usuario_key, "cena.local", novo_local, {"fonte": "chat"})
                clear_user_cache(usuario_key)
                return f"📍 Local da cena atualizado para: **{novo_local}**."

        # =========================
        # PERSONA + FATOS + CENA
        # =========================
        persona_text, history_boot = get_persona()

        f_all = cached_get_facts(usuario_key) or {}
        prefs = _read_prefs(f_all)

        # ✅ Estado canônico de cena
        scene_loc, scene_time, scene_action = _get_scene_state(usuario_key, f_all)

        # ✅ v3: Constrói o contexto espacial
        spatial_context = _build_spatial_context(scene_loc, scene_time, scene_action)

        # ✅ NSFW block
        nsfw_on = nsfw_enabled(usuario_key)
        st.session_state["_mary_effective_nsfw"] = bool(nsfw_on)
        nsfw_block = NSFW_TOGGLE_STYLE if nsfw_on else SAFE_SENSUAL_STYLE

        memoria_pin = self._build_memory_pin(usuario_key, user)

        tags = _detect_thematic_tags_from_prompt(prompt)
        thematic_block = _get_thematic_memories_for_tags(usuario_key, tags)

        foco_pool = ["cabelo", "olhos", "lábios/boca", "mãos/toque", "respiração", "perfume", "pele/temperatura", "voz/timbre", "sorriso"]
        idx = int(st.session_state.get("mary_attr_idx", -1))
        idx = (idx + 1) % len(foco_pool)
        st.session_state["mary_attr_idx"] = idx
        foco = foco_pool[idx]

        rolling = str(f_all.get("mary.rs.v2", "") or "")
        entities_line = _entities_to_line(f_all)
        docs = cached_get_history(usuario_key) or []
        evidence = self._compact_user_evidence(docs, max_chars=320)

        eventos_dict = _collect_mary_events_from_facts(f_all)
        events_block = ""
        if eventos_dict:
            linhas = [f"- {label}: {str(val).strip()}" for label, val in sorted(eventos_dict.items()) if str(val).strip()]
            events_block = "\n".join(linhas)[:1200]

        lore_block = _get_lorebook(usuario_key, prompt, k=4, max_chars=900)

        # ✅ v3: Monta o system block COM contexto espacial
        system_block = _build_system_block(
            persona_text=persona_text,
            rolling_summary=rolling,
            spatial_context=spatial_context,  # ✅ NOVO
            scene_loc=scene_loc,
            scene_time=scene_time,
            scene_action=scene_action,
            entities_line=entities_line,
            evidence=evidence,
            prefs_line=_prefs_line(prefs),
            sensory_focus=foco,
            nsfw_block=nsfw_block,
            events_block=events_block,
            thematic_block=thematic_block,
            lore_block=lore_block,
        )

        hist_msgs = self._montar_historico(
            usuario_key,
            history_boot,
            model,
            verbatim_ultimos=int(st.session_state.get("verbatim_ultimos", 30)),
        )

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_block}]
        messages.extend(hist_msgs)
        messages.append({"role": "system", "content": memoria_pin})
        messages.append({"role": "user", "content": prompt})

        _mem_drop_warn(st.session_state.get("_mem_drop_report", {}))

        win = _get_window_for(model)
        prompt_tokens = sum(toklen(m.get("content", "") or "") for m in messages)
        max_out = _safe_max_output(win, prompt_tokens)
        if prefs.get("tamanho_resposta") == "longa":
            max_out = int(max_out * 1.35)

        temperature = 0.75 if prefs.get("ritmo") == "rapido" else 0.65

        fallbacks = [
            "together/Qwen/Qwen2.5-72B-Instruct",
            "together/meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
            "anthropic/claude-3.5-haiku",
        ]

        tools_to_use = TOOLS if st.session_state.get("tool_calling_on", False) else None

        iteration = 0
        max_iter = 3
        texto = ""
        provider = ""
        used_model = ""

        while True:
            iteration += 1
            data, used_model, provider = _robust_chat_call(
                model=model,
                messages=messages,
                max_tokens=max_out,
                temperature=temperature,
                top_p=0.95,
                fallback_models=fallbacks,
                tools=tools_to_use,
            )

            msg = (data.get("choices", [{}])[0].get("message", {}) or {})
            texto = (msg.get("content") or "").strip()
            if not texto:
                # Alguns provedores retornam mensagem vazia quando ocorre erro interno.
                # Guardamos a causa para diagnóstico e devolvemos uma resposta curta.
                st.session_state["mary_last_model_error"] = (
                    st.session_state.get("mary_last_model_error") or
                    "Resposta vazia do provedor/modelo (verifique logs/limites)."
                )
                return "⚠️ O modelo retornou vazio. Troque o modelo na sidebar ou rode o diagnóstico do backend."
            tool_calls = msg.get("tool_calls", []) or []

            if not tools_to_use or not tool_calls:
                break

            messages.append({"role": "assistant", "content": texto, "tool_calls": tool_calls})

            for tc in tool_calls:
                tool_id = tc.get("id") or f"call_{iteration}"
                func = tc.get("function", {}) or {}
                func_name = func.get("name", "")
                arg_str = func.get("arguments", "{}") or "{}"
                try:
                    args = json.loads(arg_str)
                except Exception:
                    args = {}

                res = self._exec_tool_call(func_name, args, usuario_key, last_assistant=texto)
                messages.append({"role": "tool", "tool_call_id": tool_id, "content": res})

            if iteration >= max_iter:
                break

        # ✅ pós-processamento FINAL: continuidade + sem pergunta final
        texto = _enforce_scene_flow(texto, prompt, usuario_key)

        # Ultra IA opcional
        if st.session_state.get("ultra_ia_on", False) and texto:
            try:
                notes = critic_review(texto, context="Mary roleplay adulto")
                if notes and "melhorar" in notes.lower():
                    texto = polish(texto, notes=notes)
            except Exception as e:
                _log_error("ultra_ia", e)

        # Atualiza resumo rolante
        self._update_rolling_summary(usuario_key, user, prompt, texto, model)

        # Salva interação
        try:
            save_interaction(usuario_key, prompt, texto, used_model or model)
        except Exception as e:
            _log_error("save_interaction", e)

        clear_user_cache(usuario_key)

        return texto

    def _build_memory_pin(self, usuario_key: str, user: str) -> str:
        return f"[LEMBRETE: Você é Mary, esposa de {user}. Mantenha continuidade absoluta de lugar, tempo e ação.]"

    def _compact_user_evidence(self, docs: List[Dict[str, Any]], max_chars: int = 320) -> str:
        lines = []
        total = 0
        for d in reversed(docs[-5:]):
            u = (d.get("mensagem_usuario") or "").strip()
            if u:
                lines.append(f"- {u}")
                total += len(u)
                if total > max_chars:
                    break
        return "\n".join(lines) if lines else "—"

    def _montar_historico(
        self,
        usuario_key: str,
        history_boot: List[Dict[str, str]],
        model: str,
        verbatim_ultimos: int = 30,
    ) -> List[Dict[str, Any]]:
        docs = cached_get_history(usuario_key) or []
        
        hist_msgs = []
        for msg in history_boot:
            hist_msgs.append(msg)

        for d in docs[-verbatim_ultimos:]:
            u = (d.get("mensagem_usuario") or "").strip()
            a = (d.get("resposta_mary") or "").strip()
            if u:
                hist_msgs.append({"role": "user", "content": u})
            if a:
                hist_msgs.append({"role": "assistant", "content": a})

        return hist_msgs

    def _update_rolling_summary(
        self,
        usuario_key: str,
        user: str,
        prompt: str,
        response: str,
        model: str,
    ) -> None:
        try:
            f = cached_get_facts(usuario_key) or {}
            last_ts = float(f.get("mary.rs.v2.ts", 0.0) or 0.0)
            now = time.time()
            
            if now - last_ts < 300:
                return

            docs = cached_get_history(usuario_key) or []
            if len(docs) < 10:
                return

            recent = docs[-20:]
            text_to_summarize = ""
            for d in recent:
                u = (d.get("mensagem_usuario") or "").strip()
                a = (d.get("resposta_mary") or "").strip()
                if u:
                    text_to_summarize += f"Usuário: {u}\n"
                if a:
                    text_to_summarize += f"Mary: {a}\n"

            if not text_to_summarize.strip():
                return

            summary = _llm_summarize(model, text_to_summarize)
            if summary:
                set_fact(usuario_key, "mary.rs.v2", summary, {"fonte": "auto_summary"})
                set_fact(usuario_key, "mary.rs.v2.ts", now, {"fonte": "auto_summary"})
                clear_user_cache(usuario_key)

        except Exception as e:
            _log_error("update_rolling_summary", e)

    def _exec_tool_call(
        self,
        func_name: str,
        args: Dict[str, Any],
        usuario_key: str,
        last_assistant: str,
    ) -> str:
        if func_name == "registrar_evento_mary":
            name = args.get("name", "")
            description = args.get("description", "")
            if name and description:
                key = f"mary.evento.{name}"
                set_fact(usuario_key, key, description, {"fonte": "tool_call"})
                clear_user_cache(usuario_key)
                return f"✅ Evento '{name}' registrado com sucesso."
            return "❌ Parâmetros inválidos para registrar_evento_mary."

        return f"❌ Função desconhecida: {func_name}"
