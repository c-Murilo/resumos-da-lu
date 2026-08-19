import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from google import genai
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from werkzeug.exceptions import HTTPException

import audio
import plaud_token
import storage
import trabalhos

load_dotenv()

app = Flask(__name__, static_folder="static", static_url_path="")
# Em dev o Vite troca de porta quando a 5173 está ocupada, então libera qualquer
# porta local. Em produção, defina FRONTEND_ORIGIN com o domínio real.
LOCAL_ORIGINS = [r"http://localhost:\d+", r"http://127\.0\.0\.1:\d+"]
CORS(app, resources={r"/api/*": {
    "origins": [item.strip() for item in os.getenv("FRONTEND_ORIGIN", "").split(",") if item.strip()] or LOCAL_ORIGINS
}})


class ServiceError(Exception):
    pass


def _mcp_command():
    command = os.getenv("PLAUD_MCP_COMMAND", "npx")
    args = os.getenv("PLAUD_MCP_ARGS", "-y @plaud-ai/mcp@latest").split()

    # `npx -y pacote@latest` consulta o registro do npm a cada chamada, o que no
    # servidor estoura o timeout do balanceador. Se o pacote já estiver instalado
    # na imagem, o binário local faz a mesma coisa em milissegundos.
    if command == "npx":
        binario = shutil.which("plaud-mcp")
        if binario:
            command, args = binario, []

    return StdioServerParameters(command=command, args=args)


async def _call_plaud(tool_name, arguments=None):
    """Abre uma sessão MCP curta, evitando manter tokens/sockets no processo web."""
    async with stdio_client(_mcp_command()) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments or {})
            if result.isError:
                raise ServiceError("Plaud retornou um erro. Confirme o login no MCP.")
            text_parts = [item.text for item in result.content if hasattr(item, "text")]
            payload = "\n".join(text_parts)
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                return payload


