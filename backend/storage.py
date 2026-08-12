"""Persistência no Supabase via PostgREST.

O app funciona sem isso: se SUPABASE_URL/SUPABASE_SERVICE_KEY não estiverem no
.env, todas as funções viram no-op e a aplicação segue sem histórico.
"""

import os

import requests

TIMEOUT = 20


class StorageError(Exception):
    pass


def _config():
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()
    return (url, key) if url and key else (None, None)


def enabled():
    return _config() != (None, None)


def _request(method, path, **kwargs):
    url, key = _config()
    if not url:
        raise StorageError("Supabase não configurado.")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        **kwargs.pop("headers", {}),
    }
    try:
        response = requests.request(method, f"{url}/rest/v1/{path}", headers=headers, timeout=TIMEOUT, **kwargs)
        response.raise_for_status()
    except requests.RequestException as error:
        detail = getattr(error.response, "text", "") if getattr(error, "response", None) is not None else ""
        raise StorageError(f"Supabase: {error} {detail[:200]}") from error
    return response.json() if response.content else []


def salvar_resumo(recording_id, dados):
    """Upsert por recording_id: reprocessar uma aula substitui o material anterior."""
    payload = {"recording_id": recording_id, **dados}
    rows = _request(
        "POST",
        "resumos?on_conflict=recording_id",
        json=payload,
        headers={"Prefer": "resolution=merge-duplicates,return=representation"},
    )
    return rows[0] if rows else None


def buscar_resumo(recording_id):
    rows = _request("GET", f"resumos?recording_id=eq.{recording_id}&select=*&limit=1")
    return rows[0] if rows else None


def listar_resumos():
    """Só os campos que a lista precisa — evita trafegar transcrição inteira."""
    campos = "recording_id,nome,modelo,gravado_em,duracao_ms,criado_em"
    return _request("GET", f"resumos?select={campos}&order=criado_em.desc")


def salvar_pergunta(resumo_id, pergunta, resposta, modelo):
    _request("POST", "perguntas", json={
        "resumo_id": resumo_id,
        "pergunta": pergunta,
        "resposta": resposta,
        "modelo": modelo,
    })


def buscar_config(chave):
    rows = _request("GET", f"config?chave=eq.{chave}&select=valor&limit=1")
    return rows[0]["valor"] if rows else None


def salvar_config(chave, valor):
    _request(
        "POST",
        "config?on_conflict=chave",
        json={"chave": chave, "valor": valor},
        headers={"Prefer": "resolution=merge-duplicates"},
    )


def buscar_token():
    rows = _request("GET", "plaud_token?id=eq.unico&select=conteudo&limit=1")
    return rows[0]["conteudo"] if rows else None


def salvar_token(conteudo):
    _request(
        "POST",
        "plaud_token?on_conflict=id",
        json={"id": "unico", "conteudo": conteudo},
        headers={"Prefer": "resolution=merge-duplicates"},
    )


def listar_perguntas(resumo_id):
    return _request("GET", f"perguntas?resumo_id=eq.{resumo_id}&select=pergunta,resposta&order=criado_em")
