"""Persistência no MongoDB Atlas.

O app funciona sem isso: se MONGODB_URI não estiver no .env, todas as funções
viram no-op e a aplicação segue sem histórico.

Coleções:
- resumos    — uma por gravação, chaveada por recording_id
- perguntas  — histórico do chat, ligado pelo recording_id
- config     — ajustes internos e o token da Plaud, chaveados por _id
- nomes      — títulos escolhidos à mão, inclusive de gravação ainda sem resumo
"""

import os
import threading

TIMEOUT_MS = 20000
_cliente = None
_trava = threading.Lock()


class StorageError(Exception):
    pass


def enabled():
    return bool(os.getenv("MONGODB_URI", "").strip())


def _db():
    """Cliente único por processo: abrir conexão por chamada é caro no Atlas."""
    global _cliente
    if not enabled():
        raise StorageError("MongoDB não configurado.")

    with _trava:
        if _cliente is None:
            try:
                from pymongo import ASCENDING, MongoClient
            except ImportError as error:
                raise StorageError("pymongo não instalado: pip install -r requirements.txt") from error

            try:
                _cliente = MongoClient(
                    os.getenv("MONGODB_URI").strip(),
                    serverSelectionTimeoutMS=TIMEOUT_MS,
                    connectTimeoutMS=TIMEOUT_MS,
                    appname="resumos-da-lu",
                )
                base = _cliente[os.getenv("MONGODB_DB", "resumos_da_lu").strip()]
                base.resumos.create_index([("recording_id", ASCENDING)], unique=True)
                base.perguntas.create_index([("recording_id", ASCENDING), ("criado_em", ASCENDING)])
            except Exception as error:
                _cliente = None
                raise StorageError(f"MongoDB: {error}") from error

    return _cliente[os.getenv("MONGODB_DB", "resumos_da_lu").strip()]


def _agora():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _limpar(documento):
    """O _id do Mongo não é serializável em JSON e não interessa ao frontend."""
    if documento:
        documento.pop("_id", None)
    return documento


def salvar_resumo(recording_id, dados):
    """Upsert: reprocessar uma aula substitui o material anterior."""
    try:
        _db().resumos.update_one(
            {"recording_id": recording_id},
            {"$set": {**dados, "recording_id": recording_id, "atualizado_em": _agora()},
             "$setOnInsert": {"criado_em": _agora()}},
            upsert=True,
        )
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error
    return {"id": recording_id}


def atualizar_resumo(recording_id, campos):
    """Edições feitas à mão (nome, anotações). Devolve False se não houver resumo."""
    try:
        resultado = _db().resumos.update_one(
            {"recording_id": recording_id},
            {"$set": {**campos, "atualizado_em": _agora()}},
        )
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error
    return resultado.matched_count > 0


def salvar_nome(recording_id, nome):
    """Título dado pela usuária. Vale mesmo antes de a aula ser resumida."""
    try:
        _db().nomes.update_one(
            {"_id": recording_id},
            {"$set": {"nome": nome, "atualizado_em": _agora()}},
            upsert=True,
        )
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error


def listar_nomes():
    """Mapa recording_id -> nome, para renomear a lista que vem da Plaud."""
    try:
        return {item["_id"]: item["nome"] for item in _db().nomes.find({})}
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error


def apagar_resumo(recording_id):
    """Apaga o resumo e o histórico de perguntas dele. False se não existia."""
    try:
        resultado = _db().resumos.delete_one({"recording_id": recording_id})
        _db().perguntas.delete_many({"recording_id": recording_id})
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error
    return resultado.deleted_count > 0


def buscar_resumo(recording_id):
    try:
        return _limpar(_db().resumos.find_one({"recording_id": recording_id}))
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error


def listar_resumos():
    """Só os campos da lista — evita trafegar transcrição e material inteiros."""
    campos = {"recording_id": 1, "nome": 1, "modelo": 1, "gravado_em": 1, "duracao_ms": 1, "criado_em": 1}
    try:
        return [_limpar(item) for item in _db().resumos.find({}, campos).sort("criado_em", -1)]
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error


def salvar_pergunta(recording_id, pergunta, resposta, modelo):
    try:
        _db().perguntas.insert_one({
            "recording_id": recording_id,
            "pergunta": pergunta,
            "resposta": resposta,
            "modelo": modelo,
            "criado_em": _agora(),
        })
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error


def listar_perguntas(recording_id):
    campos = {"pergunta": 1, "resposta": 1}
    try:
        cursor = _db().perguntas.find({"recording_id": recording_id}, campos).sort("criado_em", 1)
        return [_limpar(item) for item in cursor]
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error


def buscar_config(chave):
    try:
        documento = _db().config.find_one({"_id": chave})
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error
    return documento.get("valor") if documento else None


def salvar_config(chave, valor):
    try:
        _db().config.update_one(
            {"_id": chave},
            {"$set": {"valor": valor, "atualizado_em": _agora()}},
            upsert=True,
        )
    except StorageError:
        raise
    except Exception as error:
        raise StorageError(f"MongoDB: {error}") from error


def buscar_token():
    return buscar_config("plaud_token")


def salvar_token(conteudo):
    salvar_config("plaud_token", conteudo)