def _causa(error):
    """O MCP embrulha tudo em ExceptionGroup, cuja mensagem não diz nada útil."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return f"{type(error).__name__}: {error}"


def plaud_call(tool_name, arguments=None):
    plaud_token.garantir()
    try:
        return asyncio.run(_call_plaud(tool_name, arguments))
    except ServiceError:
        raise
    except Exception as error:
        raise ServiceError(f"Não foi possível conectar à Plaud MCP: {_causa(error)}") from error
    finally:
        # O MCP pode ter renovado (e rotacionado) o token durante a chamada.
        plaud_token.sincronizar()


def load_system_prompt(env_var="PROMPT_FILE", default="prompt.md"):
    """Instruções editáveis. Lidas a cada chamada para permitir ajuste sem restart."""
    prompt_file = Path(__file__).parent / os.getenv(env_var, default)
    try:
        return prompt_file.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ServiceError(f"Não foi possível ler o prompt em {prompt_file}: {error}") from error


def extract_file_payload(payload):
    if isinstance(payload, dict):
        return payload.get("data", payload)
    raise ServiceError("A resposta da Plaud não está no formato esperado.")


def gemini_client():
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key or api_key == "cole_sua_chave_aqui":
        raise ServiceError("Defina GEMINI_API_KEY no arquivo backend/.env.")
    return genai.Client(api_key=api_key)


# Só entra em cena se a listagem ao vivo falhar (chave ausente, rede fora).
FALLBACK_MODELS = "gemini-3.6-flash,gemini-3.5-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash"
MODEL_CACHE_SECONDS = 600
_model_cache = {"at": 0.0, "models": [], "saida": {}, "falhou_em": 0.0}


EXCLUDED_MODEL_TAGS = ("image", "tts", "live", "native-audio", "embedding", "learnlm")


def _model_rank(name):
    """Estável antes de preview, versão maior antes da menor, alias '-latest' por último."""
    version = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return (
        any(tag in name for tag in ("preview", "exp")),
        -float(version.group(1)) if version else 0.0,
        name,
    )


def _live_models():
    """Lista o que a chave realmente enxerga, para não depender de nomes fixos no código."""
    now = time.monotonic()
    if _model_cache["models"] and now - _model_cache["at"] < MODEL_CACHE_SECONDS:
        return _model_cache["models"]
    # Listagem quebrada não é motivo para tentar de novo a cada chamada da Gemini.
    if now - _model_cache["falhou_em"] < MODEL_CACHE_SECONDS:
        return _model_cache["models"]
    try:
        names, saida = [], {}
        client = gemini_client()  # precisa viver enquanto o paginador itera
        for model in client.models.list():
            actions = getattr(model, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            name = (model.name or "").removeprefix("models/")
            # Fora imagem, voz e embedding: aqui só interessa quem lê áudio e devolve texto.
            if name.startswith("gemini-") and not any(tag in name for tag in EXCLUDED_MODEL_TAGS):
                names.append(name)
                # O teto de saída varia por modelo; pedir acima dele é erro 400.
                teto = getattr(model, "output_token_limit", None)
                if isinstance(teto, int) and teto > 0:
                    saida[name] = teto
    except Exception as error:
        app.logger.warning("Não foi possível listar modelos da Gemini: %s", error)
        _model_cache["falhou_em"] = now
        return []
    names.sort(key=_model_rank)
    _model_cache.update(at=now, models=names, saida=saida)
    return names


def available_models():
    """GEMINI_MODELS, quando definido, restringe a lista; senão vale o que a chave oferece."""
    configured = [item.strip() for item in os.getenv("GEMINI_MODELS", "").split(",") if item.strip()]
    models = configured or _live_models() or [item for item in FALLBACK_MODELS.split(",")]
    default = os.getenv("GEMINI_MODEL", "").strip() or (models[0] if models else "gemini-2.5-flash")
    if default not in models:
        models.insert(0, default)
    return models, default


def resolve_model(requested):
    """Só aceita modelo vindo do cliente se estiver na allowlist do .env."""
    models, default = available_models()
    requested = (requested or "").strip()
    if not requested:
        return default
    if requested not in models:
        raise ServiceError(f"Modelo não permitido: {requested}.")
    return requested


def _anotar(sessao, texto):
    """Etapa atual: vai para o log e para quem estiver acompanhando por /status."""
    app.logger.info("%s", texto)
    registrar = (sessao or {}).get("anotar")
    if callable(registrar):
        registrar(texto)


def _hit_output_limit(generated):
    """Transcrição + material de estudo numa resposta só pode estourar o teto de saída."""
    for candidate in getattr(generated, "candidates", None) or []:
        if str(getattr(candidate, "finish_reason", "")).endswith("MAX_TOKENS"):
            return True
    return False


def _generation_config(**extra):
    """Sem max_output_tokens aqui: ele depende do modelo e entra em _com_saida()."""
    return dict(extra)


def _teto_desejado():
    limite = os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "").strip()
    return int(limite) if limite.isdigit() and int(limite) > 0 else 0


def _com_saida(config, modelo):
    """Aplica GEMINI_MAX_OUTPUT_TOKENS sem passar do teto que o modelo aceita.

    Pedir mais do que o modelo suporta vira 400 INVALID_ARGUMENT, que não é erro
    temporário e derrubaria a aula. Quando o teto do modelo é desconhecido
    (listagem indisponível), vale o padrão da API.
    """
    desejado = _teto_desejado()
    if not desejado:
        return config
    _live_models()  # popula o cache de tetos, com validade de 10 min
    teto = _model_cache["saida"].get(modelo)
    if not teto:
        return config
    return {**config, "max_output_tokens": min(desejado, teto)}


TENTATIVAS_POR_MODELO = 3
ESPERA_ENTRE_TENTATIVAS = 4  # segundos, dobrando a cada falha
ERROS_TEMPORARIOS = ("503", "429", "500", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded")
MARCAS_COTA_DIARIA = ("perday", "per day", "requests per day", "rpd")

# No plano gratuito o gargalo é requisição por minuto (5 na maioria dos flash) e
# por dia. Uma aula longa dispara várias chamadas seguidas, então elas saem
# espaçadas em vez de tomarem 429 em série.
_ultima_chamada = {}
_ritmo = threading.Lock()


def _aguardar_vez(modelo):
    """Espaça as chamadas de um mesmo modelo para caber no limite por minuto."""
    rpm = os.getenv("GEMINI_RPM", "5").strip()
    intervalo = 60.0 / (int(rpm) if rpm.isdigit() and int(rpm) > 0 else 5)
    with _ritmo:  # o limite é da chave inteira, não desta thread
        falta = _ultima_chamada.get(modelo, 0.0) + intervalo - time.monotonic()
        if falta > 0:
            time.sleep(falta)
        _ultima_chamada[modelo] = time.monotonic()


def _cota_diaria(error):
    """429 de cota do dia: insistir no mesmo modelo não adianta, só queima tempo."""
    texto = str(error).lower()
    if "429" not in texto and "resource_exhausted" not in texto:
        return False
    return any(marca in texto for marca in MARCAS_COTA_DIARIA)


def _espera_pedida(error):
    """A própria Gemini diz quanto esperar em retryDelay; melhor que chutar."""
    achado = re.search(r"retry.?delay[\'\"]?[:=]\s*[\'\"]?(\d+)", str(error), re.IGNORECASE)
    return min(int(achado.group(1)), 60) if achado else 0


def _cadeia_de_modelos(preferido, sessao=None):
    """Modelo escolhido primeiro; depois os reservas de GEMINI_FALLBACK.

    Dentro de uma mesma aula o material sai em várias chamadas (um trecho por
    vez). Se a primeira caiu para o reserva, as seguintes começam por ele: uma
    aula meio escrita por um modelo e meio por outro sai desencontrada no tom.
    """
    escolhido = (sessao or {}).get("modelo") or preferido
    reservas = os.getenv("GEMINI_FALLBACK", "gemini-3.6-flash").split(",")
    cadeia = [escolhido, preferido] + [item.strip() for item in reservas if item.strip()]
    return list(dict.fromkeys(item for item in cadeia if item))  # sem repetir, na ordem


def _temporario(error):
    texto = str(error)
    return any(marca in texto for marca in ERROS_TEMPORARIOS)


def gerar(client, modelo, contents, config, tarefa, sessao=None):
    """Tenta 3 vezes no modelo pedido e, se ele estiver fora, cai para o reserva."""
    ultimo, sem_cota = None, []
    for atual in _cadeia_de_modelos(modelo, sessao):
        for tentativa in range(1, TENTATIVAS_POR_MODELO + 1):
            try:
                _aguardar_vez(atual)
                resposta = client.models.generate_content(
                    model=atual, contents=contents, config=_com_saida(config, atual)
                )
                if sessao is not None and sessao.get("modelo") != atual:
                    if sessao.get("modelo"):
                        app.logger.warning("Aula seguindo em %s no lugar de %s", atual, sessao["modelo"])
                    sessao["modelo"] = atual
                return resposta
            except Exception as error:
                ultimo = error
                if not _temporario(error):
                    raise ServiceError(f"Falha ao {tarefa}: {error}") from error
                if _cota_diaria(error):
                    # Cota do dia estourada: esperar não devolve requisição nenhuma.
                    app.logger.warning("%s: cota diária de %s no fim, indo para o próximo", tarefa, atual)
                    sem_cota.append(atual)
                    break
                app.logger.warning(
                    "%s: %s falhou (tentativa %s/%s): %s",
                    tarefa, atual, tentativa, TENTATIVAS_POR_MODELO, str(error)[:120],
                )
                if tentativa < TENTATIVAS_POR_MODELO:
                    time.sleep(_espera_pedida(error) or ESPERA_ENTRE_TENTATIVAS * tentativa)
        else:
            app.logger.warning("%s: desistindo de %s, tentando o próximo modelo", tarefa, atual)
    if sem_cota:
        raise ServiceError(
            "A cota gratuita de hoje acabou em " + ", ".join(sem_cota) +
            ". O plano gratuito dá poucas requisições por dia em cada modelo, e uma aula "
            "longa gasta várias. Tente amanhã, escolha outro modelo ou use uma chave paga."
        )
    raise ServiceError(f"Falha ao {tarefa}, mesmo trocando de modelo: {ultimo}")


def _transcrever_bloco(client, caminho, model, anterior, parte, total, sessao=None):
    uploaded = client.files.upload(file=caminho)
    try:
        instrucao = "Transcreva esta aula segundo as instruções do sistema."
        if total > 1:
            instrucao = (
                f"Este é o bloco {parte} de {total} da MESMA aula, em sequência.\n"
                "Transcreva apenas o que ouvir neste bloco. Não recomece, não resuma o anterior "
                "e mantenha a mesma identificação de falantes.\n"
            )
            if anterior:
                instrucao += f"\nFim do bloco anterior, só para dar continuidade:\n...{anterior[-600:]}"
        generated = gerar(
            client, model, [uploaded, instrucao],
            _generation_config(system_instruction=load_system_prompt("TRANSCRIPT_PROMPT_FILE", "prompt_transcricao.md")),
            "transcrever o áudio", sessao,
        )
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass

    if _hit_output_limit(generated):
        return None  # veio cortada; quem chamou parte o bloco no meio
    return (generated.text or "").strip()


AUDIO_BLOCO_MINIMO = 5  # minutos; abaixo disso, dividir mais não resolve


def transcribe(client, caminho, model, sessao=None):
    """Etapa 1: transcrição, em blocos quando a aula é longa.

    Se um bloco ainda assim volta cortado no teto de saída (professor que fala
    rápido, aula densa), ele é partido no meio e refeito, em vez de derrubar a
    aula inteira.
    """
    minutos = int(os.getenv("AUDIO_BLOCO_MINUTOS", "40"))
    blocos = audio.dividir(caminho, minutos * 60)
    duracoes = [minutos] * len(blocos)
    if len(blocos) > 1:
        app.logger.info("Áudio dividido em %s blocos de até %s min", len(blocos), minutos)

    partes, criados, indice = [], [b for b in blocos if b != caminho], 0
    try:
        while indice < len(blocos):
            bloco, duracao = blocos[indice], duracoes[indice]
            if len(blocos) > 1:
                _anotar(sessao, f"Transcrevendo o bloco {indice + 1} de {len(blocos)}")
            else:
                _anotar(sessao, "Transcrevendo a aula")
            texto = _transcrever_bloco(
                client, bloco, model, "\n\n".join(partes), indice + 1, len(blocos), sessao
            )
            if texto is None:
                if duracao <= AUDIO_BLOCO_MINIMO:
                    raise ServiceError(
                        "A transcrição foi cortada no limite de saída mesmo em blocos curtos. "
                        "Reduza AUDIO_BLOCO_MINUTOS ou aumente GEMINI_MAX_OUTPUT_TOKENS."
                    )
                metade = max(AUDIO_BLOCO_MINIMO, duracao // 2)
                menores = audio.dividir(bloco, metade * 60, destino=Path(bloco).parent)
                if len(menores) <= 1:
                    raise ServiceError(
                        "A transcrição foi cortada no limite de saída e o bloco não pôde "
                        "ser dividido. Reduza AUDIO_BLOCO_MINUTOS."
                    )
                app.logger.warning("Bloco %s veio cortado; dividindo em %s", indice + 1, len(menores))
                criados += menores
                blocos[indice:indice + 1] = menores
                duracoes[indice:indice + 1] = [metade] * len(menores)
                continue
            if texto:
                partes.append(texto)
            indice += 1
    finally:
        for bloco in criados:
            Path(bloco).unlink(missing_ok=True)

    transcript = "\n\n".join(partes).strip()
    if not transcript:
        raise ServiceError("A Gemini não devolveu transcrição para este áudio.")
    return transcript


# Uma aula de ~2 h vira uma transcrição que não cabe num material de estudo só:
# a Gemini corta a resposta no teto de saída e o resumo simplesmente não sai.
# A partir deste tamanho o texto é estudado em partes e depois costurado.
# Cada trecho é uma requisição, e no plano gratuito requisição é o recurso caro.
# Então o padrão é grande de propósito: a aula inteira numa chamada só. Se a
# resposta vier cortada, _estudar_trecho parte no meio e refaz — o custo extra
# só aparece quando realmente precisa.
ESTUDO_BLOCO_CARACTERES = 200000
ESTUDO_BLOCO_MINIMO = 6000  # abaixo disso, dividir mais não resolve


def _limite_do_bloco():
    valor = os.getenv("ESTUDO_BLOCO_CARACTERES", "").strip()
    return int(valor) if valor.isdigit() and int(valor) > 0 else ESTUDO_BLOCO_CARACTERES


def _partir_texto(texto, pedacos):
    """Divide em N partes de tamanho parecido, quebrando entre parágrafos."""
    if pedacos <= 1:
        return [texto]
    alvo = len(texto) / pedacos
    partes, atual = [], []
    for paragrafo in texto.split("\n\n"):
        atual.append(paragrafo)
        if len(partes) < pedacos - 1 and sum(len(item) + 2 for item in atual) >= alvo:
            partes.append("\n\n".join(atual))
            atual = []
    if atual:
        partes.append("\n\n".join(atual))
    return partes or [texto]


def _json_gerado(generated):
    """JSON da resposta, ou None se ela voltou cortada ou inválida."""
    if _hit_output_limit(generated):
        return None
    try:
        return json.loads(generated.text or "")
    except (json.JSONDecodeError, TypeError):
        return None


def _estudar_trecho(client, trecho, model, rotulo, sessao=None):
    """Material de estudo de um trecho. Se a resposta vier cortada, parte no meio.

    Devolve uma lista porque um trecho teimoso vira dois — o chamador só junta.
    """
    unico = not rotulo
    campos = ('{"resumo": "string", "acoes": ["string"], "estudo": "string em Markdown",\n'
              ' "slides": [{"titulo": "string", "topicos": ["string"], "nota": "string"}]}'
              if unico else
              '{"resumo": "string", "acoes": ["string"], "estudo": "string em Markdown"}')
    contexto = ""
    if rotulo:
        contexto = (
            f"Este é o trecho {rotulo} da MESMA aula, em sequência.\n"
            "Trabalhe só o conteúdo deste trecho: não recapitule o que veio antes, não\n"
            "escreva introdução nem conclusão da aula inteira e não invente o que ainda\n"
            "não foi dito. Não escreva o cabeçalho `# Tema da aula` nem a linha de\n"
            "metadados: comece direto nas seções `##`.\n\n"
        )
    prompt = f"""{contexto}TRANSCRIÇÃO DA AULA:
{trecho}

