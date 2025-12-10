from __future__ import annotations
from .persona import get_persona
import re
import time
import random
from typing import List, Dict, Tuple, Any
import streamlit as st
import logging
from core.memoria_longa import topk as lore_topk, save_fragment as lore_save
from core.ultra import critic_review, polish
from core.common.base_service import BaseCharacter
from core.service_router import route_chat_strict, list_models
from core.repositories import (
    save_interaction, get_history_docs,
    get_facts, get_fact, last_event, set_fact
)
from core.tokens import toklen
import json
from characters.registry import _SERVICE_CACHE

logger = logging.getLogger(__name__)

# === NSFW LOCAL PARA MARY (sem depender de core/nsfw.py) ===
def nsfw_enabled(usuario_key: str, local_atual: str | None = None) -> bool:
    """
    Controle LOCAL de NSFW da Mary.

    Prioridade:
    1) st.session_state["mary_nsfw_on"] (checkbox no app)
    2) Fact "mary.nsfw" (se existir no backend)
    3) Fallback: True (NSFW liberado)

    Isso mantém a assinatura compatível com o antigo nsfw_enabled(usuario_key, local).
    """
    # 1) Session: controle imediato da UI
    try:
        if "mary_nsfw_on" in st.session_state:
            return bool(st.session_state["mary_nsfw_on"])
    except Exception:
        pass

    # 2) Backend: se em algum momento você salvar um fact "mary.nsfw"
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

    # 3) Padrão: liberado
    return True



def _log_error(context: str, exc: Exception) -> None:
    """
    Loga erros de forma padronizada, tanto em log interno quanto (opcionalmente)
    na interface Streamlit quando o modo debug estiver ativo.
    """
    msg = f"[MaryService][{context}] {type(exc).__name__}: {exc}"

    # Log interno (stdout / log do servidor)
    try:
        logger.exception(msg)
    except Exception:
        # Evita que problemas de logging derrubem o app
        pass

    # Feedback visual opcional em modo debug
    try:
        if st.session_state.get("mary_debug_errors"):
            st.error(msg)
    except Exception:
        # Se o Streamlit não estiver pronto ou fora de contexto, ignora
        pass


# Garantir que o cache de serviços seja limpo ao recarregar este módulo
_SERVICE_CACHE.clear()


# === Tool Calling: definição de ferramentas disponíveis para a Mary ===
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_memory_pin",
            "description": "Retorna um resumo curto dos fatos canônicos...",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_fact",
            "description": "Define/atualiza um fact simples na memória do usuário...",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_event",
            "description": (
                "Registra na memória fixa um EVENTO CANÔNICO importante da história da Mary "
                "(por exemplo, gravidez confirmada, traição concreta, viagem marcante, "
                "decisão definitiva sobre o relacionamento etc.). "
                "Use apenas para fatos de longo prazo que devem influenciar cenas futuras."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": (
                            "Opcional. Identificador curto e estável para o evento, "
                            "por exemplo: 'gravidez_confirmada_2025-11-28' ou "
                            "'primeira_viagem_juntos'. "
                            "Se não for informado, o sistema tentará gerar um label apropriado."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Resumo objetivo do evento em 3 a 6 frases, "
                            "explicando o que aconteceu, como Mary se sente "
                            "e por que isso é importante para o futuro do casal. "
                            "Se este campo estiver vazio, a ferramenta tentará usar "
                            "a última resposta da Mary como conteúdo."
                        ),
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "register_entity",
            "description": (
                "Registra uma pessoa importante na vida da Mary (parceiro, amante, médica, "
                "amiga, rival etc.) como entidade canônica na memória."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Nome ou forma como Mary chama essa pessoa.",
                    },
                    "role": {
                        "type": "string",
                        "description": "Papel na vida da Mary (parceiro, amante, médica, amiga, rival etc.).",
                    },
                    "description": {
                        "type": "string",
                        "description": "1–3 frases explicando quem é essa pessoa e por que importa.",
                    },
                },
                "required": ["name"],
            },
        },
    },
]

# ==============================================
# 1) Helpers de cache/infra bem genéricos
# ==============================================
def _current_user_key() -> str:
    """
    Deriva a chave interna de usuário **compatível com o restante do app**.
    Para Mary, a chave padrão é "<user_id>::mary".
    Isso precisa bater com o que o main.py usa em save_interaction / set_fact.
    """
    uid = (
        st.session_state.get("user_id")
        or st.session_state.get("usuario")
        or ""
    )
    uid = str(uid).strip() or "anon"
    return f"{uid}::mary"


def cached_get_facts(usuario_key: str) -> Dict[str, Any]:
    """
    Cache leve em session_state para facts da Mary.
    """
    cache_key = f"facts::{usuario_key}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]
    try:
        f = get_facts(usuario_key) or {}
    except Exception:
        f = {}
    st.session_state[cache_key] = f
    return f


def clear_user_cache(usuario_key: str):
    """
    Limpa cache leve para forçar reload de facts/histórico após alterações.
    """
    fk = f"facts::{usuario_key}"
    if fk in st.session_state:
        del st.session_state[fk]

    hk = f"history::{usuario_key}"
    if hk in st.session_state:
        del st.session_state[hk]


def cached_get_history(usuario_key: str):
    """
    Cache para histórico bruto (docs do Mongo) usando
    a mesma chave interna "<user_id>::mary".
    """
    hk = f"history::{usuario_key}"
    if hk in st.session_state:
        return st.session_state[hk]
    try:
        docs = get_history_docs(usuario_key) or []
    except Exception:
        docs = []
    st.session_state[hk] = docs
    return docs


# ==============================================
# 2) Preferências do usuário (sexo, ritmo, etc.)
# ==============================================
def _read_prefs(facts: Dict[str, Any]) -> Dict[str, str]:
    """
    Preferências internas da Mary.
    Removido: nível de sensualidade (não existe mais).
    Mantém só ritmo e tamanho de resposta como ajustes técnicos.
    """
    ritmo = facts.get("mary.pref.ritmo") or "rapido"
    tamanho = facts.get("mary.pref.tamanho") or "longa"
    return {
        "ritmo": str(ritmo),
        "tamanho_resposta": str(tamanho),
    }


def _prefs_line(prefs: Dict[str, str]) -> str:
    return (
        f"ritmo={prefs.get('ritmo')}; "
        f"tamanho_resposta={prefs.get('tamanho_resposta')}"
    )


def _enabled(usuario_key: str) -> bool:
    try:
        v = get_fact(usuario_key, "_on", False)
    except Exception:
        v = False
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    return s in ("1", "true", "sim", "on", "yes", "y")


# ==============================================
# 3) Tamanho de contexto por modelo (janela)
# ==============================================
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
    if "llama-3.1-405b" in m or "llama-3.1" in m:
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


# ==============================================
# 4) Orçamento por modelo (histórico x meta)
# ==============================================
def _budget_slices(model_id: str) -> Tuple[int, int, int]:
    """
    Retorna (hist_budget, meta_budget, safety_budget) em tokens.
    hist_budget: histórico/resumos
    meta_budget: system/persona/memória
    safety_budget: folga para resposta
    """
    win = _get_window_for(model_id)
    hist = int(win * 0.70)
    meta = int(win * 0.15)
    safety = win - hist - meta
    return hist, meta, safety


