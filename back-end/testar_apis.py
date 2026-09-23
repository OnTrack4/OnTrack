"""
Teste isolado do provedor de IA ativo do OnTrack: o Claude (Anthropic).

    python back-end/testar_apis.py             # UMA requisição (consome tokens)
    python back-end/testar_apis.py --modelos   # lista os modelos (não consome tokens)

O que este script faz:

  * manda UMA única requisição para POST /v1/messages, com a pergunta mais curta
    possível (o objetivo é validar credencial e disponibilidade, não a qualidade da
    resposta), e imprime o status HTTP, o modelo usado e um trecho do texto devolvido;
  * valida a credencial de ponta a ponta: chave errada (401), chave sem permissão
    (403), modelo inexistente (404) e cota/limite (429) aparecem com a mensagem que
    a própria API devolve, e o script termina com código de saída 1.

Por que ele é isolado: NÃO importa back-end/app.py. Fala direto com a API usando
apenas `requests` e o back-end/.env. Assim ele não mexe em contadores locais, não
cria cooldown, não grava memória e não sobe nenhum servidor.

Este script testa SOMENTE o Claude: nenhuma outra IA é tocada aqui, então rodá-lo não
consome a cota de Gemini, Mistral, Cloudflare nem OpenRouter. Esses provedores voltaram
à rotação em back-end/app.py (PROVEDORES_IA_AUTORIZADOS) e são acionados pelo chat
normalmente — se quiser testá-los, use o chat ou acrescente um verificador aqui.

Atenção: o teste padrão é uma requisição de verdade e consome tokens da conta. É uma
por execução, de propósito — não rode isto em laço.

Opções:
    --modelos         lista os modelos disponíveis para a chave (não gasta tokens)
    --modelo MODELO   troca o modelo testado (padrão: CLAUDE_MODEL do .env)
    --sem-cores       desliga as cores (bom para log/CI)

O modelo padrão abaixo espelha back-end/app.py. Se você trocar lá (CLAUDE_MODEL),
troque aqui também — ou defina CLAUDE_MODEL no .env, que os dois respeitam.
"""

import argparse
import os
import sys
import time
from pathlib import Path

import requests

# O console do Windows usa cp1252 por padrão e quebra ao imprimir acentos e "x";
# aqui a saída é fixada em UTF-8, com "replace" para nunca derrubar o teste
for _fluxo in (sys.stdout, sys.stderr):
    try:
        _fluxo.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

BASE_DIR = Path(__file__).resolve().parent

# carrega o back-end/.env pelo caminho do arquivo, independentemente do diretório
# de execução (mesmo padrão do app.py)
try:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
except ImportError:  # sem python-dotenv, ainda funciona com variáveis já exportadas
    pass

# ---------------------------------------------------------------------------
# ENDPOINT, PROMPT MÍNIMO E TEMPOS
# ---------------------------------------------------------------------------

URL_CLAUDE = "https://api.anthropic.com/v1/messages"
URL_CLAUDE_MODELOS = "https://api.anthropic.com/v1/models"
CLAUDE_VERSION = "2023-06-01"

# pergunta mais simples possível: valida a credencial sem gastar tokens à toa
PERGUNTA = "Responda apenas com a palavra OK."
MODELO_CLAUDE = (
    os.getenv("CLAUDE_MODEL") or ""
).strip() or "claude-sonnet-5"
MAX_TOKENS = 64          # resposta de teste é uma palavra: não precisa de mais
TEMPERATURA = 0.1
TIMEOUT = 30


def chave(nome):
    """Lê a variável de ambiente; ausente ou vazia devolve ''."""
    return (os.getenv(nome) or "").strip()