Retorne APENAS JSON válido com estas chaves:
{campos}
Siga as instruções do sistema para o conteúdo de cada campo."""
    generated = gerar(
        client, model, prompt,
        _generation_config(
            system_instruction=load_system_prompt(),
            response_mime_type="application/json",
        ),
        "gerar o material de estudo", sessao,
    )

    dados = _json_gerado(generated)
    if dados is not None:
        return [dados]
    if len(trecho) <= ESTUDO_BLOCO_MINIMO:
        raise ServiceError(
            "O material de estudo foi cortado no limite de saída mesmo em trechos curtos. "
            "Reduza ESTUDO_BLOCO_CARACTERES ou peça um resumo mais enxuto em prompt.md."
        )

    app.logger.warning("Trecho %s estourou a saída; dividindo em dois", rotulo or "único")
    metades = _partir_texto(trecho, 2)
    resultado = []
    for indice, metade in enumerate(metades, start=1):
        resultado += _estudar_trecho(client, metade, model, f"{rotulo or 'único'}.{indice}", sessao)
    return resultado


def _juntar_estudo(partes):
    blocos = [str(parte.get("estudo") or "").strip() for parte in partes]
    return "\n\n---\n\n".join(bloco for bloco in blocos if bloco)


def _juntar_acoes(partes):
    acoes, vistos = [], set()
    for parte in partes:
        for acao in parte.get("acoes") or []:
            texto = str(acao).strip()
            if texto and texto.lower() not in vistos:
                vistos.add(texto.lower())
                acoes.append(texto)
    return acoes


def _fechar_material(client, estudo, model, sessao=None):
    """Resumo e slides da aula inteira, a partir do estudo já pronto.

    Entra texto grande, sai texto pequeno — é a única chamada que enxerga a aula
    toda sem risco de estourar a saída.
    """
    _anotar(sessao, "Montando o resumo e os slides")
    prompt = f"""MATERIAL DE ESTUDO JÁ PRONTO DESTA AULA:
{estudo}