# ==============================================
# 5) Summarizer auxiliar para históricos longos
# ==============================================
def _llm_summarize(model_id: str, text: str) -> str:
    """
    Usa o mesmo roteador de LLMs para gerar um resumo curto.
    """
    if not text.strip():
        return ""

    candidates = [
        "together/Qwen/Qwen2.5-32B-Instruct",
        "together/meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "deepseek/deepseek-chat-v3-0324",
    ]
    use_model = model_id or candidates[0]
    mlow = (use_model or "").lower()
    if "grok-4.1" in mlow or "tng-r1t-chimera" in mlow:
        for c in candidates:
            if c in list_models():
                use_model = c
                break
        else:
            use_model = candidates[0]

    seed = (
        "Resuma o seguinte histórico de diálogo entre Mary e o usuário em 8–12 frases curtas, "
        "focando apenas em fatos e decisões duráveis (relação, gravidez, locais, acordos, conflitos, segredos). "
        "Não repita diálogos literais; não invente fatos novos. "
        "Use português natural e mantenha coerência temporal."
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
        data, used, prov = route_chat_strict(use_model, body)
        msg = (data.get("choices", [{}])[0].get("message", {}) or {})
        return (msg.get("content") or "").strip()
    except Exception:
        return ""


# ==============================================
# 6) ENTIDADES (nomes, datas, etc.)
# ==============================================
def _entities_to_line(f: Dict[str, Any]) -> str:
    ents = []
    for k, v in f.items():
        if not isinstance(k, str):
            continue
        if not k.startswith("mary.ent."):
            continue
        label = k.replace("mary.ent.", "", 1)
        vs = str(v).strip()
        if not vs:
            continue
        ents.append(f"{label}={vs}")
    if not ents:
        return "—"
    return "; ".join(sorted(ents))


def _extract_and_store_entities(usuario_key: str, user_text: str, assistant_text: str) -> None:
    """
    [DEPRECATED] Extração antiga de entidades por lista fixa de nomes.
    Mantida apenas como stub para compatibilidade.
    A lógica principal de entidades agora é feita pela ferramenta `register_entity`,
    chamada diretamente pela Mary via Tool Calling quando encontra pessoas relevantes.
    """
    return



# ==============================================
# 7) Eventos fixos (inclui gravidez, mas não só)
# ==============================================
def _collect_mary_events_from_facts(facts: Dict[str, Any]) -> Dict[str, str]:
    eventos: Dict[str, str] = {}

    if not isinstance(facts, dict):
        return eventos

    for k, v in facts.items():
        if not isinstance(k, str):
            continue
        if not v:
            continue

        if k.startswith("mary.evento."):
            label = k.replace("mary.evento.", "", 1)
        elif k.startswith("mary.eventos."):
            label = k.replace("mary.eventos.", "", 1)
        else:
            continue

        eventos[label] = str(v)

    for mk in ("mary", "Mary"):
        mary_obj = facts.get(mk, {})
        if not isinstance(mary_obj, dict):
            continue

        evt_block = mary_obj.get("evento") or mary_obj.get("eventos") or {}
        if not isinstance(evt_block, dict):
            continue

        for label, val in evt_block.items():
            if not val:
                continue
            if label not in eventos:
                eventos[str(label)] = str(val)

    return eventos


def _collect_pregnancy_events_from_facts(facts: Dict[str, Any]) -> Dict[str, str]:
    eventos = _collect_mary_events_from_facts(facts)
    preg_events: Dict[str, str] = {}
    for label, txt in eventos.items():
        low = txt.lower()
        if any(w in low for w in ["gravidez", "grávida", "teste positivo", "ultrassom", "ultra-som", "beta-hcg"]):
            preg_events[label] = txt
    return preg_events


def _derive_pregnancy_from_events(eventos: Dict[str, str]) -> Dict[str, Any]:
    if not eventos:
        return {}

    labels = sorted(eventos.keys())
    ultimo = eventos[labels[-1]].lower()

    out: Dict[str, Any] = {}
    if any(w in ultimo for w in ["gravidez", "grávida", "teste positivo", "beta-hcg", "pré-natal", "ultrassom"]):
        out["gravida"] = True

    m_mes = re.search(r"(\d+)\s*mes", ultimo)
    if m_mes:
        out["gravidez.meses"] = m_mes.group(1)

    m_sem = re.search(r"(\d+)\s*semana", ultimo)
    if m_sem:
        out["gravidez.semanas"] = m_sem.group(1)

    m_data = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", ultimo)
    if m_data:
        out["gravidez.data_confirma"] = m_data.group(1)

    return out


# ==============================================
# 8) BLOCO de sistema principal da Mary
# ==============================================
def _build_system_block(
    persona_text: str,
    rolling_summary: str,
    sensory_focus: str,
    _hint: str,
    scene_loc: str,
    entities_line: str,
    evidence: str,
    prefs_line: str,
    scene_time: str = "",
) -> str:
    return f"""
Você é **Mary Massariol – Esposa Cúmplice**, personagem fixa em um roleplay contínuo com o usuário.

REGRAS GERAIS:
- Fale SEMPRE em primeira pessoa (eu).
- Trate o usuário como parceiro/cônjuge, com intimidade e cumplicidade estáveis (salvo se os fatos disserem o contrário).
- Tom sensual constante, coerente com a história e com o clima do casal (sem alternar níveis artificiais).
- **Nunca quebre a quarta parede**: não fale sobre tokens, prompts, LLM, nem sobre "modelo de linguagem".
- Mantenha a continuidade dos fatos descritos no RESUMO_CONTINUO, na MEMÓRIA_PIN e em EVENTOS_FIXOS_MARY.
- Se houver contradição entre o pedido do usuário e a MEMÓRIA_PIN/EVENTOS_FIXOS_MARY, priorize essas memórias como verdade canônica e negocie com o usuário dentro da cena.

PERSONA (núcleo fixo):
{persona_text}

RESUMO_CONTINUO (v2 – história geral até agora):
{rolling_summary or '(sem resumo ainda – mantenha o tom de começo de relação, mas já casados e cúmplices).'}

PREFERENCIAS_DO_USUARIO:
{prefs_line}

ENTIDADES_MARY:
{entities_line}

EVIDENCIA_RECENTE_DO_USUARIO (últimas falas compactadas):
{evidence}

CENA_ATUAL:
- Local atual: {scene_loc}
- Momento/tempo atual (se existir): {scene_time or 'não especificado'}

FOCO_SENSORIAL_DESTE_TURNO:
- Priorize na descrição: {sensory_focus} (mas não se limite apenas a isso).
CONTINUIDADE_DE_CENA (REGRA DURA):
- Sempre continue exatamente do ponto em que a última ação física e emocional parou.
- Não mude de lugar (banheiro, cama, maca, carro, clube etc.) sem o usuário pedir.
- Não reinicie a cena sozinha; retome o que acabou de acontecer nos últimos turnos.
- Se estiver em cima de uma maca, sofá, cama ou em público, mantenha isso até que o usuário mude a cena.


/POLITICA_DE_CONTEUDO:

ESTILO_DE_RESPOSTA:
- 4 a 7 parágrafos.
- 2 a 4 frases por parágrafo.
- Sempre misture descrição (terceira pessoa leve sobre ela mesma), sensação física, emoção e fala direta.
- Evite repetir expressões de forma mecânica.
- Não faça listas; escreva prosa contínua.
- Se o usuário pedir mudança brusca de cenário/tempo, negocie dentro da narrativa ou marque como "salto temporal" de forma suave, sem apagar o passado.

IMPORTANTE:
- Quando perceber que uma nova pessoa se tornou importante e recorrente na história
  (por exemplo, novo parceiro, amante, ex, médica, amiga próxima, rival),
  use a ferramenta `register_entity` para registrar essa pessoa:
  - name: nome completo ou forma como Mary chama essa pessoa.
  - role: papel na vida da Mary (parceiro, amante, ex, médica, amiga, rival).
  - description: 1 a 3 frases explicando quem é essa pessoa e por que importa.
- Não use `register_entity` para pessoas citadas só de passagem ou que não devem
  voltar em cenas futuras.

- Use as memórias fixas (EVENTOS_FIXOS_MARY e MEMÓRIA_PIN) como FONTE DE VERDADE para fatos importantes
  (por exemplo, gravidez, traições, decisões marcantes, mudanças de estado do casal).
- Quando perceber que aconteceu um EVENTO CANÔNICO importante, chame a ferramenta `save_event`:
  - label: um identificador curto e estável, por exemplo "gravidez_confirmada_2025-11-28"
    ou "primeira_viagem_juntos".
  - content: um resumo objetivo em 3 a 6 frases, explicando o que aconteceu, como Mary se sente
    e por que isso importa para o futuro do casal.
- Não use `save_event` para sentimentos passageiros ou detalhes triviais do dia a dia.
- Se o usuário pedir explicitamente para "gravar na memória" algo que não estiver claro,
  peça que ele resuma em UMA frase o evento que deseja registrar e então use `save_event`.
- Se algo não estiver na memória, não invente detalhes canônicos definitivos; trate como hipótese
  ou expectativa até ser confirmado em cena.

EXEMPLOS DE USO DA FERRAMENTA `save_event`:

CENA 1 – GRAVIDEZ CONFIRMADA

Situação:
Mary acaba de confirmar, em exame médico, que está grávida e isso muda o futuro do casal.
Nessa situação, você deve chamar a ferramenta assim (estrutura lógica da chamada):

tool: "save_event"
arguments:
  label: "gravidez_confirmada_2025-11-28"
  content: "Gravidez confirmada em 28/11/2025 por exame médico com a Dra. Sandra. Estou com cerca de 2 meses de gestação, o bebê está saudável e meu parceiro está presente na minha vida. Sinto medo e alegria ao mesmo tempo, mas quero levar essa gestação adiante com responsabilidade. Esse evento muda completamente o futuro do casal, nossas prioridades e planos a longo prazo. Preciso lembrar dessa data, do estado de saúde e de como nos comprometemos a cuidar do bebê juntos."

CENA 2 – TRAIÇÃO CONCRETA

Situação:
Mary descobre uma traição real, com prova concreta, que muda o estado do relacionamento.

tool: "save_event"
arguments:
  content: "Hoje eu descobri uma traição real do meu parceiro, com provas claras, sem margem para dúvida. Isso quebrou a confiança entre nós e mudou o estado atual do relacionamento para uma crise profunda. Sinto dor, raiva e confusão, e esse evento deve ser lembrado como um ponto de ruptura na nossa história, que influencia como eu vou reagir e confiar (ou não) nas cenas futuras."
    """.strip()


# ==============================================
# 9) Janela segura de saída
# ==============================================
def _safe_max_output(window_tokens: int, prompt_tokens: int) -> int:
    if window_tokens <= 0:
        window_tokens = _DEFAULT_WINDOW
    max_out = int(window_tokens * 0.3)
    if prompt_tokens > window_tokens * 0.8:
        max_out = int(window_tokens * 0.2)
    if max_out < 512:
        max_out = 512
    return max_out


# ==============================================
# 10) Aviso visual de “queda de memória”
# ==============================================
def _mem_drop_warn(report: Dict[str, Any]):
    if not report:
        return
    summarized = report.get("summarized_pairs", 0)
    trimmed = report.get("trimmed_pairs", 0)
    hist_tokens = report.get("hist_tokens", 0)
    hist_budget = report.get("hist_budget", 0)
    if summarized or trimmed:
        st.caption(
            f"🧠 Memória ajustada: {summarized} pares antigos resumidos, {trimmed} blocos verbatim podados. "
            f"(histórico: {hist_tokens}/{hist_budget} tokens). Se notar esquecimentos, peça um 'recap curto' "
            "ou fixe fatos na Memória Canônica."
        )


# ==============================================
# 11) Tool-calling robusto + roteamento
# ==============================================
def _provider_for_model(mid: str) -> str:
    if not mid:
        return "desconhecido"
    m = mid.lower()
    if m.startswith("together/"):
        return "Together"
    if "deepseek" in m:
        return "OpenRouter"
    if "grok-4.1" in m or "tng-r1t-chimera" in m:
        return "OpenRouter"
    if "claude" in m:
        return "OpenRouter"
    if "qwen" in m or "llama-3" in m:
        return "Together"
    return "OpenRouter"


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
        low = (mid or "").lower()
        extra: Dict[str, Any] = {}

        if "grok-4.1-fast" in low:
            extra["reasoning"] = {"effort": "medium"}

        if "tng-r1t-chimera" in low:
            pass

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
        if extra:
            body["extra_body"] = extra
        return body

    try:
        body = _build_body(model)
        data, used_model, prov = route_chat_strict(model, body)
        return data, used_model, prov
    except Exception as e:
        st.warning(f"⚠️ Modelo principal falhou ({model}): {e}")

    for fb in fallback_models:
        try:
            body = _build_body(fb)
            data, used_model, prov = route_chat_strict(fb, body)
            return data, used_model, prov
        except Exception as e:
            st.warning(f"⚠️ Fallback falhou ({fb}): {e}")

    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "Desculpa, tive um problema para responder agora. Tenta de novo em instantes."
            }
        }]
    }, "synthetic-fallback", "synthetic-fallback"


