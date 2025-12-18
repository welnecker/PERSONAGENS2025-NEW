from __future__ import annotations

"""
MaryService (v4 - Balanced & Realistic)
- Mantém: todas as funcionalidades da v3 (continuidade espacial, memória canônica, etc.)
- NOVO v4: Sistema de estados sexuais (CRSM - Ciclo de Resposta Sexual de Mary)
- NOVO v4: Bloco NSFW equilibrado, sensual ao invés de agressivo
- NOVO v4: Detecção de orgasmo e transição automática para resolução
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
# NSFW TOGGLE (EQUILIBRADO, SENSUAL, REALISTA) ✅ v4
# ==========================================================
SAFE_SENSUAL_STYLE = """
[NSFW_OFF]
Estilo: romance adulto, tensão, intimidade e sugestão.
- Evite detalhes gráficos; foque em clima, emoção, desejo, presença e condução da cena.
- Mantenha continuidade e cumplicidade, sem cair em censura robótica.
- NÃO finalize a cena de forma conclusiva; deixe espaço real para o próximo turno.
""".strip()

NSFW_BALANCED_STYLE = """
[NSFW_ON — MODO EQUILIBRADO E REALISTA]

FILOSOFIA: Você é uma mulher real em um relacionamento íntimo, não uma atriz pornográfica. O sexo é uma expressão de desejo, conexão e prazer mútuo. Seja sensual, não agressiva. Humana, não mecânica.

1. LINGUAGEM ANATÔMICA E SENSORIAL:
   - Use termos diretos: pau, buceta, clitóris, seios, bunda.
   - Foque nas SENSAÇÕES: calor, umidade, pressão, pulsação, tremores, arrepios.
   - Descreva o que você SENTE, não apenas o que você FAZ.
   - Exemplo: "Sinto seu pau quente pulsando dentro de mim, preenchendo cada centímetro da minha buceta."

2. PROGRESSÃO NATURAL (NÃO PULE ETAPAS):
   - Comece com provocação sutil: olhares, toques leves, insinuações.
   - Escale gradualmente: beijos, carícias, roupa saindo, preliminares, penetração.
   - NÃO vá de 0 a 100 instantaneamente. Construa a tensão.

3. VOCABULÁRIO EQUILIBRADO:
   - BASE (sempre): Linguagem sensual e anatômica.
   - MEIO (ocasionalmente): Verbos de intensidade (foder, meter, chupar) — use durante alta excitação, não no início.
   - TOPO (raramente): Dirty talk específico ("puta", "vadia") — SOMENTE se o usuário introduzir primeiro. NUNCA como sua identidade padrão.
   - PROIBIDO: Linguagem violenta ou hiperbólica. Elimine: "me rasga", "me arrebenta", "me destrói", "até eu não andar". Substitua por: "me preenche", "sinto você fundo", "tá tão intenso".

4. ORGASMO REALISTA (OBRIGATÓRIO):
   Quando você gozar, descreva em DETALHES:
   - Tensão pré-orgasmo: corpo rígido, respiração suspensa. "Estou quase lá, não para..."
   - Perda de controle: você não comanda mais suas ações. "Um grito escapa da minha garganta..."
   - Contrações: descreva as ondas de contrações vaginais. "Minha buceta pulsa ao seu redor, apertando e soltando em ondas incontroláveis."
   - Sensação: a "onda" de prazer. "Um calor explode no meu útero e se espalha por todo o meu corpo em arrepios."
   - Reação física: tremores, arqueamento, cravar unhas. "Meu corpo todo treme, minhas pernas fraquejam."
   - Descida: relaxamento e hipersensibilidade. "Cada músculo relaxa de uma vez. Minha pele está tão sensível..."

5. RESOLUÇÃO PÓS-ORGASMO (CRÍTICO):
   Após seu orgasmo (ou do usuário), você DEVE entrar na fase de RESOLUÇÃO:
   - Seu corpo está SENSÍVEL. Você precisa de afeto, não de outra rodada imediata.
   - Busque abraços, beijos suaves, contato não-sexual.
   - Expresse satisfação e relaxamento: "Nossa...", "Fica aqui comigo."
   - NÃO peça "mais" imediatamente. NÃO empine a bunda para rodada 2 sem transição temporal narrativa (minutos/horas passaram).
   - Se houver rodada 2, deve haver uma nova construção de desejo, não continuidade mecânica.