Retorne APENAS JSON válido com estas chaves:
{{"resumo": "string",
 "slides": [{{"titulo": "string", "topicos": ["string"], "nota": "string"}}]}}
O `resumo` cobre a aula inteira, não um trecho. Os `slides` seguem as instruções
do sistema e cobrem a aula inteira, na ordem do material acima."""
    generated = gerar(
        client, model, prompt,
        _generation_config(
            system_instruction=load_system_prompt(),
            response_mime_type="application/json",
        ),
        "montar o resumo e os slides", sessao,
    )
    return _json_gerado(generated) or {}


def build_study(client, transcript, model, sessao=None):
    """Etapa 2: material de estudo a partir do texto — sem áudio, muito mais barato."""
    limite = _limite_do_bloco()
    pedacos = max(1, -(-len(transcript) // limite))
    trechos = _partir_texto(transcript, pedacos)
    if len(trechos) > 1:
        app.logger.info("Transcrição de %s caracteres estudada em %s trechos", len(transcript), len(trechos))

    partes = []
    for indice, trecho in enumerate(trechos, start=1):
        rotulo = f"{indice} de {len(trechos)}" if len(trechos) > 1 else ""
        _anotar(sessao, f"Escrevendo o material de estudo{f' ({rotulo})' if rotulo else ''}")
        partes += _estudar_trecho(client, trecho, model, rotulo, sessao)

    estudo = _juntar_estudo(partes)
    acoes = _juntar_acoes(partes)
    if len(partes) == 1:
        parte = partes[0]
        fechamento = parte if parte.get("slides") else _fechar_material(client, estudo, model, sessao)
    else:
        fechamento = _fechar_material(client, estudo, model, sessao)

    resumo = str(fechamento.get("resumo") or "").strip()
    if not resumo:
        # O fechamento falhou: os resumos dos trechos, em sequência, servem.
        resumo = " ".join(str(parte.get("resumo") or "").strip() for parte in partes).strip()
    if not estudo and not resumo:
        raise ServiceError("Gemini não retornou o material de estudo. Tente novamente.")

    return {
        "resumo": resumo,
        "acoes": acoes,
        "estudo": estudo,
        "slides": fechamento.get("slides") or [],
    }


# Uma aula por vez no processo inteiro. Duas em paralelo (botão + auto-resumo)
# dobrariam o pico de memória e ainda brigariam pelo limite por minuto da Gemini.
_uma_aula_por_vez = threading.Lock()


def _verificar(client, transcript, material, model, sessao):
    """Confere o material contra a transcrição e devolve o que não se sustenta.

    Custa uma requisição a mais por aula, o que pesa no plano gratuito — daí o
    VERIFICAR_RESUMO, para desligar quando a cota estiver apertada. Nada é
    reescrito automaticamente: em conteúdo clínico, apontar o trecho suspeito é
    mais seguro que trocar uma frase errada por outra igualmente inventada.
    """
    if os.getenv("VERIFICAR_RESUMO", "1").strip() in ("0", "false", "nao", "não"):
        return []

    _anotar(sessao, "Conferindo o resumo contra a transcrição")
    prompt = f"""TRANSCRIÇÃO DA AULA:
{transcript}