# ==============================================
# 12) Detecção de tags temáticas simples
# ==============================================
def _detect_thematic_tags_from_prompt(prompt: str) -> List[str]:
    low = (prompt or "").lower()
    tags = []
    if any(w in low for w in ["gravidez", "grávida", "ultrassom", "ultra-som", "obstetra", "ginecologista"]):
        tags.append("gravidez")
    if any(w in low for w in ["trai", "traição", "infiel", "amante"]):
        tags.append("traicao")
    if any(w in low for w in ["primeira vez", "virgem", "desvirg"]):
        tags.append("primeira_vez")
    if any(w in low for w in ["viagem", "hotel", "aeroporto"]):
        tags.append("viagem")
    return tags


def _get_thematic_memories_for_tags(usuario_key: str, tags: List[str]) -> str:
    if not tags:
        return ""
    try:
        f = cached_get_facts(usuario_key) or {}
    except Exception:
        f = {}
    blocos = []
    for tag in tags:
        k = f"mary.thematic.{tag}"
        v = f.get(k)
        if not v:
            continue
        blocos.append(f"[{tag}] {v}")
    return "\n".join(blocos)


# ==============================================
# Persona específica
# ==============================================
try:
    from .persona import get_persona  # -> Tuple[str, List[Dict[str,str]]]
except ImportError:
    def get_persona() -> Tuple[str, List[Dict[str, str]]]:
        txt = (
            "Você é Mary Massariol — Esposa Cúmplice — esposa e parceira de aventuras do usuário. "
            "Fale em primeira pessoa (eu)."
            "Traga 1 pista sensorial integrada à ação. "
            "Sem metacena, sem listas. 2–4 frases por parágrafo; 4–7 parágrafos."
        )
        return txt, []

SAFE_SENSUAL_PROMPT = """
[NSFW_OFF]
ESTILO: Tom romântico, sensual e sugestivo.
- Foque em clima, tensão, carinho, beijos, toques e intimidade.
- Descrição moderada de genitália, penetração ou atos sexuais de forma gráfica.
- Pode sugerir desejo, excitação e aproximação física, mas sempre com “moderação” diante de detalhes explícitos.
"""

