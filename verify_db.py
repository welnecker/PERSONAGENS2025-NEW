# verify_db.py
import sys
import os

# Ajusta path
sys.path.append(os.getcwd())

from core.database import get_col, _ensure_mongo, db_status, ping_db
from core.repositories import ensure_indexes

print("--- Inicializando DB ---")
_ensure_mongo()

print(f"--- DB Status: {db_status()} ---")

ping = ping_db()
print(f"--- Ping: {ping} ---")

print("--- Garantindo Índices ---")
ensure_indexes()

try:
    print("\n--- Verificando Índices (apenas se Mongo estiver ativo) ---")
    if ping[0] == "mongo" and ping[1]:
        h_idx = list(get_col("history")._col.list_indexes())
        print("History Indexes:", [list(i['key'].keys()) for i in h_idx])
        
        s_idx = list(get_col("state_data")._col.list_indexes())
        print("State Indexes:", [list(i['key'].keys()) for i in s_idx])
        
        e_idx = list(get_col("events")._col.list_indexes())
        print("Events Indexes:", [list(i['key'].keys()) for i in e_idx])
    else:
        print("Mongo não conectado ou falhou no ping. Pulando verificação de índices reais.")
except Exception as e:
    print(f"Erro ao listar índices: {e}")

print("\n--- Concluído ---")
