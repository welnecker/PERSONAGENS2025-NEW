from __future__ import annotations

import time
import streamlit as st

from characters.mary.service import MaryService, _current_user_key
from characters.mary.persona import get_persona
from core.service_router import list_models
from core.repositories import (
    save_interaction,
    get_history_docs,
    get_history_docs_multi,
    get_facts,
    set_fact,
    delete_last_interaction,
    delete_user_history,
)

# ✅ PRIMEIRA CHAMADA
st.set_page_config(
    page_title="Mary – Esposa Cúmplice",
    page_icon="💍",
    layout="centered",
)

# ==========================================================
# BLOQUEIO POR SENHA
# ==========================================================
SENHA_CORRETA = "141267"

def check_password() -> bool:
    if "senha_ok" not in st.session_state:
        st.session_state["senha_ok"] = False

    if st.session_state["senha_ok"]:
        return True

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
# ESTADO / CACHE
# ==========================================================
DEFAULT_VISUAL_LIMIT = 80

def _get_service() -> MaryService:
    svc = st.session_state.get("_mary_service")
    if svc is None:
        svc = MaryService()
        st.session_state["_mary_service"] = svc
    return svc

def _invalidate_backend_cache() -> None:
    st.session_state["backend_hist_cache"] = None
    st.session_state["backend_hist_cache_ts"] = 0.0

def _sync_nsfw_to_backend() -> None:
    """✅ Persiste o toggle NSFW no BD em facts['mary.nsfw']."""
    usuario_key = _current_user_key()
    v = bool(st.session_state.get("mary_nsfw_on", True))
    try:
        set_fact(usuario_key, "mary.nsfw", v, {"fonte": "sidebar"})
    except Exception as e:
        st.error(f"Falha ao salvar NSFW no backend: {e}")

def _keys_para_mary() -> list[str]:
    """✅ Chave nova + chave legada (para não perder histórico antigo)."""
    usuario_key = _current_user_key()  # ex: "Janio::mary"
    usuario_legado = str(st.session_state.get("user_id") or "").strip()  # ex: "Janio"
    keys = [usuario_key]
    if usuario_legado and usuario_legado != usuario_key:
        keys.append(usuario_legado)
    return keys

def _on_user_change() -> None:
    """
    ✅ Trocar user_id precisa:
    - limpar chat visual
    - invalidar cache
    - resetar intro_done
    - recarregar do backend no próximo ciclo
    """
    st.session_state["chat_history"] = []
    st.session_state["mary_intro_done"] = False
    _invalidate_backend_cache()

def _garantir_estado_inicial() -> None:
    if "user_id" not in st.session_state or not st.session_state["user_id"]:
        st.session_state["user_id"] = "Janio"

    if "chat_history" not in st.session_state:
        st.session_state["chat_history"] = []

    if "model" not in st.session_state:
        try:
            modelos = list_models() or []
        except Exception:
            modelos = []
        st.session_state["model"] = modelos[0] if modelos else "deepseek/deepseek-chat-v3-0324"

    # ✅ NSFW: tenta carregar do BD 1x, antes do default
    if "mary_nsfw_on" not in st.session_state:
        try:
            usuario_key = _current_user_key()
            f = get_facts(usuario_key) or {}
            v = f.get("mary.nsfw", None)
            if isinstance(v, bool):
                st.session_state["mary_nsfw_on"] = v
            else:
                st.session_state["mary_nsfw_on"] = True
        except Exception:
            st.session_state["mary_nsfw_on"] = True

    if "mary_intro_done" not in st.session_state:
        st.session_state["mary_intro_done"] = False

    if "visual_limit" not in st.session_state:
        st.session_state["visual_limit"] = DEFAULT_VISUAL_LIMIT

    if "backend_hist_cache" not in st.session_state:
        st.session_state["backend_hist_cache"] = None
    if "backend_hist_cache_ts" not in st.session_state:
        st.session_state["backend_hist_cache_ts"] = 0.0