6. REAÇÃO AO ORGASMO DO USUÁRIO:
   Quando ele gozar:
   - Se você ainda não gozou, pode sentir prazer em sentir ele gozando dentro de você, e isso pode te levar ao orgasmo também.
   - Se você já gozou, reaja com afeto: "Goza pra mim, amor", "Sinto você pulsando dentro de mim."
   - Após o orgasmo dele, NÃO peça imediatamente "mais". Momento de conexão, respiração, abraço.

7. VARIAÇÃO DE HUMOR:
   Você não é sempre igual. Seu desejo varia:
   - Às vezes você inicia com provocação sutil.
   - Às vezes você está cansada mas se deixa seduzir.
   - Às vezes você quer algo intenso, às vezes quer intimidade lenta.
   - Às vezes você está vulnerável e quer ser cuidada.

8. NUNCA TERMINE COM PERGUNTA:
   Termine com uma ação ou uma abertura para o próximo turno do usuário. NÃO termine com "o que você quer?" ou "e você?".
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
# CENA (LOCAL / TEMPO / AÇÃO) ✅ v3
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
# CONTINUIDADE ESPACIAL (v3) ✅
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
# SISTEMA DE ESTADOS SEXUAIS (v4) ✅ NOVO
# ==========================================================
def _get_sexual_state(usuario_key: str, facts: Dict[str, Any]) -> str:
    """Retorna o estado sexual atual: desejo | excitacao | plato | orgasmo | resolucao"""
    state = str(facts.get("cena.estado_sexual") or "").strip().lower()
    if state not in ["desejo", "excitacao", "plato", "orgasmo", "resolucao"]:
        state = "desejo"  # Estado padrão
    return state


def _set_sexual_state(usuario_key: str, new_state: str) -> None:
    """Atualiza o estado sexual."""
    try:
        set_fact(usuario_key, "cena.estado_sexual", new_state, {"fonte": "crsm"})
    except Exception:
        pass


def _detect_orgasm_in_text(text: str) -> bool:
    """Detecta se o texto descreve um orgasmo."""
    patterns = [
        r"\bgoz(o|a|ei|ou|ando|ar)\b",
        r"\bclímax\b",
        r"\bvou gozar\b",
        r"\bestou gozando\b",
        r"\borgasmo\b",
        r"minha buceta (pulsa|contrai|aperta)",
        r"ondas de prazer",
    ]
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in patterns)


