"""Envia o token da Plaud desta máquina para o MongoDB.

Rode uma vez, depois do `npx -y @plaud-ai/mcp@latest install`:

    .venv/bin/python enviar_token.py

O container no App Platform não tem navegador para fazer OAuth; ele lê este
token do banco ao subir e regrava lá quando a Plaud o renova.
"""

import json
import sys

from dotenv import load_dotenv

load_dotenv()

import plaud_token  # noqa: E402  (precisa do .env carregado antes)
import storage  # noqa: E402


def main():
    if not storage.enabled():
        sys.exit("Configure SUPABASE_URL e SUPABASE_SERVICE_KEY no backend/.env.")

    conteudo = plaud_token._ler_arquivo()
    if not conteudo:
        sys.exit(
            f"Não achei o token em {plaud_token.TOKEN_PATH}.\n"
            "Faça o login primeiro: npx -y @plaud-ai/mcp@latest install"
        )
    if not conteudo.get("refresh_token"):
        sys.exit("O token não tem refresh_token; refaça o login na Plaud.")

    storage.salvar_token(conteudo)
    print(f"Token enviado ao MongoDB ({len(json.dumps(conteudo))} bytes).")
    print("O deploy no App Platform já consegue falar com a Plaud.")


if __name__ == "__main__":
    main()