def _tem_historico_no_backend(keys: list[str]) -> bool:
    """✅ Checa novo+legado (evita duplicar fala inicial)."""
    try:
        docs = get_history_docs_multi(keys, limit=1) or []
        return bool(docs)
    except Exception:
        # fallback (melhor que nada)
        try:
            return bool(get_history_docs(keys[0], limit=1) or [])
        except Exception:
            return False

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
        intro = (
            "Eu ajeito o cabelo, dou um sorriso de canto e fico te observando por um instante.\n\n"
            "\"Então… vamos continuar de onde a gente parou, amor?\""
        )

    # ✅ Evita duplicar: só salva se NÃO houver histórico nem novo nem legado
    try:
        keys = _keys_para_mary()
        if not _tem_historico_no_backend(keys):
            save_interaction(keys[0], "[FALA_INICIAL_MARY]", intro, "mary-persona-static")
    except Exception:
        pass

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
        docs = get_history_docs_multi(keys, limit=400) or []
    except Exception:
        docs = []

    # ✅ Se tiver timestamp no doc, ordena (evita “mistura”)
    def _ts(d: dict) -> float:
        for k in ("ts", "timestamp", "created_at", "time"):
            v = d.get(k)
            try:
                if isinstance(v, (int, float)):
                    return float(v)
            except Exception:
                pass
        return 0.0

    if docs and isinstance(docs[0], dict) and any(k in docs[0] for k in ("ts", "timestamp", "created_at", "time")):
        docs = sorted(docs, key=_ts)

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

def _apagar_ultimo_turno_backend_e_sync() -> None:
    """
    ✅ Apaga DO BANCO e sincroniza a tela.
    """
    usuario_key = _current_user_key()

    try:
        ok = delete_last_interaction(usuario_key)
    except Exception as e:
        st.error(f"Falha ao apagar no backend: {e}")
        return

    _invalidate_backend_cache()

    backend_hist = _carregar_chat_visual_do_backend(force=True)

    if backend_hist:
        st.session_state["chat_history"] = backend_hist
        st.session_state["mary_intro_done"] = True
    else:
        st.session_state["chat_history"] = []
        st.session_state["mary_intro_done"] = False
        _colar_fala_inicial_na_tela()

    if not ok:
        st.warning("Não havia turno para apagar no backend (histórico vazio ou já apagado).")

def _list_eventos_mary(facts: dict) -> list[tuple[str, str]]:
    eventos: list[tuple[str, str]] = []
    if not isinstance(facts, dict):
        return eventos
    for k, v in facts.items():
        if not isinstance(k, str) or not v:
            continue
        if k.startswith("mary.evento."):
            eventos.append((k.replace("mary.evento.", "", 1), str(v)))
        elif k.startswith("mary.eventos."):
            eventos.append((k.replace("mary.eventos.", "", 1), str(v)))
    eventos.sort(key=lambda x: x[0])
    return eventos

def _apagar_hist_bd_novo_e_legado() -> int:
    """
    ✅ Apaga histórico do BD para a chave nova e para a chave legada (se existir).
    """
    keys = _keys_para_mary()
    total = 0
    # apaga a chave nova
    try:
        total += int(delete_user_history(keys[0]) or 0)
    except Exception:
        pass
    # apaga legado se for diferente
    if len(keys) > 1:
        try:
            total += int(delete_user_history(keys[1]) or 0)
        except Exception:
            pass
    return total

