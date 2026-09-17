# Integração futura — Economatica (dados de mercado reais)

Este documento descreve como integrar **cotações e históricos oficiais da Economatica** ao
agente do OnTrack. Ele substitui a versão anterior deste arquivo, que descrevia um módulo de
dados de mercado (`obter_dados_b3`, `gerar_grafico_historico_b3`, `montar_contexto_b3`) e um
uso de `yfinance` — elementos que **não existem** em `agente.py` hoje.

A intenção é complementar o backend **sem alterar rotas Flask, o contrato do chat ou o
frontend**: `processar_mensagem()` continua devolvendo `{"tipo": "texto" | "imagem"}`.

---

## 1. Como o agente funciona hoje (sem dados de mercado reais)

Todo o fluxo vive em `back-end/agente.py` e é acionado por `back-end/app.py` na rota
`POST /api/chat`. Não há provedor de cotações: os números vêm do **modelo Gemini** e do
**blog público** da Economatica.

### Configuração e estado de módulo

| Item | Descrição |
| --- | --- |
| `GEMINI_API_KEY` | Lida de `back-end/.env` via `python-dotenv` (carregado pelo caminho do arquivo) |
| `MODEL_NAME` / `API_URL` | Modelo `gemini-3.5-flash` e endpoint REST de `generateContent` |
| `MEMORY_FILE` / `GRAFICOS_DIR` | `back-end/memoria.json` e `back-end/static/graficos/` |
| `ECONOMATICA_URL` | `https://insight.economatica.com/` — apenas o **blog público** |
| `CACHE_TTL_SEGUNDOS` / `_cache_economatica` | Cache de 30 minutos do texto do blog em memória |
| `SYSTEM_INSTRUCTION` / `CHART_SYSTEM_INSTRUCTION` | Instruções do consultor e do gerador de gráficos |
| `PALAVRAS_GRAFICO` / `PALAVRAS_IMAGEM_GERAL` | Palavras que classificam pedidos de gráfico e de imagem |
| `STATUS_TRANSITORIOS`, `MAX_TENTATIVAS_GEMINI`, `ESPERA_INICIAL_SEGUNDOS` | Política de retry do Gemini para indisponibilidade do serviço (500/502/503/504), com backoff |
| `COOLDOWN_QUOTA_PADRAO`, `COOLDOWN_QUOTA_DIARIA`, `_quota_bloqueada_ate` | Espera aplicada após um HTTP 429 (60 s para limite por minuto, 5 min para cota diária). Em cooldown, **nenhuma** chamada é enviada ao Gemini |
| `ARQUIVO_USO_GEMINI`, `LIMITE_DIARIO_PADRAO`, `FUSO_RENOVACAO`, `FUSO_EXIBICAO` | Contador local de cota em `back-end/uso_gemini.json`, limite padrão do plano gratuito (20/dia) e fusos usados para calcular a próxima renovação |

### Funções atuais

