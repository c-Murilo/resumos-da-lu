"""Mantém o token da Plaud vivo entre deploys.

O servidor MCP só sabe ler e escrever `~/.plaud/tokens-mcp.json`. Num container
efêmero esse arquivo nasce vazio e morre no redeploy, então aqui ele é
espelhado no Supabase: restaurado ao subir, e regravado sempre que o MCP o
renova (o refresh token pode rotacionar, e perder a rotação quebra o login).
"""

import json
import os
from pathlib import Path

import storage

TOKEN_PATH = Path(os.getenv("PLAUD_TOKEN_PATH", Path.home() / ".plaud" / "tokens-mcp.json"))
_ultimo_visto = None


def _ler_arquivo():
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def restaurar():
    """Escreve no disco o token guardado no banco. Chamado uma vez, ao subir."""
    global _ultimo_visto
    if not storage.enabled():
        return False

    local = _ler_arquivo()
    if local:  # Máquina de desenvolvimento: o disco manda, e o banco acompanha.
        _ultimo_visto = local
        return False

    try:
        salvo = storage.buscar_token()
    except storage.StorageError:
        return False
    if not salvo:
        return False

    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(json.dumps(salvo), encoding="utf-8")
    TOKEN_PATH.chmod(0o600)
    _ultimo_visto = salvo
    return True


def sincronizar():
    """Sobe o token ao banco quando o MCP o renova. Barato: o arquivo tem ~1 KB."""
    global _ultimo_visto
    if not storage.enabled():
        return
    atual = _ler_arquivo()
    if not atual or atual == _ultimo_visto:
        return
    try:
        storage.salvar_token(atual)
        _ultimo_visto = atual
    except storage.StorageError:
        pass  # Falhar aqui só custa uma rodada de sincronização.
