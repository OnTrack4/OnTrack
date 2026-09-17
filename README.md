# OnTrack — Consultor Financeiro IA

Plataforma de acompanhamento do mercado financeiro brasileiro (B3) com um consultor
baseado em IA. O frontend é estático (HTML/CSS/JavaScript) e o backend em Python/Flask
serve **o site e a API na mesma porta**, então não é preciso subir nenhum servidor
separado para o frontend.

## Requisitos

- **Python 3.10 ou superior** (testado com 3.14)
- Uma **chave da API do Google Gemini** (`GEMINI_API_KEY`)

## 1. Instalar as dependências

Na raiz do projeto:

```bash
# (opcional, mas recomendado) criar o ambiente virtual
python -m venv back-end/venv

# ativar o ambiente virtual
# Windows (CMD):
back-end\venv\Scripts\activate.bat
# Windows (PowerShell):
.\back-end\venv\Scripts\Activate.ps1
# Linux / macOS:
source back-end/venv/bin/activate

# instalar as dependências
pip install -r back-end/requirements.txt
```

> **Atenção:** o `requirements.txt` lista o pacote `economatica`, que **não é usado pelo
> código atual** (o contexto econômico é obtido via `requests`) e pode não instalar em
> qualquer máquina. Se a instalação falhar nele, instale apenas o necessário:
>
> ```bash
> pip install flask flask-cors requests matplotlib python-dotenv
> ```

## 2. Configurar as variáveis de ambiente

O backend lê o arquivo `back-end/.env` (carregado pelo caminho do arquivo, então funciona
rodando de qualquer diretório). Crie-o a partir do exemplo:

```bash
# Windows:
copy back-end\.env.example back-end\.env
# Linux / macOS:
cp back-end/.env.example back-end/.env
```

Depois edite `back-end/.env` e preencha a chave:

```
GEMINI_API_KEY=sua_chave_aqui
```

O `back-end/.env` está no `.gitignore` — **nunca** versione esse arquivo nem a chave.

## 3. Rodar o projeto

Na raiz do projeto:

```bash
python back-end/app.py
```

Saída esperada:

```
OnTrack rodando em http://localhost:5000
 * Serving Flask app 'app'
 * Debug mode: on
 * Running on http://127.0.0.1:5000
```

Abra **http://localhost:5000** no navegador. Esse único comando entrega:

- o site completo (`index.html`, `paginas/`, `css/`, `assets/`, `firebase.js`)
- a API do consultor: `POST /api/chat`
- os gráficos gerados: `/static/graficos/<arquivo>.png`

Para encerrar, use `Ctrl+C` no terminal.

### Usando o ambiente virtual sem ativá-lo

```bash
# Windows
back-end\venv\Scripts\python.exe back-end\app.py

# Linux / macOS
back-end/venv/bin/python back-end/app.py
```

## Rotas principais

| Rota | Método | Descrição |
| --- | --- | --- |
| `/` | GET | Página inicial (`index.html`) |
| `/paginas/<pagina>.html` | GET | Páginas internas (consultor, notícias, perfil, login...) |
| `/api/chat` | POST | Recebe `{"mensagem": "...", "uid": "..."}` e devolve texto ou gráfico |
| `/api/health` | GET | Verificação de saúde da API (`{"status": "ok"}`) |
| `/static/graficos/<arquivo>` | GET | Imagens de gráficos geradas pelo agente |

## Estrutura do projeto

```
OnTrack/
├── index.html              # página inicial
├── style.css               # estilos globais
├── script.js / firebase.js # scripts do site e autenticação (Firebase)
├── css/                    # estilos por página
├── paginas/                # páginas internas
├── assets/                 # imagens e logo da marca
├── documentacao/           # documentação (Mintlify)
└── back-end/
    ├── app.py              # servidor Flask (site + API)
    ├── agente.py           # agente de IA (Gemini + gráficos matplotlib)
    ├── requirements.txt    # dependências Python
    ├── .env.example        # modelo das variáveis de ambiente
    └── static/graficos/    # gráficos gerados em tempo de execução
```

## Solução de problemas

- **Porta 5000 ocupada** (`Address already in use` / `WinError 10048`) — a porta é fixa no
  `app.py` e o frontend aponta para ela. Encerre o processo que a está usando (ou ajuste `port=5000` em `app.py` e a
  constante `BACKEND_URL` em `index.html` e `paginas/consultor.html`).
- **"GEMINI_API_KEY não configurada"** — confira se `back-end/.env` existe e contém a
  chave; o arquivo é lido pelo caminho dele, não pelo diretório atual.
- **`RequestsDependencyWarning: urllib3 ... doesn't match a supported version`** — apenas
  aviso de versões do `urllib3`/`chardet` na máquina; não impede a execução.
- **Aviso de cota no fim da resposta** — o agente conta localmente as chamadas bem-sucedidas do
  dia em `back-end/uso_gemini.json` e, quando a cota diária está acabando (faltando 20% ou 3
  chamadas), acrescenta ao fim da resposta quantas restam e a data/hora da próxima renovação
  (meia-noite do Pacífico, exibida no horário de Brasília). O contador zera sozinho a cada
  renovação e o arquivo é ignorado pelo git. Nesses avisos o chat mostra um botão
  **Tentar novamente**, que reenvia a mesma pergunta sem precisar digitá-la de novo.
- **Mensagem sobre cota/limite do Gemini (HTTP 429)** — o plano gratuito tem limite de
  requisições por minuto e por dia. Cada mensagem faz **uma única** tentativa na API: ao receber
  429 o backend devolve a explicação ao usuário e entra em espera (60 s para limite por minuto,
  15 min para cota diária) sem continuar chamando a API.
- **Resposta "A API do Gemini está temporariamente indisponível (HTTP 503)"** — indisponibilidade
  do serviço: o servidor tenta novamente com espera progressiva; se persistir, aguarde alguns instantes.

## Notas de desenvolvimento

- O `app.py` roda com `debug=True` (servidor de desenvolvimento do Flask, com recarga
  automática). Para produção, use um servidor WSGI (por exemplo `waitress`) com
  `debug=False`.
- A variável `BASE_URL` (padrão `http://localhost:5000`) define o endereço usado nas URLs
  dos gráficos retornados pela API.