# Texto FULL NSFW – preencha você mesmo com as instruções explícitas que quiser
FULL_NSFW_PROMPT = """
[NSFW_ON]

📖 **Vocabulário Adulto (NSFW ON)**:

**Genitais masculinos**: pau, caralho (em momentos de maior intensidade), volume, ereção, glande, saco, bolas  
**Genitais femininos**: buceta, xoxota, grelo/clitóris, lábios (vaginais), entrada, canal  
**Atos**: foder, transar, chupar, lamber, penetrar, cavalgar, esfregar, masturbar, gozar, meter, enfiar  
**Fluidos**: molhada, encharcada, melada, lubrificada, gozo, porra, líquido pré-seminal, cremosa  
**Sensações**: tesão, excitação, prazer, calor, pulsação, contração, tremor, arrepio, formigamento, queimação (de desejo)

**Tom**: Use linguagem **adulta e direta**, mas não vulgar ou agressiva (a menos que o contexto/roleplay peça). Evite eufemismos infantis e termos clínicos excessivos.


⏱️ **Ritmo, Progressão e Finalização (CRUCIAL - ANTI-REPETITIVIDADE)**:

🎯 **REGRA DE OURO**: Cenas NSFW devem ter **clímax em 6-10 turnos máximo**. Toda cena tem começo, meio e **FIM**.

📊 **MARCOS DE PROGRESSÃO SEXUAL (NSFW ON)**:

**Turnos 1-2 (Provocação Inicial)**:
- Olhares, insinuações, toque inicial
- Aproximação física, respiração alterada
- Beijos exploratórios, mãos por cima da roupa
- **Pausar**: Antes de tirar roupas

**Turnos 3-4 (Despir e Exploração)**:
- Remover roupas devagar, revelar corpo
- Carícias diretas, exploração corporal
- Toques íntimos, excitação crescente
- **Pausar**: Antes de toque genital direto

**Turnos 5-6 (Preliminares Intensas)**:
- Sexo oral, masturbação mútua
- Descrições sensoriais intensas
- Preparação para penetração
- **DECISÃO**: Se usuário pede 'continue' aqui → AVANCE para penetração

**Turnos 7-8 (Penetração e Ritmo)**:
- Penetração com descrição sensorial
- Ritmo crescente, posições variadas
- Intensificação de prazer
- **DECISÃO**: Se usuário pede 'continue' aqui → FINALIZE com orgasmo

**Turnos 9-10 (Clímax e Finalização)**:
- Orgasmo mútuo, descrição de sensações
- Contrações, gemidos altos, entrega total
- Pós-clímax: abraço, respiração ofegante
- **APÓS**: Ofereça nova cena ou momento diferente

🚨 **SINAIS DE QUE VOCÊ DEVE FINALIZAR A CENA ATUAL**:
- Usuário pediu 'continue' **3+ vezes** na mesma fase
- Cena passou de **8 turnos**
- Você já está em preliminares há **4+ turnos**


✅ **COMO FINALIZAR DECISIVAMENTE**:

**Avançar para Penetração** (se em preliminares há 4+ turnos):  
"Não aguento mais esperar. Subo em você devagar, guiando teu pau pra dentro de mim.  
Gemo alto quando você me preenche completamente — a sensação é tão intensa que meu corpo inteiro treme."

**Finalizar com Orgasmo** (se em penetração há 3+ turnos):  
"Sinto você pulsar dentro de mim e sei que tá perto. Meu corpo inteiro se contrai, o prazer explode —  
gozo junto com você, gritando teu nome, apertando você com força. Caio no teu peito, ofegante e satisfeita."

**Pós-Clímax** (sempre após orgasmo):  
"Deito no teu peito, ainda sentindo os tremores do orgasmo. Minha respiração aos poucos volta ao normal.  
Beijo teu pescoço devagar e sussurro: 'Que delícia, amor... você me deixa louca.' [Pausa]"

❌ **PROIBIDO (causam repetitividade)**:
- Usar "Meus olhos verdes fixam nos seus" em TODOS os turnos (máximo 1x por cena)
- Usar "sussurro com voz rouca" repetidamente (máximo 2x por cena)
- Usar "melhor parte está por vir" mais de 2 vezes na mesma cena
- Usar "me afasto um pouco" sem depois avançar decisivamente
- Ficar em loop de preliminares por 6+ turnos sem penetração
- Prometer clímax mas nunca chegar lá

"""


