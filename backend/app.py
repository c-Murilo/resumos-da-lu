import asyncio
import json
import os
import re
import sys
import tempfile
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

import plaud_token
import storage

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


def plaud_call(tool_name, arguments=None):
    try:
        return asyncio.run(_call_plaud(tool_name, arguments))
    except ServiceError:
        raise
    except Exception as error:
        raise ServiceError(f"Não foi possível conectar à Plaud MCP: {error}") from error
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
_model_cache = {"at": 0.0, "models": []}


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
    try:
        names = []
        client = gemini_client()  # precisa viver enquanto o paginador itera
        for model in client.models.list():
            actions = getattr(model, "supported_actions", None) or []
            if actions and "generateContent" not in actions:
                continue
            name = (model.name or "").removeprefix("models/")
            # Fora imagem, voz e embedding: aqui só interessa quem lê áudio e devolve texto.
            if name.startswith("gemini-") and not any(tag in name for tag in EXCLUDED_MODEL_TAGS):
                names.append(name)
    except Exception as error:
        app.logger.warning("Não foi possível listar modelos da Gemini: %s", error)
        return []
    names.sort(key=_model_rank)
    _model_cache.update(at=now, models=names)
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


def _hit_output_limit(generated):
    """Transcrição + material de estudo numa resposta só pode estourar o teto de saída."""
    for candidate in getattr(generated, "candidates", None) or []:
        if str(getattr(candidate, "finish_reason", "")).endswith("MAX_TOKENS"):
            return True
    return False


def _generation_config(**extra):
    config = dict(extra)
    limit = os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "").strip()
    if limit.isdigit():
        config["max_output_tokens"] = int(limit)
    return config


def transcribe(client, uploaded, model):
    """Etapa 1: só a transcrição, para o teto de saída ser gasto inteiro com ela."""
    try:
        generated = client.models.generate_content(
            model=model,
            contents=[uploaded, "Transcreva esta aula segundo as instruções do sistema."],
            config=_generation_config(system_instruction=load_system_prompt("TRANSCRIPT_PROMPT_FILE", "prompt_transcricao.md")),
        )
    except Exception as error:
        raise ServiceError(f"Falha ao transcrever o áudio: {error}") from error

    transcript = (generated.text or "").strip()
    if not transcript:
        raise ServiceError("A Gemini não devolveu transcrição para este áudio.")
    if _hit_output_limit(generated):
        raise ServiceError(
            "A aula é longa demais: a transcrição foi cortada no limite de saída. "
            "Aumente GEMINI_MAX_OUTPUT_TOKENS ou grave a aula em partes."
        )
    return transcript


def build_study(client, transcript, model):
    """Etapa 2: material de estudo a partir do texto — sem áudio, muito mais barato."""
    prompt = f"""TRANSCRIÇÃO DA AULA:
{transcript}

Retorne APENAS JSON válido com estas chaves:
{{"resumo": "string",
 "pontos_principais": ["string"],
 "acoes": ["string"],
 "estudo": "string em Markdown",
 "slides": [{{"titulo": "string", "topicos": ["string"], "nota": "string"}}]}}
Siga as instruções do sistema para o conteúdo de cada campo."""
    try:
        generated = client.models.generate_content(
            model=model,
            contents=prompt,
            config=_generation_config(
                system_instruction=load_system_prompt(),
                response_mime_type="application/json",
            ),
        )
    except Exception as error:
        raise ServiceError(f"Falha ao gerar o material de estudo: {error}") from error

    if _hit_output_limit(generated):
        raise ServiceError(
            "O material de estudo foi cortado no limite de saída. "
            "Aumente GEMINI_MAX_OUTPUT_TOKENS ou peça um resumo mais enxuto em prompt.md."
        )
    try:
        return json.loads(generated.text)
    except json.JSONDecodeError as error:
        raise ServiceError("Gemini não retornou o JSON esperado. Tente novamente.") from error


def process_audio(file_info, model):
    """Duas requisições: transcrever o áudio, depois estudar o texto."""
    client = gemini_client()

    audio_url = file_info.get("presigned_url")
    if not audio_url:
        raise ServiceError("A Plaud não forneceu uma URL temporária para este áudio.")

    try:
        response = requests.get(audio_url, timeout=180)
        response.raise_for_status()
    except requests.RequestException as error:
        raise ServiceError(f"Falha ao baixar o áudio temporário da Plaud: {error}") from error

    suffix = Path(audio_url.split("?", 1)[0]).suffix or ".audio"
    temp_path = None
    uploaded = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp_file:
            temp_file.write(response.content)
            temp_path = temp_file.name
        uploaded = client.files.upload(file=temp_path)
        transcript = transcribe(client, uploaded, model)
    finally:
        if uploaded:
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
        if temp_path:
            Path(temp_path).unlink(missing_ok=True)

    return {"transcricao": transcript, **build_study(client, transcript, model)}


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
            config=_generation_config(system_instruction=system_prompt),
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
    payload = plaud_call("list_files", filters)
    return jsonify(extract_file_payload(payload))


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
        app.logger.warning("Falha ao ler do Supabase: %s", error)
        return None
    if not saved:
        return None
    try:
        saved["historico"] = storage.listar_perguntas(saved["id"])
    except storage.StorageError:
        saved["historico"] = []
    return saved


@app.get("/api/recordings/<recording_id>/resumo")
def get_resumo(recording_id):
    """Material já gerado, para abrir uma aula antiga sem gastar cota da Gemini."""
    saved = _resumo_salvo(recording_id)
    if not saved:
        return jsonify({"error": "Esta gravação ainda não foi resumida."}), 404
    return jsonify(saved)


@app.post("/api/recordings/<recording_id>/process")
def process_recording(recording_id):
    body = request.get_json(silent=True) or {}
    model = resolve_model(body.get("modelo"))

    if not body.get("forcar"):
        saved = _resumo_salvo(recording_id)
        if saved:
            return jsonify(saved)

    info = extract_file_payload(plaud_call("get_file", {"file_id": recording_id}))
    resultado = {**process_audio(info, model), "modelo": model}

    if storage.enabled():
        try:
            linha = storage.salvar_resumo(recording_id, {
                "nome": info.get("name"),
                "modelo": model,
                "gravado_em": info.get("start_at"),
                "duracao_ms": info.get("duration"),
                "transcricao": resultado.get("transcricao"),
                "resumo": resultado.get("resumo"),
                "pontos_principais": resultado.get("pontos_principais") or [],
                "acoes": resultado.get("acoes") or [],
                "estudo": resultado.get("estudo"),
                "slides": resultado.get("slides") or [],
            })
            if linha:
                resultado["id"] = linha["id"]
        except storage.StorageError as error:
            # Perder o histórico é ruim, mas não justifica descartar o resumo pronto.
            app.logger.warning("Falha ao salvar no Supabase: %s", error)

    resultado["historico"] = []
    return jsonify(resultado)


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

    resumo_id = body.get("resumo_id")
    if storage.enabled() and resumo_id:
        try:
            storage.salvar_pergunta(resumo_id, question, resposta, model)
        except storage.StorageError as error:
            app.logger.warning("Falha ao salvar a pergunta no Supabase: %s", error)

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

if os.getenv("AUTO_RESUMO", "").strip() == "1":
    import auto_resumo

    auto_resumo.iniciar(app, sys.modules[__name__], int(os.getenv("AUTO_RESUMO_INTERVALO", "300")))