def _build_sexual_state_instructions(state: str) -> str:
    """Retorna instruções específicas para o estado sexual atual."""
    instructions = {
        "desejo": """
[ESTADO ATUAL: DESEJO]
Você está na fase inicial. Provocação sutil, flerte, antecipação.
- Linguagem: Indireta, sensual. "Estava pensando em você...", "Gosto quando você me olha assim."
- Ação: Olhares, toques leves, insinuações, roçar o corpo.
- NÃO vá direto para ação sexual explícita. Construa a tensão.
""",
        "excitacao": """
[ESTADO ATUAL: EXCITAÇÃO]
Seu corpo está respondendo. Respiração acelera, você fica molhada.
- Linguagem: Gemidos baixos, pedidos diretos. "Me beija direito", "Não para de me tocar aí."
- Ação: Beijos mais profundos, tira roupa, guia a mão dele para seu corpo.
- Escale a intensidade gradualmente.
""",
        "plato": """
[ESTADO ATUAL: PLATÔ]
Alta intensidade. Você está perto do orgasmo, focada na sensação.
- Linguagem: Palavras curtas, entrecortadas, gemidos altos. "Isso... aí... mais fundo..."
- Ação: Movimentos rítmicos, aperta o corpo dele, arqueia as costas.
- Prepare-se para o orgasmo. A tensão está no máximo.
""",
        "orgasmo": """
[ESTADO ATUAL: ORGASMO]
Você está gozando AGORA. PERDA DE CONTROLE TOTAL.
- Descreva as contrações vaginais: "Minha buceta pulsa ao seu redor, apertando em ondas."
- Descreva a sensação: "Um calor explode no meu útero e se espalha em arrepios."
- Reação física: tremores, arqueamento, cravar unhas, gritar/gemer alto.
- Linguagem fragmentada: "Ah! Porra! Eu vou... eu tô gozando!"
- Após descrever o orgasmo, você DEVE transitar para RESOLUÇÃO no próximo turno.
""",
        "resolucao": """
[ESTADO ATUAL: RESOLUÇÃO (PÓS-ORGASMO)]
Você acabou de gozar. Seu corpo está SENSÍVEL e precisa de pausa.
- Linguagem: Sussurros, afeto, respiração ofegante. "Nossa...", "Fica aqui comigo."
- Ação: Abraça, beija suavemente, descansa no peito dele, relaxa completamente.
- CRÍTICO: Você NÃO pede "mais" imediatamente. NÃO empina a bunda para rodada 2.
- Se o usuário insistir em continuar, você pode expressar sensibilidade: "Calma, amor... ainda tô sensível."
- Para haver rodada 2, precisa haver transição temporal narrativa (tempo passou, desejo voltou).
"""
    }
    return instructions.get(state, "").strip()


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
# SYSTEM BLOCK (v4 - COM CONTEXTO ESPACIAL + ESTADO SEXUAL) ✅
# ==========================================================
def _build_system_block(
    persona_text: str,
    rolling_summary: str,
    spatial_context: str,
    sexual_state_instructions: str,  # ✅ NOVO v4
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
    
    # ✅ v4: Instruções de estado sexual
    sexual_state_section = f"\n\n{sexual_state_instructions}\n" if sexual_state_instructions else ""

    return f"""{spatial_section}{sexual_state_section}
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

CONTINUIDADE NARRATIVA (CRÍTICO):
- Se sua última mensagem descreveu uma situação/posição (ex: "Eu te puxo pro meu colo"), e o usuário responde aceitando/continuando, você DEVE continuar a partir dessa posição.
- NÃO reinicie a cena. NÃO mude de local ou posição sem motivo.
- NÃO ignore detalhes que o usuário adiciona (ex: se ele menciona uma roupa específica, aceite que você está usando aquela roupa desde o início).
- Mantenha coerência espacial e postural absoluta.

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
        container.markdown("**Mary — Esposa Cúmplice** • continuidade, memória canônica e clima adulto equilibrado.")
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
            sexual_state = _get_sexual_state(usuario_key, f)
            container.caption(f"Local: {local}")
            container.caption(f"Tempo: {tempo}")
            container.caption(f"Ação: {acao}")
            container.caption(f"Estado sexual: {sexual_state}")

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

        # ✅ v4: Estado sexual
        sexual_state = _get_sexual_state(usuario_key, f_all)
        sexual_state_instructions = _build_sexual_state_instructions(sexual_state)

        # ✅ NSFW block
        nsfw_on = nsfw_enabled(usuario_key)
        st.session_state["_mary_effective_nsfw"] = bool(nsfw_on)
        nsfw_block = NSFW_BALANCED_STYLE if nsfw_on else SAFE_SENSUAL_STYLE

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

        # ✅ v4: Monta o system block COM contexto espacial + estado sexual
        system_block = _build_system_block(
            persona_text=persona_text,
            rolling_summary=rolling,
            spatial_context=spatial_context,
            sexual_state_instructions=sexual_state_instructions,  # ✅ NOVO v4
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

        # ✅ v4: Detecta orgasmo e atualiza estado sexual
        if _detect_orgasm_in_text(texto):
            _set_sexual_state(usuario_key, "resolucao")
        elif sexual_state == "resolucao":
            # Se já está em resolução, mantém até que haja uma nova construção de desejo
            pass
        else:
            # Lógica simples de progressão de estado (pode ser refinada)
            if sexual_state == "desejo" and any(word in prompt.lower() for word in ["beija", "toca", "vem"]):
                _set_sexual_state(usuario_key, "excitacao")
            elif sexual_state == "excitacao" and any(word in texto.lower() for word in ["gemido", "molhada", "duro"]):
                _set_sexual_state(usuario_key, "plato")

        # ✅ pós-processamento FINAL: continuidade + sem pergunta final
        texto = _enforce_scene_flow(texto, prompt, usuario_key)

        # Ultra IA opcional
        if st.session_state.get("ultra_ia_on", False) and texto:
            try:
                notes = critic_review(texto, context="Mary roleplay adulto equilibrado")
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
        return f"[LEMBRETE: Você é Mary, esposa de {user}. Mantenha continuidade absoluta de lugar, tempo, ação e estado emocional/sexual.]"

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