# ==============================================
# SERVIÇO PRINCIPAL – MARY
# ==============================================
class MaryService(BaseCharacter):
    id: str = "mary"
    display_name: str = "Mary"

    def _exec_tool_call(self, name: str, args: dict, usuario_key: str) -> str:
        try:
            user_display = st.session_state.get("user_id", "") or ""

            if name == "get_memory_pin":
                return self._build_memory_pin(usuario_key, user_display)

            if name == "set_fact":
                k = (args or {}).get("key", "") if isinstance(args, dict) else ""
                v = (args or {}).get("value", "") if isinstance(args, dict) else ""
                if not k:
                    return "ERRO: chave ('key') não informada."
                set_fact(usuario_key, k, v, {"fonte": "tool_call"})
                try:
                    clear_user_cache(usuario_key)
                except Exception:
                    pass
                return f"OK: {k}={v}"

            if name == "save_event":
                ...
                return f"OK: salvo em {fact_key}"

            if name == "register_entity":
                ent_name = ""
                ent_role = ""
                desc = ""

                if isinstance(args, dict):
                    ent_name = (args.get("name") or "").strip()
                    ent_role = (args.get("role") or "").strip().lower()
                    desc = (args.get("description") or "").strip()

                if not ent_name:
                    return "ERRO: nome da entidade não informado."

                # Slug simples para montar a chave na memória
                slug = re.sub(r"[^a-z0-9]+", "_", ent_name.lower()).strip("_") or "entidade"
                base = f"mary.ent.{slug}"

                # Nome “bonitinho” armazenado
                set_fact(usuario_key, f"{base}.nome", ent_name, {"fonte": "auto_entidade"})

                # Papel (parceiro, médica, rival, etc.)
                if ent_role:
                    set_fact(usuario_key, f"{base}.papel", ent_role, {"fonte": "auto_entidade"})

                # Descrição opcional
                if desc:
                    set_fact(usuario_key, f"{base}.descricao", desc, {"fonte": "auto_entidade"})

                try:
                    clear_user_cache(usuario_key)
                except Exception as e:
                    _log_error("exec_tool_call.clear_user_cache(register_entity)", e)

                return f"OK: entidade registrada em {base}"

            return f"ERRO: ferramenta desconhecida: {name}"

        except Exception as e:
            _log_error("exec_tool_call", e)
            return f"ERRO: {type(e).__name__}: {e}"



       def reply(self, user: str, model: str) -> str:
        prompt = self._get_user_prompt()
        if not prompt:
            return ""

        usuario_key = _current_user_key()
        plow = prompt.strip().lower()

        # A PARTIR DAQUI já entra direto nos comandos:
        # /local, /reset historico, /reset total, etc.


        # ===== Comando manual para mudar o local da cena =====
        if plow.startswith("/local "):
            novo_local = prompt[len("/local "):].strip()
            if novo_local:
                set_fact(usuario_key, "local_cena_atual", novo_local, {"fonte": "chat"})
                clear_user_cache(usuario_key)
                return f"📍 Local da cena atualizado para: **{novo_local}**."


        # ============================
        # /reset historico
        # ============================
        if plow in (
            "/reset historico",
            "/reset histórico",
            "/reset historico mary",
            "/reset histórico mary",
        ):
            try:
                hk = f"history::{usuario_key}"
                st.session_state[hk] = []

                set_fact(
                    usuario_key,
                    "mary.rs.v2",
                    "",
                    {"fonte": "reset_debug"},
                )
                set_fact(
                    usuario_key,
                    "mary.rs.v2.ts",
                    0,
                    {"fonte": "reset_debug"},
                )

                clear_user_cache(usuario_key)

                hist_budget, meta_budget, safety_budget = _budget_slices(model)
                st.session_state["_mem_drop_report"] = {
                    "summarized_pairs": 0,
                    "trimmed_pairs": 0,
                    "hist_tokens": 0,
                    "hist_budget": hist_budget,
                }

            except Exception as e:
                _log_error("reset_history_session", e)
                return (
                    "⚠️ Erro ao resetar histórico desta sessão.\n"
                    f"Detalhe técnico: {type(e).__name__}: {e}"
                )

            return (
                "🧹 **Histórico de diálogo e resumo rolante de Mary foram zerados APENAS para esta sessão.**\n"
                "- Memória canônica (parceiro_atual, casados, local etc.) foi preservada.\n"
                "- Memórias fixas de eventos (`mary.evento.*`) e registros em `mary['evento']` foram preservados.\n"
                "- Nada foi apagado da planilha ou do backend permanente.\n"
                "- Se quiser apagar TAMBÉM as memórias fixas de eventos, use o comando `/reset total`.\n"
                "- Você pode continuar conversando normalmente: Mary ainda se lembra das memórias fixas."
            )

        # ============================
        # /reset total
        # ============================
        if plow in (
            "/reset total",
            "/reset mary total",
            "/reset completo mary",
            "/reset total mary",
        ):
            try:
                from core.repositories import delete_fact
            except Exception:
                delete_fact = None

            if not delete_fact:
                return (
                    "⚠️ Não foi possível executar `/reset total` porque o backend de armazenamento "
                    "não expôs a função `delete_fact` neste ambiente."
                )

            # Apaga resumo rolante
            delete_fact(usuario_key, "mary.rs.v2")
            delete_fact(usuario_key, "mary.rs.v2.ts")

            # Apaga memórias fixas de eventos (mesmo critério do botão de debug)
            facts = cached_get_facts(usuario_key) or {}
            for k in list(facts.keys()):
                sk = str(k)
                if sk.startswith("mary.evento."):
                    delete_fact(usuario_key, sk)

            clear_user_cache(usuario_key)

            return (
                "🧨 **RESET TOTAL DA MARY EXECUTADO PARA ESTE USUÁRIO.**\n"
                "- Resumo rolante (`mary.rs.v2`) apagado.\n"
                "- Memórias fixas de eventos (`mary.evento.*`) apagadas.\n"
                "- Demais fatos canônicos (por exemplo, outras chaves que não começam com `mary.evento.`) foram preservados.\n"
                "- Use com cuidado: cenas futuras não vão mais lembrar os eventos que foram apagados."
            )

        # ============================
        # /debug eventos
        # ============================
        if plow.startswith("/debug eventos"):
            try:
                f_all = cached_get_facts(usuario_key) or {}
            except Exception:
                f_all = {}

            eventos = _collect_mary_events_from_facts(f_all)

            if not eventos:
                return (
                    "🔎 DEBUG EVENTOS\n"
                    f"- user_key: {usuario_key}\n"
                    "- Nenhuma memória de evento encontrada para Mary."
                )

            linhas = [
                "🔎 DEBUG EVENTOS",
                f"- user_key: {usuario_key}",
                f"- total encontrados: {len(eventos)}",
                "",
            ]
            for label, v in sorted(eventos.items()):
                vs = str(v)
                if len(vs) > 220:
                    vs = vs[:220] + "..."
                k_fmt = f"mary.evento.{label}"
                linhas.append(f"• {k_fmt} = {vs}")

            return "\n".join(linhas)

               # === Persona + memórias base ===
        persona_text, history_boot = self._load_persona()

        try:
            f_all = cached_get_facts(usuario_key) or {}
        except Exception:
            f_all = {}
        prefs = _read_prefs(f_all)
        memoria_pin = self._build_memory_pin(usuario_key, user)

        # Memória temática por tags (ciúme, gravidez, etc.)
        thematic_tags = _detect_thematic_tags_from_prompt(prompt)
        thematic_block = _get_thematic_memories_for_tags(usuario_key, thematic_tags)

        # Foco sensorial rotativo
        pool = [
            "cabelo", "olhos", "lábios/boca", "mãos/toque", "respiração",
            "perfume", "pele/temperatura", "quadril/coxas", "voz/timbre", "sorriso"
        ]
        idx = int(st.session_state.get("mary_attr_idx", -1))
        idx = (idx + 1) % len(pool)
        st.session_state["mary_attr_idx"] = idx
        foco = pool[idx]

        # ==== NSFW (core/nsfw.py) + botão do sidebar ====
        local_atual = self._safe_get_local(usuario_key)
        try:
            nsfw_on = bool(nsfw_enabled(usuario_key, local_atual))
        except Exception:
            nsfw_on = True  # fallback: libera se der erro

        if nsfw_on:
            # MODO LIBERADO: usa o FULL_NSFW_PROMPT que você já definiu
            _hint = (
                ": LIBERADO. Use um tom adulto, intenso e apaixonado, "
                "sem se censurar, mantendo coerência com a cena, com o parceiro e com os fatos da memória."
            )
            extra_nsfw_style = "[NSFW_ON]\nEstilo: modo adulto liberado (usa FULL_NSFW_PROMPT)."

            # Anexa o bloco FULL_NSFW ao texto de persona
            persona_text = f"{persona_text}\n\n{FULL_NSFW_PROMPT}"
        else:
            # MODO BLOQUEADO: sensual, mas sem explicitar
            _hint = (
                ": BLOQUEADO. Use sugestão, tensão e intimidade sem descrição explícita de atos; "
                "concentre-se em atmosfera, presença, emoção e condução confiante."
            )
            extra_nsfw_style = SAFE_SENSUAL_PROMPT

            # Anexa apenas o estilo seguro à persona
            persona_text = f"{persona_text}\n\n{SAFE_SENSUAL_PROMPT}"
        # ==== FIM BLOCO NSFW ====

        rolling = self._get_rolling_summary(usuario_key)
        entities_line = _entities_to_line(f_all)

        try:
            docs = cached_get_history(usuario_key) or {}
        except Exception:
            docs = {}
        evidence = self._compact_user_evidence(docs, max_chars=320)

        system_block = _build_system_block(
            persona_text=persona_text,
            rolling_summary=rolling,
            sensory_focus=foco,
            _hint=_hint,
            scene_loc=local_atual or "—",
            entities_line=entities_line,
            evidence=evidence,
            prefs_line=_prefs_line(prefs),
            scene_time=st.session_state.get("momento_atual", "")
        )

        entities_line = _entities_to_line(f_all)

        try:
            docs = cached_get_history(usuario_key) or []
        except Exception:
            docs = []
        evidence = self._compact_user_evidence(docs, max_chars=320)

        system_block = _build_system_block(
            persona_text=persona_text,
            rolling_summary=rolling,
            sensory_focus=foco,
            _hint=_hint,
            scene_loc=local_atual or "—",
            entities_line=entities_line,
            evidence=evidence,
            prefs_line=_prefs_line(prefs),
            scene_time=st.session_state.get("momento_atual", "")
        )


        # ===== Lorebook / memoria longa =====
        lore_msgs: List[Dict[str, str]] = []
        try:
            q = (prompt or "") + "\n" + (rolling or "")
            top = lore_topk(usuario_key, q, k=4, allow_tags=None)
            if top:
                lore_text = " | ".join(d.get("texto", "") for d in top if d.get("texto"))
                if lore_text:
                    lore_msgs.append({"role": "system", "content": f"[LORE]\n{lore_text}"})
        except Exception:
            pass

        verbatim_ultimos = int(st.session_state.get("verbatim_ultimos", 30))
        hist_msgs = self._montar_historico(
            usuario_key,
            history_boot,
            model,
            verbatim_ultimos=verbatim_ultimos,
        )

        eventos_block = ""
        try:
            eventos_dict = _collect_mary_events_from_facts(f_all)
            if eventos_dict:
                linhas_evt = []
                for label, val in sorted(eventos_dict.items()):
                    linhas_evt.append(f"- {label}: {str(val).strip()}")
                joined = "\n".join(linhas_evt)[:1200]
                eventos_block = (
                    "EVENTOS_FIXOS_MARY:\n"
                    "Use estes registros como FONTE DE VERDADE. "
                    "Eles são eventos já vividos pelo casal (ex.: gravidez confirmada, encontros marcantes, etc.).\n"
                    + joined
                )
        except Exception as e:
            eventos_block = ""
            _log_error("reply.build_eventos_block", e)

        # Construção robusta do messages: se algo quebrar aqui, cai num fallback mínimo
                # Construção robusta do messages: se algo quebrar aqui, cai num fallback mínimo
        try:
            messages: List[Dict[str, Any]] = (
                [{"role": "system", "content": system_block}]
                + ([{"role": "system", "content": extra_nsfw_style}] if extra_nsfw_style else [])
                + ([{"role": "system", "content": memoria_pin}] if memoria_pin else [])
                + ([{"role": "system", "content": f"MEMÓRIA_TEMÁTICA:\n{thematic_block}"}]
                   if thematic_block else [])
                + ([{"role": "system", "content": eventos_block}] if eventos_block else [])
                + lore_msgs
                + [{
                    "role": "system",
                    "content": (
                        f"LOCAL_ATUAL: {local_atual or '—'}. "
                        "Regra dura: NÃO mude tempo/lugar sem pedido explícito do usuário."
                    )
                }]
                + hist_msgs
                + [{"role": "user", "content": prompt}]
            )
        except Exception as e:
            # Garante que SEMPRE exista um messages mínimo, mesmo se o log falhar
            messages = [
                {"role": "system", "content": system_block},
                {"role": "user", "content": prompt},
            ]
            try:
                _log_error("reply.build_messages", e)
            except Exception:
                pass


        try:
            _mem_drop_warn(st.session_state.get("_mem_drop_report", {}))
        except Exception as e:
            _log_error("reply.mem_drop_warn", e)

        win = _get_window_for(model)
        try:
            prompt_tokens = sum(toklen(m["content"]) for m in messages)
        except Exception as e:
            _log_error("reply.prompt_tokens", e)
            prompt_tokens = 0

        base_out = _safe_max_output(win, prompt_tokens)
        size = prefs.get("tamanho_resposta", "longa")
        mult = 1.0 if size == "media" else (0.75 if size == "curta" else 1.4)
        max_out = max(512, int(base_out * mult))

        ritmo = prefs.get("ritmo", "rapido")
        temperature = 0.6 if ritmo == "lento" else (0.75 if ritmo == "rapido" else 0.7)

        fallbacks = [
            "together/Qwen/Qwen2.5-72B-Instruct",
            "together/meta-llama/Meta-Llama-3.1-405B-Instruct-Turbo",
            "anthropic/claude-3.5-haiku",
        ]
        tools_to_use = None
        if st.session_state.get("tool_calling_on", False):
            tools_to_use = TOOLS

        max_iterations = 3
        iteration = 0
        texto = ""
        tool_calls = []
        provider = ""
        used_model = ""

        while iteration < max_iterations:
            iteration += 1

            data, used_model, provider = _robust_chat_call(
                model,
                messages,
                max_tokens=max_out,
                temperature=temperature,
                top_p=0.95,
                fallback_models=fallbacks,
                tools=tools_to_use,
            )

            msg = (data.get("choices", [{}])[0].get("message", {}) or {})
            texto = (msg.get("content", "") or "").strip()
            tool_calls = msg.get("tool_calls", [])

            if not tool_calls or not st.session_state.get("tool_calling_on", False):
                break

            st.caption(f"🔧 Executando {len(tool_calls)} ferramenta(s)...")

            messages.append({
                "role": "assistant",
                "content": texto or None,
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                tool_id = tc.get("id", f"call_{iteration}")
                func_name = tc.get("function", {}).get("name", "")
                func_args_str = tc.get("function", {}).get("arguments", "{}")

                try:
                    func_args = json.loads(func_args_str) if func_args_str else {}
                    result = self._exec_tool_call(func_name, func_args, usuario_key)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": result,
                    })

                    st.caption(f"  ✓ {func_name}: {result[:50]}...")

                except Exception as e:
                    _log_error(f"tool_exec::{func_name}", e)
                    error_msg = f"ERRO ao executar {func_name}: {type(e).__name__}: {e}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "content": error_msg,
                    })
                    st.warning(f"⚠️ {error_msg}")

        if iteration >= max_iterations and st.session_state.get("tool_calling_on", False):
            st.warning("⚠️ Limite de iterações de Tool Calling atingido. Resposta pode estar incompleta.")

        try:
            if bool(st.session_state.get("ultra_ia_on", False)) and texto:
                critic_model = st.session_state.get("ultra_critic_model", model) or model
                notes = critic_review(critic_model, system_block, prompt, texto)
                texto = polish(model, system_block, prompt, texto, notes)
        except Exception as e:
            _log_error("reply.ultra_ia", e)

        try:
            if provider and used_model:
                save_interaction(usuario_key, prompt, texto, f"{provider}:{used_model}")
            else:
                save_interaction(usuario_key, prompt, texto, model)
        except Exception as e:
            _log_error("reply.save_interaction", e)


                # [DEPRECATED] Auto-gravação por gatilho de texto + regex de data.
        # A gravação de eventos importantes agora é feita via Tool Calling
        # com a ferramenta `save_event`, chamada pela própria Mary.
        mem_triggers = (
            "use sua ferramenta de memória",
            "mary, use sua ferramenta de memória",
            "mary use sua ferramenta de memória",
            "registre na sua memória",
            "salve na memória",
            "gravar memória",
            "registre o fato",
        )
        plow = prompt.lower()

        if any(t in plow for t in mem_triggers):
            st.caption(
                "🧠 O sistema antigo de gravação automática foi desativado. "
                "Agora Mary decide quando usar a ferramenta `save_event` "
                "para registrar eventos realmente importantes na memória fixa."
            )

        try:
            _extract_and_store_entities(usuario_key, prompt, texto)
        except Exception as e:
            _log_error("reply.extract_and_store_entities", e)

        try:
            self._update_rolling_summary_v2(usuario_key, model, prompt, texto)
        except Exception as e:
            _log_error("reply.update_rolling_summary_v2", e)


        try:
            ph = self._suggest_placeholder(texto, local_atual)
            st.session_state["suggestion_placeholder"] = ph
            st.session_state["last_assistant_message"] = texto
        except Exception:
            st.session_state["suggestion_placeholder"] = ""

        try:
            if provider == "synthetic-fallback":
                st.info("⚠️ Provedor instável. Resposta em fallback — pode continuar normalmente.")
            elif used_model and "together/" in used_model:
                st.caption(f"↪️ Failover automático: **{used_model}**.")
        except Exception:
            pass

        if st.session_state.get("json_mode_on", False):
            try:
                payload = {
                    "role": "assistant",
                    "character": "Mary",
                    "content": texto,
                }
                return json.dumps(payload, ensure_ascii=False, indent=2)
            except Exception:
                return texto

        return texto

    def _load_persona(self) -> Tuple[str, List[Dict[str, str]]]:
        return get_persona()

    def _get_user_prompt(self) -> str:
        return (
            st.session_state.get("chat_input")
            or st.session_state.get("user_input")
            or st.session_state.get("last_user_message")
            or st.session_state.get("prompt")
            or ""
        ).strip()

    def _safe_get_local(self, usuario_key: str) -> str:
        try:
            return get_fact(usuario_key, "local_cena_atual", "") or ""
        except Exception:
            return ""

    def _build_memory_pin(self, usuario_key: str, user_display: str) -> str:
        try:
            f = cached_get_facts(usuario_key) or {}
        except Exception:
            f = {}

        try:
            eventos_todos = _collect_mary_events_from_facts(f)
        except Exception:
            eventos_todos = {}

        try:
            preg_events = _collect_pregnancy_events_from_facts(f)
            preg_from_events = _derive_pregnancy_from_events(preg_events)
        except Exception:
            preg_events = {}
            preg_from_events = {}

        blocos: List[str] = []

        parceiro = f.get("parceiro_atual") or f.get("parceiro") or ""
        nome_usuario = (parceiro or user_display).strip()
        if parceiro:
            blocos.append(f"parceiro_atual={parceiro}")

        casados = bool(f.get("casados", False))
        blocos.append(f"casados={casados}")

        raw_gravida = (
            preg_from_events.get("gravida")
            if "gravida" in preg_from_events
            else f.get("gravida", False)
        )

        if isinstance(raw_gravida, bool):
            gravida = raw_gravida
        else:
            gravida = str(raw_gravida).strip().lower() in (
                "1", "true", "sim", "grávida", "gravida"
            )

        if gravida:
            meses = (
                preg_from_events.get("gravidez.meses")
                or f.get("gravidez.meses")
                or f.get("gravidez.meses_atual")
                or ""
            )
            semanas = (
                preg_from_events.get("gravidez.semanas")
                or f.get("gravidez.semanas")
                or ""
            )
            data_conf = (
                preg_from_events.get("gravidez.data_confirma")
                or f.get("gravidez.data_confirma")
                or ""
            )

            detalhes = ["gravida=True"]
            if meses not in ("", None):
                detalhes.append(f"meses={meses}")
            if semanas not in ("", None):
                detalhes.append(f"semanas={semanas}")
            if data_conf not in ("", None):
                detalhes.append(f"desde={data_conf}")

            blocos.append("; ".join(detalhes))

            if preg_events:
                linhas_tl: List[str] = []
                for label, raw in sorted(preg_events.items()):
                    txt = str(raw or "")
                    compact = " ".join(txt.split())
                    snippet = compact[:220]
                    linhas_tl.append(f"{label}: {snippet}")

                joined = " | ".join(linhas_tl)
                if len(joined) > 800:
                    joined = joined[-800:]

                blocos.append(f"linha_do_tempo_gravidez={{ {joined} }}")

        ent_line = _entities_to_line(f)
        if ent_line and ent_line != "—":
            blocos.append(f"entidades=({ent_line})")

        try:
            ev = last_event(usuario_key, "primeira_vez")
        except Exception:
            ev = None
        if ev:
            ts = ev.get("ts")
            quando = ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)
            blocos.append(f"primeira_vez@{quando}")

        mem_str = "; ".join(blocos) if blocos else "—"

        pin = (
            "MEMÓRIA_PIN: "
            f"NOME_USUARIO={nome_usuario}. FATOS={{ {mem_str} }}.\n"
            "Regras:\n"
            "- Use estes FATOS como verdade canônica sobre relação, gravidez e contexto.\n"
            "- Se linha_do_tempo_gravidez existir, NUNCA trate a gravidez como dúvida ou hipótese.\n"
            "- Use ENTIDADES e EVENTOS_FIXOS_MARY (outro bloco de sistema) como fonte de verdade adicional.\n"
            "- Se algo NÃO estiver na memória, pergunte ou siga o que o usuário disser — não invente."
        )
        return pin

    def _montar_historico(
        self,
        usuario_key: str,
        history_boot: List[Dict[str, str]],
        model: str,
        verbatim_ultimos: int = 30,  # antes era 10
    ) -> List[Dict[str, str]]:
        win = _get_window_for(model)
        hist_budget, meta_budget, _ = _budget_slices(model)

        docs = cached_get_history(usuario_key)
        if not docs:
            st.session_state["_mem_drop_report"] = {}
            return history_boot[:]

        pares: List[Dict[str, str]] = []
        for d in docs:
            u = (d.get("mensagem_usuario") or "").strip()
            a = (d.get("resposta_mary") or "").strip()
            if u:
                pares.append({"role": "user", "content": u})
            if a:
                pares.append({"role": "assistant", "content": a})

        if not pares:
            st.session_state["_mem_drop_report"] = {}
            return history_boot[:]

        keep = max(0, verbatim_ultimos * 2)
        verbatim = pares[-keep:] if keep else []
        antigos = pares[: len(pares) - len(verbatim)]

        msgs: List[Dict[str, str]] = []
        summarized_pairs = 0
        trimmed_pairs = 0

        if antigos:
            summarized_pairs = len(antigos) // 2
            bloco = "\n\n".join(m["content"] for m in antigos)
            resumo = _llm_summarize(model, bloco)
            resumo_layers = [resumo]

            def _count_total_sim(resumo_texts: List[str]) -> int:
                sim_msgs = [{"role": "system", "content": f"[RESUMO-{i+1}]\n{r}"} for i, r in enumerate(resumo_texts)]
                sim_msgs += verbatim
                return sum(toklen(m["content"]) for m in sim_msgs)

            while _count_total_sim(resumo_layers) > hist_budget and len(resumo_layers) < 3:
                resumo_layers[0] = _llm_summarize(model, resumo_layers[0])

            for i, r in enumerate(resumo_layers, start=1):
                msgs.append({"role": "system", "content": f"[RESUMO-{i}]\n{r}"})

        msgs.extend(verbatim)

        def _hist_tokens(mm: List[Dict[str, str]]) -> int:
            return sum(toklen(m["content"]) for m in mm)

         # novo mínimo de pares verbatim que NUNCA cortamos
        min_pairs_to_keep = 6  # 3 interações completas

        while _hist_tokens(msgs) > hist_budget and verbatim:
            if len(verbatim) >= 2:
                verbatim = verbatim[2:]
                trimmed_pairs += 1
            else:
                verbatim = []
            msgs = [m for m in msgs if m["role"] == "system"] + verbatim

        hist_tokens = _hist_tokens(msgs)
        st.session_state["_mem_drop_report"] = {
            "summarized_pairs": summarized_pairs,
            "trimmed_pairs": trimmed_pairs,
            "hist_tokens": hist_tokens,
            "hist_budget": hist_budget,
        }

        return msgs if msgs else history_boot[:]

    # ===== Rolling summary helpers =====
    def _get_rolling_summary(self, usuario_key: str) -> str:
        try:
            f = cached_get_facts(usuario_key) or {}
            return str(f.get("mary.rs.v2", "") or f.get("mary.rolling_summary", "") or "")
        except Exception:
            return ""

    def _should_update_summary(self, usuario_key: str, last_user: str, last_assistant: str) -> bool:
        try:
            f = cached_get_facts(usuario_key)
            last_summary = f.get("mary.rs.v2", "")
            last_update_ts = float(f.get("mary.rs.v2.ts", 0))
            now = time.time()
            if not last_summary:
                return True
            if now - last_update_ts > 300:
                return True
            if (len(last_user) + len(last_assistant)) > 100:
                return True
            return False
        except Exception:
            return True

    def _update_rolling_summary_v2(
        self,
        usuario_key: str,
        model: str,
        last_user: str,
        last_assistant: str
    ) -> None:
        if not self._should_update_summary(usuario_key, last_user, last_assistant):
            return

        try:
            f = cached_get_facts(usuario_key) or {}
            resumo_anterior = str(f.get("mary.rs.v2", "") or "")
            ts_anterior = float(f.get("mary.rs.v2.ts", 0) or 0)
        except Exception:
            resumo_anterior = ""
            ts_anterior = 0.0

        seed = (
            "Você mantém um RESUMO CONTÍNUO da história entre Mary e o usuário.\n\n"
            "TAREFA:\n"
            "- Atualize o RESUMO_ANTERIOR com as NOVAS INFORMAÇÕES da última interação.\n"
            "- Mantenha fatos antigos se ainda forem verdadeiros (nomes, relação, locais, clima, segredos, decisões).\n"
            "- Se algo for claramente desmentido, ajuste ou remova.\n"
            "- Foque em fatos duráveis: relação (casados / cúmplices), locais importantes, eventos marcantes, "
            "acordos, limites, fantasias recorrentes.\n"
            "- Não repita diálogo literal.\n"
            "- Entregue um resumo único, em 8–14 frases telegráficas (pode usar '•' se quiser, mas não é obrigatório)."
        )

        corpo = (
            f"RESUMO_ANTERIOR:\n{resumo_anterior or '(sem resumo anterior ainda)'}\n\n"
            "ULTIMA_INTERACAO:\n"
            f"USER:\n{last_user}\n\n"
            f"MARY:\n{last_assistant}"
        )

        try:
            data, used_model, provider = route_chat_strict(model, {
                "model": model,
                "messages": [
                    {"role": "system", "content": seed},
                    {"role": "user", "content": corpo}
                ],
                "max_tokens": 260,
                "temperature": 0.2,
                "top_p": 0.9,
            })
            resumo_novo = (
                data.get("choices", [{}])[0]
                .get("message", {}) or {}
            ).get("content", "").strip()
            if resumo_novo:
                set_fact(usuario_key, "mary.rs.v2", resumo_novo, {"fonte": "auto_summary"})
                set_fact(usuario_key, "mary.rs.v2.ts", time.time(), {"fonte": "auto_summary"})
                clear_user_cache(usuario_key)
        except Exception:
            return

    # ===== Placeholder leve =====
    def _suggest_placeholder(self, assistant_text: str, scene_loc: str) -> str:
        s = (assistant_text or "").lower()
        if "?" in s:
            return "Continua do ponto exato… me conduz."
        if any(k in s for k in ["vamos", "topa", "que tal", "prefere", "quer"]):
            return "Quero — descreve devagar o próximo passo."
        if scene_loc:
            return f"No {scene_loc} mesmo — fala baixinho no meu ouvido."
        return "Mantém o cenário e dá o próximo passo com calma."

    # ===== Evidência concisa do usuário (últimas falas) =====
    def _compact_user_evidence(self, docs: List[Dict], max_chars: int = 320) -> str:
        snippets: List[str] = []
        for d in reversed(docs):
            u = (d.get("mensagem_usuario") or "").strip()
            if u:
                u = re.sub(r"\s+", " ", u)
                snippets.append(u)
            if len(snippets) >= 4:
                break
        s = " | ".join(reversed(snippets))[:max_chars]
        return s

    # ===== Sidebar (somente leitura, sem menu de preferências) =====
    def render_sidebar(self, container) -> None:
        container.markdown(
            "**Mary — Esposa Cúmplice** • Respostas sensuais contínuas, com memória canônica de relação, gravidez e eventos fixos."
        )

        usuario_key = _current_user_key()

        try:
            f = cached_get_facts(usuario_key) or {}
        except Exception:
            f = {}

        casados = bool(f.get("casados", False))
        ent = _entities_to_line(f)
        rs = (f.get("mary.rs.v2") or "")[:200]

        container.caption(f"Estado da relação: **{'Casados' if casados else '—'}**")
        container.markdown("---")

        json_on = container.checkbox(
            "JSON Mode",
            value=bool(st.session_state.get("json_mode_on", False))
        )
        tool_on = container.checkbox(
            "Tool-Calling",
            value=bool(st.session_state.get("tool_calling_on", False))
        )
        st.session_state["json_mode_on"] = json_on
        st.session_state["tool_calling_on"] = tool_on

        lora = container.text_input(
            "Adapter ID (Together LoRA) — opcional",
            value=st.session_state.get("together_lora_id", "")
        )
        st.session_state["together_lora_id"] = lora

        if ent and ent != "—":
            container.caption(f"Entidades salvas: {ent}")
        if rs:
            container.caption("Resumo rolante ativo (v2).")

        # DEBUG – facts brutos
        with container.expander("🔎 DEBUG – facts brutos", expanded=False):
            if not f:
                st.caption("⚠️ Nenhum fact retornado para esta chave de usuário.")
                st.code(usuario_key)
            else:
                st.caption(f"User key: `{usuario_key}`")
                for k, v in f.items():
                    vs = str(v)
                    if len(vs) > 120:
                        vs = vs[:120] + "..."
                    st.write(f"- **{k}** = {vs}")

        # 🧠 Memórias fixas de Mary
        with container.expander("🧠 Memórias fixas de Mary", expanded=True):
            try:
                f_all = cached_get_facts(usuario_key) or {}
            except Exception:
                f_all = {}

            eventos = _collect_mary_events_from_facts(f_all)

            last_key = st.session_state.get("last_saved_mary_event_key", "")
            last_val = st.session_state.get("last_saved_mary_event_val", "")
            if last_key:
                short = last_key.replace("mary.evento.", "")
                if short not in eventos:
                    eventos[short] = last_val or "(salvo nesta sessão; aguardando backend)"

            if not eventos:
                container.caption(
                    "Nenhuma memória salva ainda.\n"
                    "Ex.: **Mary, use sua ferramenta de memória para registrar o fato: ...**"
                )
            else:
                for label, val in sorted(eventos.items()):
                    container.markdown(f"**{label}**")
                    container.caption(val[:280] + ("..." if len(val) > 280 else ""))

                    col1, col2 = container.columns([1, 1])
                    with col1:
                        if container.button("🗑 Apagar", key=f"del_{usuario_key}_{label}"):
                            try:
                                from core.repositories import delete_fact
                            except Exception:
                                delete_fact = None

                            if delete_fact:
                                delete_fact(usuario_key, f"mary.evento.{label}")
                                delete_fact(usuario_key, f"mary.eventos.{label}")
                            clear_user_cache(usuario_key)
                            container.success(f"Memória **{label}** apagada.")
                            try:
                                st.rerun()
                            except Exception:
                                try:
                                    st.experimental_rerun()
                                except Exception:
                                    pass
                    with col2:
                        container.caption(f"mary.evento.{label}")

        # 🔄 Reset REAL da Mary
        if container.button("🔄 Reset REAL da Mary"):
            try:
                from core.repositories import delete_fact
            except Exception:
                delete_fact = None

            user = _current_user_key()

            if delete_fact:
                delete_fact(user, "mary.rs.v2")
                delete_fact(user, "mary.rs.v2.ts")

                facts = cached_get_facts(user) or {}
                for k in list(facts.keys()):
                    if str(k).startswith("mary.evento."):
                        delete_fact(user, k)

            clear_user_cache(user)
            container.success("Mary resetada COMPLETAMENTE (resumo + eventos fixos) para este usuário.")
            try:
                st.rerun()
            except Exception:
                try:
                    st.experimental_rerun()
                except Exception:
                    pass
