"""Resume sozinho as gravações novas.

Regra central: "nova" é toda gravação criada **depois** do marco registrado na
primeira execução. Sem isso, ligar o recurso dispararia o acervo inteiro de uma
vez — caro em cota e provavelmente indesejado.
"""

import threading
import time

import storage

CHAVE_MARCO = "auto_resumo_marco"
MAX_TENTATIVAS = 3

_falhas = {}
_iniciado = False


def _log(app, nivel, mensagem, *args):
    getattr(app.logger, nivel)("[auto-resumo] " + mensagem, *args)


def _criado_em(gravacao):
    return (gravacao.get("created_at") or gravacao.get("start_at") or "").strip()


def _marco(app, gravacoes):
    """Na primeira vez, fixa o marco na gravação mais recente e não processa nada."""
    try:
        salvo = storage.buscar_config(CHAVE_MARCO)
    except storage.StorageError as error:
        _log(app, "warning", "não consegui ler o marco: %s", error)
        return None
    if salvo:
        return salvo

    inicial = max((_criado_em(item) for item in gravacoes), default="")
    try:
        storage.salvar_config(CHAVE_MARCO, inicial)
        _log(app, "info", "marco inicial em %s; o acervo anterior fica de fora", inicial or "vazio")
    except storage.StorageError as error:
        _log(app, "warning", "não consegui gravar o marco: %s", error)
    return None


def _pendentes(app, modulo):
    payload = modulo.extract_file_payload(modulo.plaud_call("list_files", {}))
    gravacoes = payload["data"] if isinstance(payload, dict) else payload
    if not isinstance(gravacoes, list):
        return []

    marco = _marco(app, gravacoes)
    if marco is None:
        return []

    novas = [item for item in gravacoes if _criado_em(item) > marco and item.get("id")]
    novas = [item for item in novas if _falhas.get(item["id"], 0) < MAX_TENTATIVAS]
    # Mais antigas primeiro: a ordem de chegada é a ordem que faz sentido estudar.
    return sorted(novas, key=_criado_em)


def _processar(app, modulo, gravacao):
    recording_id = gravacao["id"]
    if storage.buscar_resumo(recording_id):
        return True

    modelo = modulo.resolve_model(None)
    info = modulo.extract_file_payload(modulo.plaud_call("get_file", {"file_id": recording_id}))
    resultado = modulo.process_audio(info, modelo)
    try:
        escolhido = storage.listar_nomes().get(recording_id)
    except storage.StorageError:
        escolhido = None
    storage.salvar_resumo(recording_id, {
        "nome": escolhido or info.get("name"),
        "modelo": modelo,
        "gravado_em": info.get("start_at"),
        "duracao_ms": info.get("duration"),
        "transcricao": resultado.get("transcricao"),
        "resumo": resultado.get("resumo"),
        "acoes": resultado.get("acoes") or [],
        "estudo": resultado.get("estudo"),
        "slides": resultado.get("slides") or [],
    })
    return True


def rodada(app, modulo):
    """Uma varredura. Devolve quantas gravações foram resumidas."""
    if not storage.enabled():
        return 0

    with app.app_context():
        try:
            pendentes = _pendentes(app, modulo)
        except Exception as error:
            _log(app, "warning", "falha ao listar gravações: %s", error)
            return 0

        feitas = 0
        for gravacao in pendentes:
            recording_id = gravacao["id"]
            nome = (gravacao.get("name") or recording_id)[:60]
            try:
                _log(app, "info", "resumindo %s", nome)
                _processar(app, modulo, gravacao)
                _falhas.pop(recording_id, None)
                feitas += 1
                # O marco avança só com sucesso: uma falha não pula a gravação.
                storage.salvar_config(CHAVE_MARCO, _criado_em(gravacao))
            except Exception as error:
                _falhas[recording_id] = _falhas.get(recording_id, 0) + 1
                restantes = MAX_TENTATIVAS - _falhas[recording_id]
                _log(app, "warning", "falhou em %s (%s). Tentativas restantes: %s", nome, error, restantes)
                break  # Erro costuma ser cota ou 503: insistir agora só piora.
        return feitas


def iniciar(app, modulo, intervalo):
    """Sobe a thread de fundo. Roda uma vez por processo."""
    global _iniciado
    if _iniciado or not storage.enabled():
        return
    _iniciado = True

    def laco():
        while True:
            try:
                rodada(app, modulo)
            except Exception as error:
                _log(app, "warning", "rodada abortada: %s", error)
            time.sleep(intervalo)

    threading.Thread(target=laco, name="auto-resumo", daemon=True).start()
    _log(app, "info", "ligado, varrendo a cada %ss", intervalo)
