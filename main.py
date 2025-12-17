# main.py – Página inicial para seleção de personagens
from __future__ import annotations
import streamlit as st

st.set_page_config(
    page_title="PERSONAGENS 2025",
    page_icon="🎭",
    layout="centered",
)

# ==== BLOQUEIO POR SENHA PARA O MARY_APP ====

SENHA_CORRETA = "311071"   # ← coloque aqui a senha que quiser

def check_password():
    """Exibe um campo de senha e barra acesso se estiver incorreto."""
    if "senha_ok" not in st.session_state:
        st.session_state["senha_ok"] = False

    # Se ainda não validou a senha, mostra a caixa
    if not st.session_state["senha_ok"]:
        st.title("🔐 Mary – Acesso Restrito")

        senha = st.text_input("Digite a senha de acesso:", type="password")

        if st.button("Entrar"):
            if senha == SENHA_CORRETA:
                st.session_state["senha_ok"] = True
                st.success("Acesso liberado!")
                st.rerun()
            else:
                st.error("Senha incorreta. Tente novamente.")

        # Impede que o app abaixo carregue
        return False

    return True


# ---- BLOQUEIA EXECUÇÃO DO APP SE A SENHA NÃO FOR VALIDADA ----
if not check_password():
    st.stop()

st.title("PERSONAGENS 2025 – Escolha da Personagem 🎭")

st.markdown("""
Bem-vindo, Janio!  
Selecione abaixo qual personagem você deseja acessar.
Cada personagem abre **seu aplicativo próprio**, com sidebar, ajustes e lógica completamente independentes.
""")

st.markdown("---")

# IMPORTANTE: aqui vai o caminho relativo a partir de main.py
personagens = {
    "Mary – Esposa Cúmplice": "pages/mary_app.py",
    # "Laura – Femme Fatale": "pages/laura_app.py",
    # "Nerith – A Elfa Azul": "pages/nerith_app.py",
}

# Garante índices de performance no banco (safe-fail)
try:
    import core.repositories
    core.repositories.ensure_indexes()
except Exception:
    pass

choice = st.selectbox(
    "Escolha uma personagem:",
    list(personagens.keys())
)

st.markdown("---")

if st.button("Entrar na Personagem 🚪"):
    script = personagens[choice]
    try:
        st.switch_page(script)
    except Exception as e:
        st.error(
            f"Erro ao abrir {choice}. "
            f"Verifique se **{script}** existe.\n\n"
            f"Detalhe técnico: {e}"
        )