| Função | Papel |
| --- | --- |
| `caminho_memoria(uid=None)` | Resolve `back-end/memoria_<uid>.json` (ou `memoria.json` para visitantes) |
| `carregar_memoria(uid=None)` / `salvar_memoria(memoria, uid=None)` | Lê e grava os fatos do usuário (`{"fatos": [...]}`), tolerando JSON inválido |
| `montar_contexto_memoria(memoria)` | Formata os fatos como bloco de contexto do prompt |
| `limpar_html(html)` | Remove scripts, estilos e tags do HTML coletado |
| `buscar_contexto_economatica()` | Baixa o blog público (timeout de 15s), limpa o HTML, corta em 4000 caracteres e cacheia por 30 min; devolve `""` em qualquer falha |
| `chamar_gemini(prompt_usuario, instrucao_sistema)` | Chamada REST ao Gemini: **uma única chamada por mensagem** em condições normais; retry/backoff só em falha de serviço (500/502/503/504). HTTP 429 interrompe na hora e levanta `CotaDoGemini` |
| `mensagem_e_cooldown_de_quota(resposta)` / `_segundos_ate_liberar(resposta)` | Lêem o 429 do Gemini, distinguem limite por minuto de cota diária e devolvem a mensagem ao usuário, o tempo de espera e o limite diário identificado |
| `ler_uso_gemini()` / `registrar_chamada_gemini()` / `aprender_limite_diario(limite)` | Contador local do dia (zera na virada do dia no Pacífico): só a chamada bem-sucedida soma; o 429 apenas ensina o limite real da chave |
| `renovacao_em_texto()` | Data/hora da próxima renovação da cota, convertida para o horário de Brasília |
| `aviso_de_cota()` / `resposta_com_aviso_de_cota(texto)` | Aviso preventivo (20% ou 3 chamadas restantes) anexado ao fim das respostas do chat |
| `montar_prompt(pergunta, memoria, contexto_economatica)` | Junta contexto do blog + memória do usuário + pergunta |
| `perguntar_ao_gemini(pergunta, memoria, contexto_economatica)` | Resposta em texto do consultor |
| `eh_pedido_de_grafico(texto)` / `eh_pedido_de_imagem_nao_grafico(texto)` | Classificação da intenção do usuário |
| `extrair_json(texto)` | Converte a resposta do Gemini em JSON (aceita blocos ` ``` `) |
| `montar_linha_de_fontes(memoria=None, contexto_economatica="")` | Monta a linha `Fontes: ...` que acompanha cada gráfico, listando o modelo de IA, o blog público (quando usado) e os fatos informados pelo usuário |
| `gerar_grafico(pergunta, memoria, contexto_economatica, output_dir=None)` | Pede ao Gemini um JSON (`titulo`, `tipo` `linha`/`barra`, `eixo_x`, `eixo_y`, `labels`, `valores`), plota com `matplotlib`, grava no PNG um rodapé com a data de geração e o aviso de estimativa, e salva `grafico_AAAAMMDD_HHMMSS.png`; devolve `(nome_arquivo, titulo)` |
| `processar_mensagem(mensagem, uid=None)` | Ponto de entrada do Flask: decide entre recusa, gráfico ou texto. Respostas de cota/limite vêm com `pode_reenviar: True`, que o frontend usa para mostrar o botão **Tentar novamente** |

> `message.py.txt` é a versão anterior do agente em linha de comando (não é usada pelo site).
> Serve apenas como referência histórica.

### De onde vêm os números hoje

A instrução `CHART_SYSTEM_INSTRUCTION` pede explicitamente que o Gemini devolva **sua melhor
estimativa aproximada** quando não houver dados confiáveis. Por isso, o texto que acompanha cada
gráfico informa a data de geração e de onde vieram os números:

> Gráfico gerado: "Título do gráfico".
>
> Gerado em 17/09/2026 às 14:52.
> Fontes: conhecimento geral do modelo gemini-3.5-flash; conteúdo público do blog da
> Economatica (insight.economatica.com).
>
> Aviso: são estimativas geradas por IA — não são dados oficiais em tempo real.

A data e as fontes vêm de `montar_linha_de_fontes()` e do carimbo de tempo de `gerar_grafico()`, e
o mesmo carimbo é impresso no rodapé do PNG. Na integração, a cotação oficial entra nessa mesma
linha (troca-se "conhecimento geral do modelo" pela fonte oficial com o horário do dado) e o
aviso de estimativa pode sair do texto.

---

## 2. Onde a Economatica entra

Nenhum arquivo fora de `agente.py` precisa mudar. O `app.py` só transporta o dicionário de
retorno, e o frontend só consome `tipo`/`resposta`/`url`.

| Ponto atual | Situação hoje | O que muda na integração |
| --- | --- | --- |
| Novo módulo de dados (`obter_dados_b3`) | Não existe | Cria o cliente Economatica autenticado e normaliza o retorno em `dict` |
| `buscar_contexto_economatica()` | Apenas blog público | Passa a montar também um bloco com cotações/indicadores reais (mantendo o blog como research) |
| `montar_prompt(pergunta, memoria, contexto_economatica)` | Contexto do blog + memória | Recebe o bloco com os dados reais no lugar do texto puro (mesma assinatura) |
| `gerar_grafico(...)` | `labels`/`valores` vêm do JSON do Gemini | Quando houver série real do ticker, substituir `labels`/`valores` por ela antes do `plt.plot`/`plt.bar` |
| Texto de retorno em `processar_mensagem()` | Avisa que são estimativas da IA | Trocar o aviso por indicação de fonte e data/hora da cotação |
| `app.py` / frontend | Rotas e contrato atuais | **Sem alteração** |

### Fluxo após a integração

1. `processar_mensagem()` detecta o ticker/métrica pedida (ex.: `PETR4`, `Ibovespa`, `Selic`).
2. O novo módulo de dados consulta a Economatica e devolve `dict`/série normalizados.
3. `montar_prompt()` recebe o bloco com os dados reais; e `gerar_grafico()` usa a série real
   quando ela existir.
4. Se a consulta falhar, a execução **segue o caminho atual** (blog + estimativa do Gemini) —
   o mesmo padrão de tolerância já usado em `buscar_contexto_economatica()`, que devolve `""`
   em caso de erro.

---

## 3. Instalação e credenciais

```bash
pip install economatica
```

A linha `economatica` já está em `requirements.txt` — hoje ela **não é importada por nenhum
módulo** do backend, então removê-la é seguro enquanto a integração não acontece.

As credenciais devem ficar em `back-end/.env` (nunca no código nem no repositório):

```
ECONOMATICA_USER=seu_usuario
ECONOMATICA_PASSWORD=sua_senha
```

O pacote também exige autenticação antes da primeira consulta; o contrato exato de
`login`/`get_quotes`/histórico depende do plano contratado — **confirme com o suporte da
Economatica** antes de fixar as chamadas.

---

## 4. Esboço de implementação

Os nomes abaixo são os pontos reais de encaixe em `agente.py`:

```python
# agente.py — novo módulo de dados de mercado (ilustrativo)

def obter_dados_b3(ticker):
    """Devolve {'ticker', 'preco_atual', 'variacao_pct', 'volume', 'atualizado_em'}."""
    import economatica

    economatica.login(os.getenv("ECONOMATICA_USER"), os.getenv("ECONOMATICA_PASSWORD"))
    codigo = ticker.upper().replace(".SA", "")
    cotacoes = economatica.get_quotes(symbols=[codigo], fields=["close", "change_pct", "volume"])
    return normalizar(cotacoes[codigo])


def obter_serie_b3(ticker, inicio, fim):
    """Devolve (labels, valores) prontos para o matplotlib."""
    ...


def buscar_contexto_economatica(ticker=None):
    """Mantém o blog público e acrescenta os dados reais quando houver ticker."""
    ...
```

E o encaixe dentro da função que já existe — trecho **proposto**, não o código atual:

```python
def gerar_grafico(pergunta, memoria, contexto_economatica, output_dir=None):
    ...
    dados = extrair_json(chamar_gemini(prompt_final, CHART_SYSTEM_INSTRUCTION))

    serie = obter_serie_b3(ticker_identificado) if ticker_identificado else None
    if serie:
        labels, valores = serie          # dados reais
    else:
        labels, valores = dados["labels"], dados["valores"]   # comportamento atual
```

O parâmetro `output_dir` já existe e pode continuar sendo usado para testes que não escrevem
em `back-end/static/graficos/`.

---

## 5. Checklist de verificação

- [ ] Gráfico de um ticker conhecido (ex.: `PETR4`) exibe série real e informa a fonte com data
      e horário do dado, no lugar do carimbo de estimativa por IA.
- [ ] Falha/timeout da Economatica não derruba o chat: cai no caminho atual (blog + estimativa).
- [ ] `POST /api/chat` mantém o contrato `{"tipo": "texto" | "imagem"}` sem mudanças no frontend.
- [ ] Credenciais fora do repositório (`back-end/.env`, já coberto pelo `.gitignore`).
- [ ] Uma mensagem continua gerando **uma única** chamada à API (429 não é retentado; o tratamento de 500/503 permanece).

---

## 6. Referências

- Blog público (já usado hoje): <https://insight.economatica.com/>
- Endpoints oficiais: consultar suporte/contrato da Economatica.
- Visão geral da plataforma: `documentacao/introducao.mdx`
- Execução local: `README.md` na raiz do projeto
