# Run doc — OnTrack (preview local + deploy na Vercel)

O backend é um único Flask (`back-end/app.py`) que serve o site estático **e** a API.
Não há package.json, bundler nem passo de compilação.

- Local: http://localhost:5000/index.html (porta fixada em `app.run(..., port=5000)`)
- Produção: https://on-track-sage.vercel.app (site e API na MESMA origem, em `/api`)
- Health check: `/api/health` → `{"status": "ok"}`

## 1) Como reproduzir os artefatos que um checkout limpo precisa

1. **Dependências Python** — lista única em `requirements.txt` (raiz), que é o arquivo
   que a Vercel lê. Localmente:
   ```bash
   python -m venv back-end/venv
   back-end/venv/Scripts/pip.exe install -r back-end/requirements.txt
   ```
   `back-end/requirements.txt` só aponta para o da raiz (`-r ../requirements.txt`), para
   não existirem duas listas divergindo.
2. **Segredos** — `back-end/.env` **não** está no Git. Copie do checkout principal:
   ```bash
   cp /c/Users/cauag/CascadeProjects/OnTrack/back-end/.env back-end/.env
   ```
   Nunca copie valores para dentro deste documento. O `.env.example` (versionado) lista
   os nomes das variáveis. Sem o `.env` o servidor sobe: as fontes e IAs sem chave se
   desativam em silêncio.
3. **Arquivos de estado** — criados sozinhos, não precisam ser copiados:
   `back-end/estado_ias.json`, `back-end/memoria*.json`, `back-end/uso_gemini.json`
   (todos no `.gitignore`).
4. **Diretório de gráficos** — `back-end/static/graficos/` é criado pelo servidor.

## 2) Como rodar o servidor local (destacado, para o Preview)

Windows/PowerShell — nomeie o executável por extenso (`python.exe`), porque o
`Start-Process` não resolve shims de shell. Use o Python do venv:

```bash
RAIZ="C:/Users/cauag/CascadeProjects/OnTrack"
LOG="$RAIZ/.freebuff/preview.log"
powershell -NoProfile -Command "Start-Process -FilePath '$RAIZ/back-end/venv/Scripts/python.exe' -ArgumentList 'back-end/app.py' -RedirectStandardOutput '$LOG' -RedirectStandardError '$LOG.err' -WorkingDirectory '$RAIZ' -WindowStyle Hidden"
```

- stdout e stderr vão para **arquivos diferentes** (o PowerShell recusa o mesmo caminho).
- **Esse comando não retorna** quando a saída é redirecionada: o wrapper fica preso e o
  comando estoura o tempo limite, mas o servidor sobe. Confirme em outro comando:
  `netstat -ano | grep -E ":5000\b.*LISTENING"` e `curl /api/health`.
- O boot leva alguns segundos (aquece o contexto da Economatica); espere a porta responder.

### Ao reiniciar, mate o que já existe

`app.run(debug=True)` liga o **reloader** do Werkzeug, que gera processos filhos
aninhados (é normal ver 3-4 `python.exe` com `app.py` na linha de comando). Só um segura
a porta, e subir por cima deixa o antigo atendendo com o código velho — foi o que fez
uma edição parecer ignorada. Antes de subir de novo:

```bash
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*app.py*' } | ForEach-Object { Stop-Process -Id \$_.ProcessId -Force }"
netstat -ano | grep -E ":5000\b.*LISTENING" || echo "porta livre"
```

Depois de subir, o dono da porta deve ter nascido **depois** da última edição de
`back-end/app.py`; o log de boot imprime as fontes e a contagem de modelos do OpenRouter.

## 3) Deploy na Vercel

Estrutura (já criada no repositório):

| Arquivo | Papel |
| --- | --- |
| `vercel.json` | Reescreve `/api/*` para a função, encaminhando o caminho pedido |
| `api/index.py` | Entrypoint: carrega o Flask de `back-end/app.py` e restaura o PATH_INFO |
| `requirements.txt` (raiz) | Dependências que a Vercel instala (versões fixadas) |
| `.python-version` | `3.12` (versão com mais rodas prontas de matplotlib/pandas) |
| `.vercelignore` | Não sobe venv, `.env`, estado de execução nem PNGs de teste |

**Por que o middleware em `api/index.py`:** o destino de um `rewrite` recebe o caminho do
destino, não o original — então `/api/chat` chega como `/api/index?caminho=chat`. O
`CaminhoOriginal` devolve `/api/chat` ao Flask antes de ele rotear. Sem isso, tudo
responderia 404.

**Variáveis de ambiente na Vercel** (Project → Settings → Environment Variables), as
mesmas do `.env` local:

```
GEMINI_API_KEY  GEMINI_API_KEY_2  GEMINI_API_KEY_3  OPENROUTER_API_KEY
HGBRASIL_API_KEY  FINNHUB_API_KEY  TWELVE_DATA_API_KEY
```

Opcionais: `ORIGENS_PERMITIDAS` (lista separada por vírgula, para quando o site e a API
ficarem em domínios diferentes) e `BASE_URL` (só defina se o frontend for servido de
outra origem; vazio = URLs relativas, que é o caso normal).

**Limites que importam:** a função precisa de `maxDuration` de pelo menos 60s (a rotação
de IAs tem orçamento de 30s e a coleta leva ~2s; o pior caso medido foi 33s), e o pacote
com matplotlib + pandas + yfinance fica em ~250MB descompactado — abaixo do limite de
500MB de Python, mas perto o bastante para acompanhar o tamanho do build. Se o plano
recusar `maxDuration: 60`, baixe o valor e o orçamento da rotação junto.

## 4) Checklist depois de cada deploy

```bash
BASE=https://on-track-sage.vercel.app
curl -s -o /dev/null -w "%{http_code} " $BASE/                    # 200 (site)
curl -s -o /dev/null -w "%{http_code} " $BASE/api/health          # 200
curl -s $BASE/api | head -c 200                                   # lista das rotas
curl -s -o /dev/null -w "%{http_code} " $BASE/back-end/.env       # 404 (tem que ser 404!)
curl -s -o /dev/null -w "%{http_code} " $BASE/back-end/app.py     # 404
curl -s -X POST $BASE/api/chat -H 'Content-Type: application/json' \
  -d '{"mensagem":"Qual a cotacao do dolar hoje?","uid":"teste"}' | head -c 300
```

Se `/api/chat` responder 404 com `{"erro": "Arquivo não encontrado."}`, o rewrite não
está entregando o caminho: confira a regra de `vercel.json` e o log da função na Vercel.
