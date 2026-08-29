# Integração futura — Economatica

Este documento descreve como substituir o **yfinance** pela biblioteca/API **Economatica** no backend do OnTrack, sem alterar rotas Flask, fluxo do chat ou a estrutura de `processar_mensagem()`.

---

## 1. Instalação

```bash
pip install economatica
```

Adicionar também em `requirements.txt`:

```
economatica
```

---

## 2. Autenticação e consulta (API Python)

Exemplo mínimo de uso (ajuste usuário/senha conforme contrato Economatica):

```python
import economatica

# Autenticação na plataforma
economatica.login("seu_usuario", "sua_senha")

# Cotações em tempo real ou histórico
cotacoes = economatica.get_quotes(
    symbols=["PETR4", "VALE3"],
    fields=["close", "open", "high", "low", "volume", "change_pct"],
)

# Exemplo de retorno esperado (estrutura ilustrativa)
# {"PETR4": {"close": 38.50, "change_pct": 1.2, ...}, ...}
```

Credenciais devem ficar no `.env` (nunca no código):

```
ECONOMATICA_USER=seu_usuario
ECONOMATICA_PASSWORD=sua_senha
```

---

## 3. Instalação via Add-in do Excel (planilha local)

Alternativa quando a API Python não estiver disponível no contrato:

1. Instalar o **Add-in Economatica** no Excel.
2. Montar uma planilha local (ex.: `dados_b3.xlsx`) com fórmulas Economatica para cotações e histórico.
3. Ler a planilha no Python:

```python
import pandas as pd

def obter_dados_b3_excel(ticker: str, planilha: str = "dados_b3.xlsx") -> dict:
    df = pd.read_excel(planilha, sheet_name="Cotacoes")
    linha = df[df["Ticker"] == ticker.upper()]
    if linha.empty:
        return {"ticker": ticker, "erro": "Ticker não encontrado na planilha"}
    row = linha.iloc[0]
    return {
        "ticker": ticker.upper(),
        "preco_atual": float(row["Preco"]),
        "variacao_pct": float(row["VariacaoPct"]),
        "volume": int(row["Volume"]),
    }
```

Agendar atualização da planilha (refresh manual ou macro) antes de consultas automatizadas.

---

## 4. Mapeamento: yfinance → Economatica

O yfinance está concentrado em **`agente.py`**. Para migrar, troque apenas o **módulo de dados de mercado**; o restante do backend permanece igual.

| Função atual (yfinance) | Substituir por (Economatica) | Observação |
|-------------------------|------------------------------|------------|
| `normalizar_ticker_b3(ticker)` | Manter igual | Economatica usa tickers B3 sem `.SA` (ex.: `PETR4`) |
| `obter_dados_b3(ticker)` | `obter_dados_b3(ticker)` reimplementada com `economatica.get_quotes()` | Mesma assinatura e dict de retorno |
| `acao.history(period=...)` dentro de `gerar_grafico_historico_b3()` | `economatica.get_history(symbol, start, end)` ou leitura Excel | Alimentar `labels` e `valores` do Matplotlib |
| `montar_contexto_b3(texto)` | Sem mudança de assinatura | Passa a chamar a nova `obter_dados_b3()` |
| `montar_prompt(..., contexto_b3)` | Sem mudança | Gemini continua recebendo o bloco formatado |
| `gerar_grafico(...)` | Sem mudança de fluxo | Continua tentando ticker real primeiro, depois fallback Gemini |
| `processar_mensagem()` | Sem mudança | Apenas consome `montar_contexto_b3()` |

### Passo a passo da migração

1. **Criar** `obter_dados_b3_economatica(ticker)` com o mesmo formato de retorno de `obter_dados_b3()`.
2. **Trocar** o corpo de `obter_dados_b3()` para delegar à Economatica (ou renomear e apontar chamadas).
3. **Substituir** `yf.Ticker(...).history()` em `gerar_grafico_historico_b3()` por histórico Economatica.
4. **Remover** `import yfinance as yf` e a linha `yfinance` de `requirements.txt` quando a Economatica estiver estável.
5. **Manter** o scraping do blog público (`buscar_contexto_economatica()`) como contexto complementar de research — é independente da API paga.

### Exemplo de substituição interna (esboço)

```python
# agente.py — substituir apenas o corpo de obter_dados_b3()

def obter_dados_b3(ticker):
    import economatica
    economatica.login(os.getenv("ECONOMATICA_USER"), os.getenv("ECONOMATICA_PASSWORD"))
    codigo = ticker.upper().replace(".SA", "")
    cotacoes = economatica.get_quotes(symbols=[codigo], fields=["close", "change_pct", "volume", "pe", "div_yield"])
    # ... montar o mesmo dict que o yfinance retorna hoje ...
    return dados
```

Nenhuma alteração necessária em `app.py`, rotas `/api/chat` ou front-end.

---

## 5. Referências

- Blog público (já usado): https://insight.economatica.com/
- Documentação oficial Economatica: consultar suporte/contrato para endpoints exatos de `get_quotes` e histórico.
