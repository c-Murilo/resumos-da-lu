"""Corta MP3 em blocos, nos limites de frame.

Aula longa numa tacada só estoura o teto de saída da Gemini e a transcrição
volta cortada. Aqui o arquivo é fatiado antes de subir.

O corte é feito no próprio fluxo MP3: cada frame é uma unidade independente,
então basta somar durações e quebrar entre frames. Sem ffmpeg, sem recodificar
(o áudio sai idêntico ao original) e sem dependência nova.
"""

from pathlib import Path

# Tabelas do padrão MPEG Audio Layer III.
BITRATES_V1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0]
BITRATES_V2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0]
SAMPLERATES = {
    3: [44100, 48000, 32000],  # MPEG1
    2: [22050, 24000, 16000],  # MPEG2
    0: [11025, 12000, 8000],   # MPEG2.5
}


def _pular_id3(dados):
    """ID3v2 vem antes do áudio e tem tamanho em inteiros de 7 bits."""
    if dados[:3] != b"ID3" or len(dados) < 10:
        return 0
    tamanho = 0
    for byte in dados[6:10]:
        tamanho = (tamanho << 7) | (byte & 0x7F)
    return 10 + tamanho


def _ler_frame(dados, pos):
    """Devolve (tamanho_em_bytes, duracao_em_segundos) ou None se não for frame."""
    if pos + 4 > len(dados):
        return None
    if dados[pos] != 0xFF or (dados[pos + 1] & 0xE0) != 0xE0:
        return None

    versao = (dados[pos + 1] >> 3) & 0x03      # 3=MPEG1, 2=MPEG2, 0=MPEG2.5
    camada = (dados[pos + 1] >> 1) & 0x03      # 1 = Layer III
    if versao == 1 or camada != 1:
        return None

    indice_bitrate = (dados[pos + 2] >> 4) & 0x0F
    indice_taxa = (dados[pos + 2] >> 2) & 0x03
    padding = (dados[pos + 2] >> 1) & 0x01
    if indice_bitrate in (0, 15) or indice_taxa == 3:
        return None

    bitrate = (BITRATES_V1 if versao == 3 else BITRATES_V2)[indice_bitrate] * 1000
    taxa = SAMPLERATES[versao][indice_taxa]
    amostras = 1152 if versao == 3 else 576
    tamanho = int((amostras // 8) * bitrate / taxa) + padding
    if tamanho <= 4:
        return None
    return tamanho, amostras / taxa


def _frames(dados, inicio):
    """Percorre o fluxo uma vez: [(posição, duração em segundos), ...]."""
    lista, pos = [], inicio
    while pos < len(dados):
        frame = _ler_frame(dados, pos)
        if frame is None:
            # Lixo entre frames: procura o próximo sync em vez de desistir.
            proximo = dados.find(b"\xff", pos + 1)
            if proximo == -1:
                break
            pos = proximo
            continue
        tamanho, duracao = frame
        lista.append((pos, duracao))
        pos += tamanho
    return lista


def planejar(frames, limite_segundos, fim):
    """Cortes em partes iguais, cada uma dentro do limite.

    Fatiar de forma fixa deixaria uma sobra minúscula no fim (80 min em blocos
    de 40 = 40 + 40 + 0,2), e cada sobra dessas custa uma requisição inteira.
    Dividir igualmente evita isso e mantém todos os blocos abaixo do teto.
    """
    total = sum(duracao for _, duracao in frames)
    if total <= limite_segundos or not frames:
        return []

    partes = int(total // limite_segundos) + (1 if total % limite_segundos else 0)
    alvo = total / partes

    cortes, inicio, acumulado, restantes = [], frames[0][0], 0.0, partes
    for pos, duracao in frames:
        if restantes > 1 and acumulado >= alvo and pos > inicio:
            cortes.append((inicio, pos))
            inicio, acumulado, restantes = pos, 0.0, restantes - 1
        acumulado += duracao
    cortes.append((inicio, fim))
    return cortes


def dividir(caminho, segundos_por_bloco, destino=None):
    """Fatia o MP3 e devolve os caminhos dos blocos.

    Se o arquivo não for um MP3 legível, devolve [caminho] — o chamador segue
    com o arquivo inteiro em vez de falhar.
    """
    origem = Path(caminho)
    dados = origem.read_bytes()
    cortes = planejar(_frames(dados, _pular_id3(dados)), segundos_por_bloco, len(dados))
    if len(cortes) <= 1:
        return [str(origem)]

    pasta = Path(destino or origem.parent)
    caminhos = []
    for indice, (comeco, fim) in enumerate(cortes, start=1):
        bloco = pasta / f"{origem.stem}-bloco{indice:02d}{origem.suffix}"
        bloco.write_bytes(dados[comeco:fim])
        caminhos.append(str(bloco))
    return caminhos