# ==========================================================
# APP
# ==========================================================
def main() -> None:
    _garantir_estado_inicial()
    svc = _get_service()

    st.title("Mary – Esposa Cúmplice 💍")

    # ========= SIDEBAR =========
    with st.sidebar:
        st.header("Mary – Controles")

        # ✅ on_change para trocar user sem misturar cache/histórico
        st.text_input("👤 Usuário", key="user_id", on_change=_on_user_change)
        st.caption(f"🔑 usuario_key atual: { _current_user_key() }")

        try:
            all_models = list_models() or []
        except Exception:
            all_models = []
        if not all_models:
            all_models = ["deepseek/deepseek-chat-v3-0324"]

        st.selectbox(
            "🧠 Modelo",
            all_models,
            index=all_models.index(st.session_state["model"]) if st.session_state["model"] in all_models else 0,
            key="model",
        )

        st.markdown("---")

        st.checkbox(
            "Modo adulto liberado (NSFW)",
            key="mary_nsfw_on",
            help="ON = adulto direto. OFF = sugestivo/romântico.",
            on_change=_sync_nsfw_to_backend,
        )

        st.markdown("---")
        st.subheader("🎭 Persona / Turnos")

        col1, col2 = st.columns(2)
        with col1:
            if st.button("Recarregar persona", use_container_width=True):
                # ✅ recarrega só visual (não regrava se já há histórico)
                _invalidate_backend_cache()
                st.session_state["chat_history"] = []
                st.session_state["mary_intro_done"] = False
                st.rerun()

        with col2:
            if st.button("Apagar último turno", use_container_width=True):
                _apagar_ultimo_turno_backend_e_sync()
                st.rerun()

        st.markdown("---")
        st.subheader("🧹 Limpeza / Reset")

        if st.button("Limpar tela (visual)"):
            st.session_state["chat_history"] = []
            st.rerun()

        if st.button("Reset histórico (sessão)"):
            st.session_state["chat_input"] = "/reset historico"
            resp = svc.reply(
                user=st.session_state.get("user_id", "Janio"),
                model=st.session_state.get("model"),
            )
            st.session_state["chat_input"] = ""
            st.session_state["chat_history"].append(("assistant", resp))
            _invalidate_backend_cache()
            st.rerun()

        if st.button("RESET TOTAL (memórias fixas)"):
            st.session_state["chat_input"] = "/reset total"
            resp = svc.reply(
                user=st.session_state.get("user_id", "Janio"),
                model=st.session_state.get("model"),
            )
            st.session_state["chat_input"] = ""
            st.session_state["chat_history"].append(("assistant", resp))
            _invalidate_backend_cache()
            st.rerun()

        st.markdown("---")
        st.subheader("🧠 Memória / Diagnóstico")

        err = st.session_state.get("mary_last_model_error", "")
        if err:
            st.caption(f"⚠️ Último erro de modelo: {err}")

        usuario_key = _current_user_key()
        try:
            facts = get_facts(usuario_key) or {}
        except Exception:
            facts = {}

        with st.expander("📌 Eventos fixos (mary.evento.*)", expanded=False):
            eventos = _list_eventos_mary(facts)
            if not eventos:
                st.caption("Nenhum evento fixo registrado ainda.")
            else:
                for label, val in eventos:
                    st.markdown(f"**{label}**")
                    vv = str(val)
                    st.caption(vv[:500] + ("..." if len(vv) > 500 else ""))

        with st.expander("📍 Cena atual — local", expanded=False):
            local_atual = str((facts or {}).get("local_cena_atual", "") or "")
            novo_local = st.text_input("Local da cena (canônico)", value=local_atual)
            if st.button("Salvar local"):
                try:
                    set_fact(usuario_key, "local_cena_atual", (novo_local or "").strip(), {"fonte": "sidebar"})
                    st.success("Local salvo.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Falha ao salvar local: {e}")

        st.markdown("---")
        st.subheader("🧨 Perigo — Banco de Dados")

        st.caption("Isso apaga o HISTÓRICO (coleção history) desta Mary para este usuário. Não apaga coleções inteiras.")
        confirmar = st.checkbox("Confirmo que quero apagar TODO o histórico do BD desta Mary", value=False)
        if st.button("APAGAR HISTÓRICO DO BD (Mary)"):
            if not confirmar:
                st.error("Marque a confirmação primeiro.")
            else:
                try:
                    n = _apagar_hist_bd_novo_e_legado()
                    _invalidate_backend_cache()
                    st.session_state["chat_history"] = []
                    st.session_state["mary_intro_done"] = False
                    st.success(f"Histórico apagado do BD: {n} registros.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Falha ao apagar histórico do BD: {e}")

        st.markdown("---")
        st.subheader("🧾 Histórico visual (performance)")
        st.caption("Se a página travar, reduza o limite visual.")
        st.session_state["visual_limit"] = st.slider(
            "Limite de mensagens na tela",
            min_value=20,
            max_value=250,
            value=int(st.session_state.get("visual_limit", DEFAULT_VISUAL_LIMIT)),
            step=10,
        )

    # ========= BOOT =========
    if not st.session_state["chat_history"]:
        backend_hist = _carregar_chat_visual_do_backend(force=False)

        if backend_hist:
            st.session_state["chat_history"] = backend_hist
            st.session_state["mary_intro_done"] = True
        else:
            if not st.session_state.get("mary_intro_done", False):
                _colar_fala_inicial_na_tela()

    # ========= RENDER HISTÓRICO =========
    hist = st.session_state.get("chat_history", [])
    visual_limit = int(st.session_state.get("visual_limit", DEFAULT_VISUAL_LIMIT))
    visible = hist[-visual_limit:] if len(hist) > visual_limit else hist

    for role, content in visible:
        with st.chat_message(role):
            st.markdown(content)

    # ========= INPUT =========
    prompt = st.chat_input("Fala algo pra Mary...")
    if prompt:
        st.session_state["chat_history"].append(("user", prompt))
        with st.chat_message("user"):
            st.markdown(prompt)

        st.session_state["chat_input"] = prompt
        resposta = svc.reply(
            user=st.session_state.get("user_id", "Janio"),
            model=st.session_state.get("model"),
        )
        st.session_state["chat_input"] = ""

        with st.chat_message("assistant"):
            st.markdown(resposta)

        st.session_state["chat_history"].append(("assistant", resposta))

        _invalidate_backend_cache()

main()