def cabecalhos(chave_api):
    """Cabeçalhos exigidos pela API do Claude: a versão é obrigatória."""
    return {
        "x-api-key": chave_api,
        "anthropic-version": CLAUDE_VERSION,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# SAÍDA NO TERMINAL
# ---------------------------------------------------------------------------

CORES = sys.stdout.isatty()


def _cor(texto, codigo):
    return f"\033[{codigo}m{texto}\033[0m" if CORES else texto


def verde(texto):
    return _cor(texto, "32")


def vermelho(texto):
    return _cor(texto, "31")


def amarelo(texto):
    return _cor(texto, "33")


def cinza(texto):
    return _cor(texto, "90")


def _limpar(texto, limite=200):
    """Uma linha só, sem quebras, para não bagunçar a tabela do terminal."""
    texto = " ".join(str(texto or "").split())
    return texto[:limite] + ("..." if len(texto) > limite else "")


def _detalhe_erro(resposta):
    """Mensagem curta de erro, aproveitando o JSON do provedor quando existir."""
    try:
        dados = resposta.json()
    except ValueError:
        return _limpar(resposta.text)

    if isinstance(dados, dict):
        erro = dados.get("error")
        if isinstance(erro, dict):
            return _limpar(erro.get("message") or erro)
        if erro:
            return _limpar(erro)
        if dados.get("message"):
            return _limpar(dados["message"])
    return _limpar(resposta.text)


# ---------------------------------------------------------------------------
# O TESTE (sempre uma única requisição de geração)
# ---------------------------------------------------------------------------

def testar_claude(modelo):
    chave_api = chave("CLAUDE_API_KEY")
    if not chave_api:
        return {"rotulo": f"Claude ({modelo})", "ok": False, "status": None,
                "detalhe": "CLAUDE_API_KEY não configurada no back-end/.env"}

    try:
        resposta = requests.post(
            URL_CLAUDE,
            headers=cabecalhos(chave_api),
            json={
                "model": modelo,
                "max_tokens": MAX_TOKENS,
                "temperature": TEMPERATURA,
                "messages": [{"role": "user", "content": PERGUNTA}],
            },
            timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException as erro:
        return {"rotulo": f"Claude ({modelo})", "ok": False, "status": None,
                "detalhe": f"falha de rede ({type(erro).__name__})"}

    if resposta.status_code != 200:
        return {"rotulo": f"Claude ({modelo})", "ok": False,
                "status": resposta.status_code, "detalhe": _detalhe_erro(resposta)}

    try:
        conteudo = resposta.json().get("content")
    except ValueError:
        return {"rotulo": f"Claude ({modelo})", "ok": False,
                "status": resposta.status_code, "detalhe": "resposta não é JSON"}

    # formato de sucesso: {"content": [{"type": "text", "text": "..."}, ...]}
    texto = ""
    if isinstance(conteudo, list):
        texto = "".join(
            str(bloco.get("text") or "")
            for bloco in conteudo
            if isinstance(bloco, dict) and bloco.get("type") == "text"
        )

    return {"rotulo": f"Claude ({modelo})", "ok": bool(texto.strip()),
            "status": resposta.status_code, "detalhe": _limpar(texto) or "resposta vazia"}


def listar_modelos():
    """
    GET /v1/models: confirma a chave e mostra quais modelos a conta enxerga. É
    leitura de metadados — não gera texto e não consome tokens.
    """
    chave_api = chave("CLAUDE_API_KEY")
    if not chave_api:
        print(vermelho("CLAUDE_API_KEY não configurada no back-end/.env"))
        return 1

    try:
        resposta = requests.get(URL_CLAUDE_MODELOS, headers=cabecalhos(chave_api),
                                params={"limit": 50}, timeout=TIMEOUT)
    except requests.exceptions.RequestException as erro:
        print(vermelho(f"falha de rede ({type(erro).__name__})"))
        return 1

    if resposta.status_code != 200:
        print(vermelho(f"HTTP {resposta.status_code} — {_detalhe_erro(resposta)}"))
        return 1

    try:
        modelos = resposta.json().get("data") or []
    except ValueError:
        print(vermelho("resposta não é JSON"))
        return 1

    if not modelos:
        print(amarelo("a API respondeu 200, mas sem nenhum modelo listado"))
        return 1

    print(verde(f"chave válida — {len(modelos)} modelo(s) disponível(is):"))
    for modelo in modelos:
        identificador = modelo.get("id", "?")
        rotulo = modelo.get("display_name") or ""
        print(f"  - {identificador}" + (f"  {cinza(rotulo)}" if rotulo else ""))
    print()
    print(cinza("Para testar outro modelo: python back-end/testar_apis.py --modelo <id>"))
    return 0


# ---------------------------------------------------------------------------
# EXECUÇÃO
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Testa o provedor de IA ativo (Claude/Anthropic) com uma requisição."
    )
    parser.add_argument("--modelos", action="store_true",
                        help="lista os modelos da chave (não consome tokens)")
    parser.add_argument("--modelo", default=MODELO_CLAUDE,
                        help=f"modelo do Claude (padrão {MODELO_CLAUDE})")
    parser.add_argument("--sem-cores", action="store_true", help="desliga as cores")
    argumentos = parser.parse_args()

    global CORES
    if argumentos.sem_cores:
        CORES = False

    if argumentos.modelos:
        return listar_modelos()

    print(f"Testando 1 chamada de IA com a pergunta: {PERGUNTA!r}")
    print(cinza("Este teste consome 1 requisição (poucos tokens) da conta do Claude."))
    print(cinza("Gemini, Mistral, Cloudflare e OpenRouter não são tocados aqui."))
    print()

    inicio = time.time()
    try:
        resultado = testar_claude(argumentos.modelo)
    except Exception as erro:  # nenhuma surpresa derruba o teste
        resultado = {"rotulo": f"Claude ({argumentos.modelo})", "ok": False,
                     "status": None, "detalhe": f"{type(erro).__name__}: {erro}"}

    segundos = round(time.time() - inicio, 1)
    print("[1/1] claude ... ", end="")
    if resultado["ok"]:
        print(verde(f"OK ({segundos}s)"))
    else:
        status = f"HTTP {resultado['status']}" if resultado["status"] else "sem resposta"
        print(vermelho(f"FALHOU — {status}"))
    print(f"    {resultado['rotulo']}: {resultado['detalhe']}")
    print()

    if resultado["ok"]:
        print(f"Resumo: {verde('1 ok')} | 0 falha(s) de 1")
        return 0

    print(f"Resumo: 0 ok | {vermelho('1 falha(s)')} de 1")
    print(amarelo("Dica: 401 = chave inválida; 403 = chave sem permissão; "
                  "404 = modelo inexistente (veja --modelos); 429 = cota/limite."))
    return 1


if __name__ == "__main__":
    sys.exit(main())
