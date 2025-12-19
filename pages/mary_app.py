from __future__ import annotations

import time
import re
import traceback
import importlib
import inspect

import streamlit as st

import characters.mary.persona as mary_persona
from characters.mary.service import MaryService, _current_user_key
from characters.mary.persona import get_persona

from core.service_router import list_models
from core.database import db_status
from core.repositories import (
    save_interaction,
    get_history_docs,
    get_history_docs_multi,
    get_facts,
    delete_fact,
    delete_last_interaction,
    delete_user_history,
)

# ==========================================================
# CONFIG
# ==========================================================
st.set_page_config(
    page_title="Mary – Esposa Cúmplice",
    page_icon="💍💍",
    layout="centered",
)

SENHA_CORRETA = "311071"
DEFAULT_VISUAL_LIMIT = 80

DEFAULT_MODEL = "tngtech/deepseek-r1t2-chimera:free"
FALLBACK_MODEL = "deepseek/deepseek-chat-v3-0324"


# ==========================================================
# UI / CSS (remove tarjas + dark + floating cards)
# ==========================================================
def _apply_dark_ui() -> None:
    st.markdown(
        """
        <style>
        /* ===== Remove tarjas brancas e “barras” do Streamlit ===== */
        html, body { background: #000 !important; }
        .stApp { background: #000 !important; }

        /* Remove espaço do topo/rodapé (as “tarjas”) */
        header { visibility: hidden; height: 0px; }
        footer { visibility: hidden; height: 0px; }
        [data-testid="stToolbar"] { visibility: hidden; height: 0px; }
        [data-testid="stDecoration"] { visibility: hidden; height: 0px; }

        /* Zera padding superior/inferior do container principal */
        .block-container {
            padding-top: 0.35rem !important;
            padding-bottom: 0.35rem !important;
        }

        /* Sidebar */
        section[data-testid="stSidebar"] {
            background-color: #070707 !important;
            border-right: 1px solid #1a1a1a !important;
        }
        section[data-testid="stSidebar"] * { color: #f2f2f2 !important; }

        /* Texto global */
        .stApp, .stApp * { color: #f2f2f2 !important; }

        /* Inputs */
        input, textarea {
            background-color: #0b0b0b !important;
            color: #f2f2f2 !important;
            border: 1px solid #2a2a2a !important;
        }

        /* Selectbox */
        div[data-baseweb="select"] > div {
            background-color: #0b0b0b !important;
            border: 1px solid #2a2a2a !important;
        }
        div[data-baseweb="select"] * { color: #f2f2f2 !important; }

        /* Botões */
        button {
            background-color: #111 !important;
            color: #f2f2f2 !important;
            border: 1px solid #2a2a2a !important;
        }
        button:hover { border-color: #3a3a3a !important; }

        /* Expander */
        div[data-testid="stExpander"] {
            background-color: #0a0a0a !important;
            border: 1px solid #1a1a1a !important;
            border-radius: 12px !important;
        }

        /* Separadores */
        hr {
            border: none !important;
            border-top: 1px solid #1a1a1a !important;
        }

        /* Código */
        pre, code {
            background-color: #0b0b0b !important;
            border: 1px solid #1a1a1a !important;
            color: #f2f2f2 !important;
        }

        /* ===== Caixa flutuante (chat bubbles) ===== */
        div[data-testid="stChatMessage"] {
            margin: 0.75rem 0 !important;
        }

        div[data-testid="stChatMessage"] > div {
            background: rgba(18, 18, 18, 0.72) !important;
            border: 1px solid rgba(255, 255, 255, 0.08) !important;
            border-radius: 18px !important;
            padding: 16px 16px 12px 16px !important;

            box-shadow:
                0 10px 24px rgba(0,0,0,0.55),
                0 2px 6px rgba(0,0,0,0.35) !important;

            backdrop-filter: blur(10px) !important;
            -webkit-backdrop-filter: blur(10px) !important;
        }

        /* Diferenciar user x assistant sem depender de classes instáveis */
        div[data-testid="stChatMessage"] p {
            margin: 0 0 0.95rem 0 !important;
            line-height: 1.6 !important;
            font-size: 1.02rem !important;
            color: #f2f2f2 !important;
        }
        div[data-testid="stChatMessage"] p:last-child { margin-bottom: 0 !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _format_paragraphs(text: str) -> str:
    t = (text or "").strip()
    if not t:
        return t
    if "\n\n" in t:
        return t

    if "\n" in t and t.count("\n") >= 2:
        t2 = re.sub(r"\n{2,}", "\n\n", t)
        if "\n\n" in t2:
            return t2
        t = t2.replace("\n", " ")

    sentences = re.split(r"(?<=[.!?…])\s+", t)
    sentences = [s.strip() for s in sentences if s.strip()]
    if len(sentences) <= 3:
        return t

    chunks = []
    i = 0
    while i < len(sentences):
        size = 2 if (i % 6) != 4 else 3
        chunk = " ".join(sentences[i : i + size]).strip()
        if chunk:
            chunks.append(chunk)
        i += size

    return "\n\n".join(chunks)


# ==========================================================
# SENHA
# ==========================================================
def check_password() -> bool:
    if "senha_ok" not in st.session_state:
        st.session_state["senha_ok"] = False
    if st.session_state["senha_ok"]:
        return True

    _apply_dark_ui()
    st.title("🔐 Mary – Acesso Restrito")
    with st.form("form_senha", clear_on_submit=False):
        senha = st.text_input("Digite a senha de acesso:", type="password")
        ok = st.form_submit_button("Entrar")

    if ok:
        if senha == SENHA_CORRETA:
            st.session_state["senha_ok"] = True
            st.success("Acesso liberado!")
            st.rerun()
        else:
            st.error("Senha incorreta. Tente novamente.")
    return False


if not check_password():
    st.stop()


# ==========================================================
# HELPERS
# ==========================================================
def _get_service() -> MaryService:
    svc = st.session_state.get("_mary_service")
    if svc is None:
        svc = MaryService()
        st.session_state["_mary_service"] = svc
    return svc


def _invalidate_backend_cache() -> None:
    st.session_state["backend_hist_cache"] = None
    st.session_state["backend_hist_cache_ts"] = 0.0


def _keys_para_mary() -> list[str]:
    usuario_key = _current_user_key()  # ex: Janio Donisete::mary
    usuario_legado = str(st.session_state.get("user_id") or "").strip()  # ex: Janio Donisete
    keys = [usuario_key]
    if usuario_legado and usuario_legado != usuario_key:
        keys.append(usuario_legado)
    return keys


def _choose_default_model(available: list[str]) -> str:
    if available and DEFAULT_MODEL in available:
        return DEFAULT_MODEL
    if available:
        # não força excluir grok; apenas evita como default se houver outros
        non_grok = [m for m in available if "grok" not in (m or "").lower()]
        return non_grok[0] if non_grok else available[0]
    return FALLBACK_MODEL


def _garantir_estado_inicial() -> None:
    if "user_id" not in st.session_state or not st.session_state["user_id"]:
        st.session_state["user_id"] = "Janio Donisete"

    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []

    # modelos disponíveis
    try:
        modelos = list_models() or []
    except Exception:
        modelos = []

    # garante model persistente: só troca se vazio ou inválido
    if "model" not in st.session_state or not st.session_state["model"]:
        st.session_state["model"] = _choose_default_model(modelos)
    else:
        if modelos and st.session_state["model"] not in modelos:
            st.session_state["model"] = _choose_default_model(modelos)

    if "mary_nsfw_on" not in st.session_state:
        st.session_state["mary_nsfw_on"] = True
    if "mary_intro_done" not in st.session_state:
        st.session_state["mary_intro_done"] = False
    if "visual_limit" not in st.session_state:
        st.session_state["visual_limit"] = DEFAULT_VISUAL_LIMIT
    if "backend_hist_cache" not in st.session_state:
        st.session_state["backend_hist_cache"] = None
    if "backend_hist_cache_ts" not in st.session_state:
        st.session_state["backend_hist_cache_ts"] = 0.0

    # debounce
    if "last_submit_ts" not in st.session_state:
        st.session_state["last_submit_ts"] = 0.0
    if "last_submit_text" not in st.session_state:
        st.session_state["last_submit_text"] = ""


def _clear_service_caches_for_keys(keys: list[str]) -> None:
    # MaryService cacheia facts/history com chaves "facts::" e "history::"
    for k in keys:
        for ck in (f"facts::{k}", f"history::{k}"):
            if ck in st.session_state:
                del st.session_state[ck]


def _gerar_fala_inicial_e_salvar_backend() -> str:
    try:
        _, history_boot = get_persona()
    except Exception:
        history_boot = []

    intro = ""
    if isinstance(history_boot, list):
        for msg in history_boot:
            if isinstance(msg, dict) and msg.get("content"):
                intro = str(msg["content"]).strip()
                break

    if not intro:
        intro = "Oi… eu tô aqui. Vamos começar do zero, do jeito certo."

    try:
        keys = _keys_para_mary()
        usuario_key = keys[0]
        if not st.session_state.get("mary_intro_done", False):
            save_interaction(usuario_key, "[FALA_INICIAL_MARY]", intro, "mary-persona-static")
    except Exception as e:
        st.error(f"⚠️ Erro ao salvar fala inicial: {e}")

    return intro


def _colar_fala_inicial_na_tela() -> None:
    intro = _gerar_fala_inicial_e_salvar_backend()
    st.session_state["chat_history"] = [("assistant", intro)]
    st.session_state["mary_intro_done"] = True


def _carregar_chat_visual_do_backend(force: bool = False) -> list[tuple[str, str]]:
    now = time.time()

    if not force:
        cached = st.session_state.get("backend_hist_cache")
        ts = float(st.session_state.get("backend_hist_cache_ts", 0.0))
        if cached is not None and (now - ts) < 2.0:
            return cached

    keys = _keys_para_mary()

    try:
        docs = get_history_docs_multi(keys, limit=800) or []
    except Exception as e:
        st.session_state["last_model_error"] = f"BOOT history load failed: {type(e).__name__}: {e}"
        st.error("💥 Falha ao carregar histórico do backend (isso causa o ‘reset’ no refresh).")
        st.write("Chaves consultadas:", keys)
        st.code(traceback.format_exc())
        # IMPORTANTÍSSIMO: não finge que está vazio — retorna e deixa o app parar aqui
        st.stop()

    hist: list[tuple[str, str]] = []
    for d in docs:
        u = (d.get("mensagem_usuario") or "").strip()
        a = (d.get("resposta_mary") or "").strip()
        if u:
            hist.append(("user", u))
        if a:
            hist.append(("assistant", a))

    st.session_state["backend_hist_cache"] = hist
    st.session_state["backend_hist_cache_ts"] = now
    return hist



def _apagar_hist_bd_novo_e_legado() -> int:
    keys = _keys_para_mary()
    total = 0
    for k in keys:
        try:
            total += int(delete_user_history(k) or 0)
        except Exception:
            pass
    return total


def _diagnostico_hist(keys: list[str]) -> dict:
    out = {}
    for k in keys:
        try:
            docs = get_history_docs(k, limit=5) or []
            out[k] = {
                "count_approx_5": len(docs),
                "first_user": (docs[0].get("mensagem_usuario") if docs else None),
                "first_mary": (docs[0].get("resposta_mary") if docs else None),
                "last_user": (docs[-1].get("mensagem_usuario") if docs else None),
                "last_mary": (docs[-1].get("resposta_mary") if docs else None),
            }
        except Exception as e:
            out[k] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _apagar_eventos_mary_fact(usuario_key: str) -> int:
    try:
        facts = get_facts(usuario_key) or {}
    except Exception:
        facts = {}

    keys = [
        k
        for k in facts.keys()
        if isinstance(k, str) and (k.startswith("mary.evento.") or k.startswith("mary.eventos."))
    ]
    removed = 0
    for k in keys:
        try:
            if delete_fact(usuario_key, k):
                removed += 1
        except Exception:
            pass
    return removed


def _delete_last_turn(keys: list[str]) -> bool:
    """
    Delete do último registro no backend, tentando novo + legado.
    Depois limpa caches do Service e invalida cache visual.
    """
    ok = False
    for k in keys:
        try:
            ok = bool(delete_last_interaction(k))
            if ok:
                break
        except Exception:
            pass

    _invalidate_backend_cache()
    _clear_service_caches_for_keys(keys)
    return ok


# ==========================================================
# APP
# ==========================================================
def main() -> None:
    _apply_dark_ui()
    _garantir_estado_inicial()
    svc = _get_service()

    st.caption("🧩 mary_app.py v3.1 (FIX: apagar turno + limpar cache do service + debounce + erro visível)")

    backend, detail = db_status()
    st.caption(f"🗄️ Backend atual: **{backend}** ({detail})")

    st.title("Mary – Esposa Cúmplice 💍💍")

    # ===== BACKEND =====
    keys = _keys_para_mary()
    with st.expander("🧨 BACKEND — apagar histórico de verdade + diagnóstico", expanded=False):
        st.write("Chaves usadas (novo + legado):", keys)

        if st.button("🔎 Diagnóstico agora"):
            st.json(_diagnostico_hist(keys))

        colA, colB = st.columns(2)
        with colA:
            confirmar = st.checkbox("Confirmo apagar TODO histórico do BD (history) para novo+legado", value=False)
            if st.button("🔥 APAGAR HISTÓRICO DO BD (AGORA)", type="primary"):
                if not confirmar:
                    st.error("Marque a confirmação.")
                else:
                    n = _apagar_hist_bd_novo_e_legado()
                    _invalidate_backend_cache()
                    _clear_service_caches_for_keys(keys)
                    st.session_state["chat_history"] = []
                    st.session_state["mary_intro_done"] = False
                    st.success(f"✅ Apaguei do BD (history): {n} registros (novo+legado).")
                    st.rerun()

        with colB:
            confirmar2 = st.checkbox("Confirmo apagar facts mary.evento.* também", value=False)
            if st.button("💣 APAGAR EVENTOS mary.evento.* (facts)"):
                if not confirmar2:
                    st.error("Marque a confirmação.")
                else:
                    removed = _apagar_eventos_mary_fact(_current_user_key())
                    _clear_service_caches_for_keys(keys)
                    st.success(f"✅ Apaguei {removed} facts de eventos mary.evento.*")
                    st.rerun()

    # ===== SIDEBAR =====
    with st.sidebar:
        st.header("Mary – Controles")

        st.text_input("👤 Usuário", value="Janio Donisete", disabled=True)
        st.caption(f"🔑 usuario_key atual: {_current_user_key()}")

        try:
            all_models = list_models() or []
        except Exception:
            all_models = []
        if not all_models:
            all_models = [FALLBACK_MODEL]

        # se o estado atual não existe mais, volta pro default
        if st.session_state.get("model") not in all_models:
            st.session_state["model"] = _choose_default_model(all_models)

        current = st.session_state.get("model")
        idx = all_models.index(current) if current in all_models else 0

        # ✅ selectbox fixa qualquer modelo escolhido (inclusive grok)
        st.selectbox("🧠 Modelo", all_models, index=idx, key="model")

        st.markdown("---")
        st.checkbox("Modo adulto liberado (NSFW)", key="mary_nsfw_on")

        st.markdown("---")
        st.subheader("Turnos")

        if st.button("Apagar último turno (backend)"):
            ok = _delete_last_turn(keys)

            if ok:
                st.session_state["chat_history"] = _carregar_chat_visual_do_backend(force=True)
                st.success("✅ Último turno apagado e tela atualizada.")
            else:
                st.warning("Nada para apagar (backend não retornou sucesso).")

            st.rerun()

        st.markdown("---")
        st.subheader("Limpar tela")
        if st.button("Limpar tela (visual)"):
            st.session_state["chat_history"] = []
            st.rerun()

        st.markdown("---")
        st.subheader("🎭 Persona")
        st.caption("Arquivo ativo:")
        st.code(inspect.getfile(mary_persona.get_persona))

        if st.button("♻️ Recarregar persona AGORA"):
            importlib.reload(mary_persona)
            st.session_state.pop("_mary_service", None)
            st.session_state["mary_intro_done"] = False
            st.session_state["chat_history"] = []
            st.session_state["backend_hist_cache"] = None
            st.session_state["backend_hist_cache_ts"] = 0.0
            _clear_service_caches_for_keys(keys)
            st.success("Persona recarregada. Fala inicial será regenerada.")
            st.rerun()

    # ===== BOOT =====
    if not st.session_state["chat_history"]:
        backend_hist = _carregar_chat_visual_do_backend(force=False)

        if backend_hist:
            st.session_state["chat_history"] = backend_hist
            st.session_state["mary_intro_done"] = True
        else:
            # Confirma de verdade se NÃO existe histórico em nenhuma key
            keys = _keys_para_mary()
            has_any = False
            for k in keys:
                try:
                    if (get_history_docs(k, limit=1) or []):
                        has_any = True
                        break
                except Exception as e:
                    st.session_state["last_model_error"] = f"BOOT history probe failed: {type(e).__name__}: {e}"
                    st.error("💥 Falha ao checar existência de histórico no backend.")
                    st.write("Key:", k)
                    st.code(traceback.format_exc())
                    st.stop()

        if has_any:
            st.warning("⚠️ Existe histórico no BD, mas o merge retornou vazio. Verifique get_history_docs_multi / filtros.")
        else:
            if not st.session_state.get("mary_intro_done", False):
                _colar_fala_inicial_na_tela()


    # ===== RENDER =====
    hist = st.session_state.get("chat_history", [])
    visual_limit = int(st.session_state.get("visual_limit", DEFAULT_VISUAL_LIMIT))
    visible = hist[-visual_limit:] if len(hist) > visual_limit else hist

    for role, content in visible:
        with st.chat_message(role):
            if role == "assistant":
                st.markdown(_format_paragraphs(content))
            else:
                st.markdown(content)

    # ===== INPUT =====
    prompt = st.chat_input("Fala algo pra Mary...")
    if prompt:
        # ✅ debounce anti-duplicação
        now = time.time()
        last_ts = float(st.session_state.get("last_submit_ts", 0.0))
        last_txt = str(st.session_state.get("last_submit_text", ""))

        if prompt.strip() == last_txt.strip() and (now - last_ts) < 1.2:
            st.warning("⚠️ Mensagem repetida muito rápido. Ignorando para evitar duplicação.")
            st.stop()

        st.session_state["last_submit_ts"] = now
        st.session_state["last_submit_text"] = prompt

        st.session_state["chat_history"].append(("user", prompt))
        with st.chat_message("user"):
            st.markdown(prompt)

        st.session_state["chat_input"] = prompt

        try:
            resposta = svc.reply(
                user=st.session_state.get("user_id", "Janio Donisete"),
                model=st.session_state.get("model") or DEFAULT_MODEL,
            )
        except Exception as e:
            st.session_state["chat_input"] = ""
            st.error(f"💥 Erro real ao chamar o modelo: {type(e).__name__}: {e}")
            st.code(traceback.format_exc())
            st.stop()

        st.session_state["chat_input"] = ""

        if not (resposta or "").strip():
            st.warning("⚠️ O modelo retornou vazio. Veja a sidebar (último erro) ou rode o diagnóstico.")
            st.stop()

        with st.chat_message("assistant"):
            st.markdown(_format_paragraphs(resposta))

        st.session_state["chat_history"].append(("assistant", resposta))
        _invalidate_backend_cache()


if __name__ == "__main__":
    main()