MATERIAL DE ESTUDO GERADO A PARTIR DELA:
{json.dumps({k: material.get(k) for k in ("resumo", "acoes", "estudo", "slides")}, ensure_ascii=False)}

Confira o material contra a transcrição segundo as instruções do sistema."""
    try:
        generated = gerar(
            client, model, prompt,
            _generation_config(
                system_instruction=load_system_prompt("CHECK_PROMPT_FILE", "prompt_verificacao.md"),
                response_mime_type="application/json",
            ),
            "conferir o material", sessao,
        )
    except ServiceError as error:
        # Sem cota para conferir não é motivo para perder o resumo já pronto.
        app.logger.warning("Verificação não rodou: %s", error)
        return []

    dados = _json_gerado(generated) or {}
    problemas = [item for item in (dados.get("problemas") or []) if isinstance(item, dict)]
    if problemas:
        app.logger.info("Verificação apontou %s trecho(s) para conferir", len(problemas))
    return problemas


def process_audio(file_info, model, anotar=None, transcricao=None, ao_transcrever=None):
    """Duas requisições: transcrever o áudio, depois estudar o texto."""
    if not _uma_aula_por_vez.acquire(blocking=False):
        _anotar({"anotar": anotar}, "Esperando a aula anterior terminar")
        _uma_aula_por_vez.acquire()
    try:
        return _process_audio(file_info, model, anotar, transcricao, ao_transcrever)
    finally:
        _uma_aula_por_vez.release()


def _process_audio(file_info, model, anotar=None, transcricao=None, ao_transcrever=None):
    client = gemini_client()
    # Um modelo por aula: quem transcreveu é quem estuda, mesmo se houve troca.
    sessao = {"anotar": anotar}

    audio_url = file_info.get("presigned_url")
    if not audio_url:
        raise ServiceError("A Plaud não forneceu uma URL temporária para este áudio.")

    if transcricao:
        # Transcrição já paga numa tentativa anterior: nem baixa o áudio de novo.
        _anotar(sessao, "Aproveitando a transcrição já salva")
        return _com_verificacao(client, transcricao, model, sessao)

    _anotar(sessao, "Baixando o áudio da Plaud")
    suffix = Path(audio_url.split("?", 1)[0]).suffix or ".audio"
    temp_path = None
    try:
        # Em pedaços e direto para o disco: uma aula de duas horas passa de 100 MB,
        # e response.content guardaria tudo isso na RAM de uma vez.
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            temp_path = temp_file.name
            try:
                with requests.get(audio_url, timeout=180, stream=True) as response:
                    response.raise_for_status()
                    for pedaco in response.iter_content(1024 * 1024):
                        temp_file.write(pedaco)
            except requests.RequestException as error:
                raise ServiceError(f"Falha ao baixar o áudio temporário da Plaud: {error}") from error
        # O upload agora acontece por bloco, dentro de transcribe.
        transcript = transcribe(client, temp_path, model, sessao)
    finally:
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)

    if callable(ao_transcrever):
        # Guarda antes de estudar: se o material falhar (cota, por exemplo), a
        # próxima tentativa não gasta outra requisição transcrevendo tudo de novo.
        ao_transcrever(transcript)
    return _com_verificacao(client, transcript, model, sessao)


def _com_verificacao(client, transcript, model, sessao):
    material = build_study(client, transcript, model, sessao)
    return {
        "transcricao": transcript,
        **material,
        "verificacao": _verificar(client, transcript, material, model, sessao),
    }


MAX_HISTORY_TURNS = 12


def answer_question(transcript, question, history, model):
    """Pergunta ancorada na transcrição: sem áudio, sem upload, só texto."""
    client = gemini_client()
    system_prompt = load_system_prompt("ASK_PROMPT_FILE", "prompt_perguntas.md")

    contents = [{"role": "user", "parts": [{"text": f"TRANSCRIÇÃO DA CONVERSA:\n{transcript}"}]},
                {"role": "model", "parts": [{"text": "Li a transcrição. Pode perguntar."}]}]
    for turn in history[-MAX_HISTORY_TURNS:]:
        pergunta = (turn.get("pergunta") or "").strip()
        resposta = (turn.get("resposta") or "").strip()
        if pergunta and resposta:
            contents.append({"role": "user", "parts": [{"text": pergunta}]})
            contents.append({"role": "model", "parts": [{"text": resposta}]})
    contents.append({"role": "user", "parts": [{"text": question}]})

    try:
        generated = client.models.generate_content(
            model=model,
            contents=contents,
            config=_com_saida(_generation_config(system_instruction=system_prompt), model),
        )
    except Exception as error:
        raise ServiceError(f"Falha ao consultar a Gemini: {error}") from error

    resposta = (generated.text or "").strip()
    if not resposta:
        raise ServiceError("A Gemini não retornou resposta. Tente reformular a pergunta.")
    return resposta


@app.get("/api/health")
def health():
    return {"status": "ok"}


MIN_PAGE_SIZE = 10


@app.get("/api/recordings")
def list_recordings():
    filters = {key: request.args[key] for key in ("query", "date_from", "date_to") if request.args.get(key)}
    # O MCP valida tipo: page e page_size são números, e page_size tem mínimo de 10.
    for key, minimum in (("page", 1), ("page_size", MIN_PAGE_SIZE)):
        value = request.args.get(key, "").strip()
        if value.isdigit():
            filters[key] = max(int(value), minimum)
    arquivos = extract_file_payload(plaud_call("list_files", filters))
    return jsonify(_com_nomes_escolhidos(arquivos))


def _com_nomes_escolhidos(arquivos):
    """O título da Plaud é a data da gravação; se ela renomeou, vale o dela."""
    if not storage.enabled() or not isinstance(arquivos, list):
        return arquivos
    try:
        nomes = storage.listar_nomes()
    except storage.StorageError as error:
        app.logger.warning("Falha ao ler os nomes escolhidos: %s", error)
        return arquivos
    for item in arquivos:
        escolhido = nomes.get(item.get("id"))
        if escolhido:
            item["name"] = escolhido
    return arquivos


@app.get("/api/recordings/<recording_id>/transcript")
def get_transcript(recording_id):
    return jsonify(extract_file_payload(plaud_call("get_transcript", {"file_id": recording_id})))


@app.get("/api/models")
def list_models():
    models, default = available_models()
    return jsonify({"models": models, "default": default})


def _resumo_salvo(recording_id):
    if not storage.enabled():
        return None
    try:
        saved = storage.buscar_resumo(recording_id)
    except storage.StorageError as error:
        app.logger.warning("Falha ao ler do MongoDB: %s", error)
        return None
    if not saved or saved.get("parcial"):
        return None  # só a transcrição guardada; ainda não há material para abrir
    try:
        saved["historico"] = storage.listar_perguntas(recording_id)
    except storage.StorageError:
        saved["historico"] = []
    return saved


@app.get("/api/diagnostico")
def diagnostico():
    """Sem segredos: só diz o que está configurado e o que a Plaud responde."""
    import shutil

    comando = os.getenv("PLAUD_MCP_COMMAND", "npx")
    estado = {
        "gemini_key": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "mongodb_configurado": storage.enabled(),
        "mcp_comando": comando,
        "mcp_encontrado": bool(shutil.which(comando)),
        "token_em_disco": plaud_token.TOKEN_PATH.is_file(),
        "token_no_banco": None,
        "plaud": None,
    }

    if storage.enabled():
        try:
            estado["token_no_banco"] = bool(storage.buscar_token())
        except storage.StorageError as error:
            estado["token_no_banco"] = f"erro: {error}"

    try:
        payload = extract_file_payload(plaud_call("list_files", {}))
        dados = payload["data"] if isinstance(payload, dict) else payload
        estado["plaud"] = f"ok, {len(dados)} gravações"
    except Exception as error:
        estado["plaud"] = f"erro: {str(error)[:200]}"

    return jsonify(estado)


@app.get("/api/resumos")
def list_resumos():
    """Biblioteca do que já foi gerado. Independe da Plaud estar respondendo."""
    if not storage.enabled():
        return jsonify([])
    try:
        salvos = storage.listar_resumos()
    except storage.StorageError as error:
        app.logger.warning("Falha ao listar resumos: %s", error)
        return jsonify([])
    # Mesmo formato da lista da Plaud, para o front renderizar o mesmo cartão.
    return jsonify([{
        "id": item["recording_id"],
        "name": item.get("nome"),
        "created_at": item.get("gravado_em") or item.get("criado_em"),
        "duration": item.get("duracao_ms"),
        "modelo": item.get("modelo"),
        "resumido": True,
    } for item in salvos])


@app.get("/api/recordings/<recording_id>/resumo")
def get_resumo(recording_id):
    """Material já gerado, para abrir uma aula antiga sem gastar cota da Gemini."""
    saved = _resumo_salvo(recording_id)
    if not saved:
        return jsonify({"error": "Esta gravação ainda não foi resumida."}), 404
    return jsonify(saved)


@app.patch("/api/recordings/<recording_id>/resumo")
def editar_resumo(recording_id):
    """Renomear o resumo e guardar as anotações feitas à mão sobre ele."""
    if not storage.enabled():
        return jsonify({"error": "Sem MongoDB configurado: não há onde guardar a edição."}), 503

    body = request.get_json(silent=True) or {}
    mudancas = {}
    if "nome" in body:
        nome = (body.get("nome") or "").strip()
        if not nome:
            return jsonify({"error": "O nome não pode ficar vazio."}), 400
        mudancas["nome"] = nome[:200]
        # Guardado à parte porque a aula pode nem ter sido resumida ainda.
        try:
            storage.salvar_nome(recording_id, mudancas["nome"])
        except storage.StorageError as error:
            raise ServiceError(str(error)) from error
    if "anotacoes" in body:
        anotacoes = body.get("anotacoes")
        if not isinstance(anotacoes, list):
            return jsonify({"error": "Anotações em formato inesperado."}), 400
        mudancas["anotacoes"] = anotacoes
    if not mudancas:
        return jsonify({"error": "Nada para alterar."}), 400

    try:
        existe = storage.atualizar_resumo(recording_id, mudancas)
    except storage.StorageError as error:
        raise ServiceError(str(error)) from error
    # Só o nome vive fora do resumo; anotação precisa de um resumo para pendurar.
    if not existe and "anotacoes" in mudancas:
        return jsonify({"error": "Esta gravação ainda não foi resumida."}), 404
    return jsonify({"ok": True, **mudancas})


@app.delete("/api/recordings/<recording_id>/resumo")
def apagar_resumo(recording_id):
    """Tira o resumo da biblioteca. A gravação na Plaud continua intacta."""
    if not storage.enabled():
        return jsonify({"error": "Sem MongoDB configurado: não há nada guardado para apagar."}), 503
    try:
        if not storage.apagar_resumo(recording_id):
            return jsonify({"error": "Esta gravação não tem resumo guardado."}), 404
    except storage.StorageError as error:
        raise ServiceError(str(error)) from error
    return jsonify({"ok": True})


def _transcricao_guardada(recording_id):
    """Transcrição de uma tentativa anterior que não chegou ao fim."""
    if not storage.enabled():
        return None
    try:
        salvo = storage.buscar_resumo(recording_id) or {}
    except storage.StorageError:
        return None
    return (salvo.get("transcricao") or "").strip() or None


def _resumir_e_guardar(recording_id, model, anotar):
    """O trabalho pesado de uma aula, do jeito que a thread de fundo executa."""
    info = extract_file_payload(plaud_call("get_file", {"file_id": recording_id}))

    def guardar_transcricao(texto):
        if not storage.enabled():
            return
        try:
            storage.salvar_resumo(recording_id, {
                "nome": info.get("name"), "modelo": model,
                "gravado_em": info.get("start_at"), "duracao_ms": info.get("duration"),
                "transcricao": texto, "parcial": True,
            })
        except storage.StorageError as error:
            app.logger.warning("Falha ao guardar a transcrição: %s", error)

    resultado = {**process_audio(
        info, model, anotar,
        transcricao=_transcricao_guardada(recording_id),
        ao_transcrever=guardar_transcricao,
    ), "modelo": model}

    if storage.enabled():
        try:
            storage.salvar_resumo(recording_id, {
                "nome": info.get("name"),
                "modelo": model,
                "gravado_em": info.get("start_at"),
                "duracao_ms": info.get("duration"),
                "transcricao": resultado.get("transcricao"),
                "resumo": resultado.get("resumo"),
                "acoes": resultado.get("acoes") or [],
                "estudo": resultado.get("estudo"),
                "slides": resultado.get("slides") or [],
                "verificacao": resultado.get("verificacao") or [],
                "parcial": False,
            })
        except storage.StorageError as error:
            # Perder o histórico é ruim, mas não justifica descartar o resumo pronto.
            app.logger.warning("Falha ao salvar no MongoDB: %s", error)
    return resultado


def _situacao(trabalho):
    return {"estado": trabalho["estado"], "etapa": trabalho["etapa"], "erro": trabalho["erro"]}


@app.post("/api/recordings/<recording_id>/process")
def process_recording(recording_id):
    """Não resume aqui: dispara o trabalho e responde na hora.

    Uma aula longa leva minutos, e o proxy da hospedagem corta a conexão bem
    antes disso. Quem chamou acompanha por /status e busca o material em
    /resumo quando ficar pronto.
    """
    body = request.get_json(silent=True) or {}
    model = resolve_model(body.get("modelo"))

    if not body.get("forcar"):
        saved = _resumo_salvo(recording_id)
        if saved:
            return jsonify(saved)

    if not storage.enabled():
        # Sem banco, o resultado só existe nesta resposta: não dá para soltar.
        return jsonify({**_resumir_e_guardar(recording_id, model, None), "historico": []})

    trabalho = trabalhos.iniciar(
        app, recording_id,
        lambda anotar: _resumir_e_guardar(recording_id, model, anotar),
    )
    return jsonify(_situacao(trabalho)), 202


@app.get("/api/recordings/<recording_id>/status")
def status_recording(recording_id):
    """Andamento de um resumo em curso, para o front mostrar em que pé está."""
    trabalho = trabalhos.estado(recording_id)
    if trabalho:
        return jsonify(_situacao(trabalho))
    # Sem trabalho na memória: ou já terminou faz tempo, ou o servidor reiniciou.
    if _resumo_salvo(recording_id):
        return jsonify({"estado": "pronto", "etapa": "Pronto", "erro": None})
    return jsonify({"estado": "parado", "etapa": None, "erro": None})


@app.post("/api/recordings/<recording_id>/ask")
def ask_recording(recording_id):
    body = request.get_json(silent=True) or {}
    question = (body.get("pergunta") or "").strip()
    transcript = (body.get("transcricao") or "").strip()
    if not question:
        return jsonify({"error": "Escreva uma pergunta."}), 400
    if not transcript:
        transcript = json.dumps(extract_file_payload(plaud_call("get_transcript", {"file_id": recording_id})), ensure_ascii=False)
    history = body.get("historico") or []
    if not isinstance(history, list):
        history = []
    model = resolve_model(body.get("modelo"))
    resposta = answer_question(transcript, question, history, model)

    if storage.enabled():
        try:
            storage.salvar_pergunta(recording_id, question, resposta, model)
        except storage.StorageError as error:
            app.logger.warning("Falha ao salvar a pergunta no MongoDB: %s", error)

    return jsonify({"resposta": resposta, "modelo": model})


@app.errorhandler(ServiceError)
def service_error(error):
    return jsonify({"error": str(error)}), 502


@app.errorhandler(HTTPException)
def http_error(error):
    """Sem isso o handler genérico transformaria todo 404 em 500."""
    return jsonify({"error": error.description}), error.code


@app.errorhandler(Exception)
def unexpected_error(error):
    app.logger.exception(error)
    return jsonify({"error": "Erro inesperado no servidor."}), 500


@app.get("/")
def index():
    return _pagina_do_app()


@app.errorhandler(404)
def rota_desconhecida(error):
    """Rota do React (ou 404 de verdade, se for /api ou não houver build)."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "Rota não encontrada."}), 404
    return _pagina_do_app()


def _pagina_do_app():
    if app.static_folder and (Path(app.static_folder) / "index.html").is_file():
        return app.send_static_file("index.html")
    return jsonify({"error": "Frontend não foi buildado neste ambiente."}), 404


plaud_token.restaurar()

def _ligar_auto_resumo():
    if os.getenv("AUTO_RESUMO", "").strip() != "1":
        return
    # Com `flask --debug` o reloader roda o módulo duas vezes; só o processo
    # filho (WERKZEUG_RUN_MAIN) deve varrer, senão a Plaud é consultada em dobro.
    if app.debug and os.getenv("WERKZEUG_RUN_MAIN") != "true":
        return

    import auto_resumo

    auto_resumo.iniciar(app, sys.modules[__name__], int(os.getenv("AUTO_RESUMO_INTERVALO", "30")))


_ligar_auto_resumo()
