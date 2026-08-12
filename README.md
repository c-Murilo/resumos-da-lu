# Resumos da Lu

PWA que lista as gravações da Plaud, resume com a Gemini e deixa perguntar sobre
cada conversa.

## Escolher o modelo

O seletor no topo da biblioteca lista os modelos que a **sua chave** realmente
oferece (`models.list` da Gemini, em cache por 10 min), com os mais novos no topo
e os `preview` no fim. A escolha fica salva no navegador e vale para o resumo e
para as perguntas.

No `.env`, ambos são opcionais:

- `GEMINI_MODELS` — restringe o seletor a uma lista fixa, separada por vírgula.
- `GEMINI_MODEL` — fixa o padrão; vazio, usa o mais recente estável da lista.

O backend só aceita modelo que esteja na lista, então o cliente não consegue
pedir um modelo arbitrário.

## Instalar como app

O `frontend/public/manifest.webmanifest` e o `sw.js` fazem do build um PWA
instalável ("Adicionar à tela de início" no iOS, ícone de instalar no Chrome).
O service worker guarda só a casca do app — as chamadas em `/api/` nunca são
cacheadas. Ícones em `frontend/public/icons/`; para regerar, veja a nota no fim
deste arquivo.

## Pré-requisitos

- Python 3.11+
- Node.js 20+
- Conta Plaud autenticada no MCP (`npx -y @plaud-ai/mcp@latest install`)
- Uma chave da Gemini API

## Executar

```bash
cp backend/.env.example backend/.env
# preencha GEMINI_API_KEY em backend/.env

cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
flask --app app run --debug --port 5001
```

Em outro terminal:

```bash
cd frontend
npm install
npm run dev
```

Abra `http://localhost:5173`.

## Deploy no DigitalOcean App Platform

Um container só: o Flask serve a API e o PWA buildado, então não há CORS nem
URL de API para configurar. O `Dockerfile` traz Python e Node, porque o servidor
MCP da Plaud é um pacote npm.

Ordem que funciona:

1. Faça o login local na Plaud (`npx -y @plaud-ai/mcp@latest install`).
2. Envie o token para o Supabase: `cd backend && .venv/bin/python enviar_token.py`.
   O container não tem navegador para OAuth; ele lê o token do banco ao subir e
   regrava lá quando a Plaud renova.
3. Suba o repo no GitHub e crie o app com `.do/app.yaml` (ajuste `github.repo`).
4. No painel, preencha os secrets `GEMINI_API_KEY` e `SUPABASE_SERVICE_KEY`.

## Resumo automático

Com `AUTO_RESUMO=1`, uma thread varre a Plaud a cada `AUTO_RESUMO_INTERVALO`
segundos (300 por padrão) e resume sozinha o que for **novo**.

"Novo" é o que foi criado depois do marco gravado em `config.auto_resumo_marco`,
fixado na primeira varredura. Ou seja: ligar o recurso não dispara o acervo
antigo. O marco só avança quando uma gravação é resumida com sucesso, então uma
falha não faz pular a seguinte; depois de 3 tentativas a gravação é abandonada
até o próximo restart.

## Login na Plaud

O login é feito uma vez, direto no MCP oficial (fluxo OAuth no navegador):

```bash
npx -y @plaud-ai/mcp@latest install
```

O token fica no perfil do usuário (`~/.plaud`), fora do projeto — o Flask apenas
sobe o MCP via `npx` e herda essa sessão. Para reautenticar, rode o mesmo comando.

## Personalizar os prompts

Três arquivos editáveis, enviados à Gemini como `system_instruction` e lidos a
cada requisição (não precisa reiniciar o servidor):

- `backend/prompt_transcricao.md` — como transcrever a aula (etapa 1).
- `backend/prompt.md` — como virar material de estudo (etapa 2).
- `backend/prompt_perguntas.md` — como responder às perguntas sobre a aula.

Processar uma gravação usa **duas requisições**: a primeira transcreve o áudio,
a segunda lê só o texto. Assim cada uma tem o teto de saída inteiro à disposição,
e a segunda não paga o custo do áudio de novo. Se alguma delas bater no limite,
o erro diz qual foi e sugere `GEMINI_MAX_OUTPUT_TOKENS`.

O contrato JSON do resumo continua em `app.py`, porque o frontend depende das
chaves. Os prompts dizem **o que** vai em cada uma:

| chave | conteúdo | onde aparece |
| --- | --- | --- |
| `transcricao` | transcrição integral, com termos médicos corrigidos | "Ver transcrição completa" |
| `resumo` | visão geral em até 5 linhas | bloco destacado no topo |
| `pontos_principais` | pontos de alta relevância para prova | coluna "Cai na prova" |
| `acoes` | recados administrativos, provas, prazos | coluna "Recados e prazos" |
| `estudo` | material de estudo completo em Markdown | seção "Material de estudo" |

## Perguntas sobre a gravação

Depois do resumo, o painel abre um campo de perguntas. Cada pergunta vai para
`POST /api/recordings/<id>/ask` junto com a transcrição e o histórico da
conversa, e a Gemini responde ancorada apenas naquele texto. Nada é gravado em
disco: o histórico vive na tela e some ao trocar de gravação.

## Fluxo

O Flask inicia o servidor MCP oficial da Plaud via `npx` e chama `list_files`,
`get_file` e `get_transcript`. Ao processar uma gravação, baixa a URL temporária
retornada pela Plaud, envia o áudio à Gemini Files API e pede uma transcrição
estruturada e um resumo em português. Não há persistência de áudio ou credenciais
no projeto.

## Regerar os ícones

O ícone (um L escuro sobre o verde-limão) é gerado por `frontend/scripts/make_icons.py`:

```bash
uv run --with pillow frontend/scripts/make_icons.py
```
