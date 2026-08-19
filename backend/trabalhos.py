"""Resumos em andamento, para o HTTP não ter que esperar por eles.

Uma aula de duas horas leva minutos: são várias chamadas à Gemini, espaçadas de
propósito para caber no limite por minuto do plano gratuito. Segurar a resposta
HTTP todo esse tempo não funciona — o proxy da hospedagem corta a conexão antes
do fim e o navegador mostra erro mesmo com o resumo terminando aqui dentro.

Então o trabalho roda numa thread e o frontend acompanha por /status. O
andamento é espelhado no Mongo, não só na memória: assim ele sobrevive a um
restart do container (deploy, OOM, health check) e o frontend descobre que o
trabalho morreu em vez de esperar para sempre. Sem banco configurado, vale só o
dicionário local e funciona igual enquanto o processo viver.
"""

import threading
import time

import storage

LIMPAR_APOS = 600  # segundos que um trabalho terminado ainda fica visível

_trabalhos = {}
_trava = threading.Lock()


def _guardar(recording_id, item):
    """Espelha no banco. Falha de rede aqui não pode derrubar o resumo em curso."""
    if not storage.enabled():
        return
    try:
        storage.salvar_trabalho(recording_id, {
            "estado": item["estado"], "etapa": item["etapa"], "erro": item["erro"],
        })
    except storage.StorageError:
        pass


def _descartar_antigos(agora):
    for chave, item in list(_trabalhos.items()):
        if item["estado"] != "rodando" and agora - item["atualizado_em"] > LIMPAR_APOS:
            _trabalhos.pop(chave, None)


def estado(recording_id):
    """Estado local se este processo é quem está tocando; senão, o do banco."""
    with _trava:
        item = _trabalhos.get(recording_id)
        if item:
            return dict(item)
    if not storage.enabled():
        return None
    try:
        salvo = storage.buscar_trabalho(recording_id)
    except storage.StorageError:
        return None
    if not salvo:
        return None
    # "rodando" no banco sem thread aqui = o container caiu no meio do trabalho.
    if salvo.get("estado") == "rodando":
        return {"estado": "parado", "etapa": salvo.get("etapa"), "erro": None}
    return {"estado": salvo.get("estado"), "etapa": salvo.get("etapa"), "erro": salvo.get("erro")}


def rodando(recording_id):
    item = estado(recording_id)
    return bool(item and item["estado"] == "rodando")


def anotar(recording_id, etapa):
    """Etapa atual, para o usuário ver que a coisa anda."""
    with _trava:
        item = _trabalhos.get(recording_id)
        if item:
            item.update(etapa=etapa, atualizado_em=time.time())
            copia = dict(item)
    if item:
        _guardar(recording_id, copia)


def iniciar(app, recording_id, tarefa):
    """Sobe a thread. Se já houver uma rodando para esta gravação, devolve ela.

    `tarefa` recebe uma função para anotar a etapa atual.
    """
    agora = time.time()
    with _trava:
        _descartar_antigos(agora)
        atual = _trabalhos.get(recording_id)
        if atual and atual["estado"] == "rodando":
            return dict(atual)
        _trabalhos[recording_id] = {
            "estado": "rodando", "etapa": "Na fila", "erro": None,
            "iniciado_em": agora, "atualizado_em": agora,
        }
        inicial = dict(_trabalhos[recording_id])
    _guardar(recording_id, inicial)
    if storage.enabled():
        try:
            storage.limpar_trabalhos(LIMPAR_APOS)
        except storage.StorageError:
            pass

    def correr():
        with app.app_context():
            try:
                tarefa(lambda etapa: anotar(recording_id, etapa))
                final = {"estado": "pronto", "etapa": "Pronto", "erro": None}
            except Exception as error:
                app.logger.warning("[trabalho] %s falhou: %s", recording_id, error)
                final = {"estado": "erro", "etapa": "Falhou", "erro": str(error)}
        with _trava:
            item = _trabalhos.get(recording_id)
            if item:
                item.update(final, atualizado_em=time.time())
        _guardar(recording_id, final)

    threading.Thread(target=correr, name=f"resumo-{recording_id[:8]}", daemon=True).start()
    return inicial
