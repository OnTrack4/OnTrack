"""
Teste rápido e isolado de todas as APIs de IA da rotação.

    python back-end/testar_apis.py

O que este script faz:

  * percorre os provedores em sequência — Gemini (principal e reservas), Mistral,
    Cloudflare Workers AI e OpenRouter — e manda UMA única requisição simples para
    cada um, uma de cada vez, nunca em paralelo;
  * faz uma pausa curta entre um provedor e o outro (--pausa, padrão 2s) para não
    encostar em limite por minuto;
  * imprime, por provedor, o status HTTP e um trecho da resposta, e termina com um
    resumo e código de saída 0 (tudo ok) ou 1 (alguma falha).

Por que ele é isolado: NÃO importa back-end/app.py. Fala direto com as APIs usando
apenas `requests` e o back-end/.env. Assim ele não mexe em contadores locais, não
cria cooldown, não grava memória e não sobe nenhum servidor.

Atenção: cada teste é uma requisição de verdade e consome cota do provedor. É um
por API, de propósito — não rode isto em laço.

Opções:
    --pausa SEGUNDOS        espera entre provedores (padrão 2)
    --somente LISTA         testa só alguns, separados por vírgula
                            (gemini,mistral,cloudflare,openrouter)
    --modelo-openrouter M   troca o modelo testado no OpenRouter
    --sem-cores             desliga as cores (bom para log/CI)

Os nomes de modelo abaixo espelham back-end/app.py. Se você trocar um modelo lá
(MODEL_NAME, MISTRAL_MODEL, CLOUDFLARE_MODEL), troque aqui também — ou defina
MISTRAL_MODEL / CLOUDFLARE_MODEL no .env, que o script respeita.
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
# PROMPT MÍNIMO E TEMPOS
# ---------------------------------------------------------------------------

# pergunta mais simples possível: o objetivo é validar credencial e disponibilidade,
# não a qualidade da resposta (e resposta curta gasta menos cota)
PERGUNTA = "Responda apenas com a palavra OK."
TEMPERATURA = 0.1
TIMEOUT = 20
PAUSA_PADRAO = 2.0

MODELO_GEMINI = "gemini-3.5-flash"                     # = MODEL_NAME de app.py
MODELO_MISTRAL = os.getenv("MISTRAL_MODEL", "mistral-small-latest").strip() or "mistral-small-latest"
MODELO_CLOUDFLARE = (
    os.getenv("CLOUDFLARE_MODEL") or ""
).strip() or "@cf/meta/llama-3.1-8b-instruct"
# só o PRIMEIRO modelo da lista de app.py é testado: um teste = uma requisição
MODELO_OPENROUTER = "inclusionai/ling-3.0-flash-fin:free"

URL_GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
URL_MISTRAL = "https://api.mistral.ai/v1/chat/completions"
URL_CLOUDFLARE = "https://api.cloudflare.com/client/v4/accounts/{conta}/ai/run/{modelo}"
URL_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"


def chave(nome):
    """Lê a variável de ambiente; ausente ou vazia devolve ''."""
    return (os.getenv(nome) or "").strip()


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


def _limpar(texto, limite=140):
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
        if dados.get("errors"):
            return _limpar(dados["errors"])
        erro = dados.get("error")
        if isinstance(erro, dict):
            return _limpar(erro.get("message") or erro)
        if erro:
            return _limpar(erro)
        if dados.get("message"):
            return _limpar(dados["message"])
    return _limpar(resposta.text)


# ---------------------------------------------------------------------------
# UM TESTE POR PROVEDOR (sempre uma única requisição)
# ---------------------------------------------------------------------------

def testar_gemini(rotulo, variavel):
    chave_api = chave(variavel)
    if not chave_api:
        return {"rotulo": rotulo, "ok": False, "status": None,
                "detalhe": f"{variavel} não configurada no .env"}

    url = URL_GEMINI.format(modelo=MODELO_GEMINI)
    try:
        resposta = requests.post(
            url,
            headers={"Content-Type": "application/json", "x-goog-api-key": chave_api},
            json={
                "contents": [{"parts": [{"text": PERGUNTA}]}],
                "generationConfig": {"temperature": TEMPERATURA, "maxOutputTokens": 512},
            },
            timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException as erro:
        return {"rotulo": rotulo, "ok": False, "status": None,
                "detalhe": f"falha de rede ({type(erro).__name__})"}

    if resposta.status_code != 200:
        return {"rotulo": rotulo, "ok": False, "status": resposta.status_code,
                "detalhe": _detalhe_erro(resposta)}

    try:
        texto = resposta.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError):
        return {"rotulo": rotulo, "ok": False, "status": resposta.status_code,
                "detalhe": "resposta em formato inesperado"}

    return {"rotulo": rotulo, "ok": bool(str(texto).strip()),
            "status": resposta.status_code, "detalhe": _limpar(texto)}


def testar_mistral():
    chave_api = chave("MISTRAL_API_KEY")
    if not chave_api:
        return {"rotulo": "Mistral", "ok": False, "status": None,
                "detalhe": "MISTRAL_API_KEY não configurada no .env"}

    try:
        resposta = requests.post(
            URL_MISTRAL,
            headers={"Authorization": f"Bearer {chave_api}", "Content-Type": "application/json"},
            json={
                "model": MODELO_MISTRAL,
                "messages": [
                    {"role": "system", "content": "Responda em português."},
                    {"role": "user", "content": PERGUNTA},
                ],
                "temperature": TEMPERATURA,
                "max_tokens": 512,
            },
            timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException as erro:
        return {"rotulo": "Mistral", "ok": False, "status": None,
                "detalhe": f"falha de rede ({type(erro).__name__})"}

    if resposta.status_code != 200:
        return {"rotulo": "Mistral", "ok": False, "status": resposta.status_code,
                "detalhe": _detalhe_erro(resposta)}

    try:
        texto = resposta.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return {"rotulo": "Mistral", "ok": False, "status": resposta.status_code,
                "detalhe": "resposta em formato inesperado"}

    return {"rotulo": f"Mistral ({MODELO_MISTRAL})", "ok": bool(str(texto).strip()),
            "status": resposta.status_code, "detalhe": _limpar(texto)}


def testar_cloudflare():
    token = chave("CLOUDFLARE_API_TOKEN")
    conta = chave("CLOUDFLARE_ACCOUNT_ID")
    rotulo = f"Cloudflare ({MODELO_CLOUDFLARE})"

    if not token or not conta:
        return {"rotulo": rotulo, "ok": False, "status": None,
                "detalhe": "CLOUDFLARE_API_TOKEN e/ou CLOUDFLARE_ACCOUNT_ID não configurados no .env"}

    url = URL_CLOUDFLARE.format(conta=conta, modelo=MODELO_CLOUDFLARE)
    try:
        resposta = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={
                "messages": [
                    {"role": "system", "content": "Responda em português e de forma curta."},
                    {"role": "user", "content": PERGUNTA},
                ],
                "temperature": TEMPERATURA,
                "max_tokens": 512,
            },
            timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException as erro:
        return {"rotulo": rotulo, "ok": False, "status": None,
                "detalhe": f"falha de rede ({type(erro).__name__})"}

    if resposta.status_code != 200:
        return {"rotulo": rotulo, "ok": False, "status": resposta.status_code,
                "detalhe": _detalhe_erro(resposta)}

    try:
        dados = resposta.json()
    except ValueError:
        return {"rotulo": rotulo, "ok": False, "status": resposta.status_code,
                "detalhe": "resposta não é JSON"}

    texto = ""
    resultado = dados.get("result")
    if isinstance(resultado, str):
        texto = resultado
    elif isinstance(resultado, dict):
        texto = resultado.get("response") or ""
        if not texto and resultado.get("choices"):
            texto = ((resultado["choices"][0] or {}).get("message") or {}).get("content") or ""

    if not str(texto).strip():
        return {"rotulo": rotulo, "ok": False, "status": resposta.status_code,
                "detalhe": _detalhe_erro(resposta) or "resposta vazia"}

    return {"rotulo": rotulo, "ok": True, "status": resposta.status_code,
            "detalhe": _limpar(texto)}


def testar_openrouter(modelo):
    chave_api = chave("OPENROUTER_API_KEY")
    if not chave_api:
        return {"rotulo": "OpenRouter", "ok": False, "status": None,
                "detalhe": "OPENROUTER_API_KEY não configurada no .env"}

    try:
        resposta = requests.post(
            URL_OPENROUTER,
            headers={
                "Authorization": f"Bearer {chave_api}",
                "Content-Type": "application/json",
                "HTTP-Referer": os.getenv("SITE_URL", "https://on-track-sage.vercel.app"),
                "X-Title": "OnTrack (teste de APIs)",
            },
            json={
                "model": modelo,
                "messages": [
                    {"role": "system", "content": "Responda em português."},
                    {"role": "user", "content": PERGUNTA},
                ],
                "temperature": TEMPERATURA,
                "max_tokens": 512,
            },
            timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException as erro:
        return {"rotulo": "OpenRouter", "ok": False, "status": None,
                "detalhe": f"falha de rede ({type(erro).__name__})"}

    if resposta.status_code != 200:
        return {"rotulo": f"OpenRouter ({modelo})", "ok": False,
                "status": resposta.status_code, "detalhe": _detalhe_erro(resposta)}

    try:
        texto = resposta.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return {"rotulo": f"OpenRouter ({modelo})", "ok": False,
                "status": resposta.status_code, "detalhe": "resposta em formato inesperado"}

    return {"rotulo": f"OpenRouter ({modelo})", "ok": bool(str(texto).strip()),
            "status": resposta.status_code, "detalhe": _limpar(texto)}


# ---------------------------------------------------------------------------
# EXECUÇÃO
# ---------------------------------------------------------------------------

def montar_testes(somente, modelo_openrouter):
    """
    Ordem igual à da rotação em app.py: Gemini principal, reservas, Mistral,
    Cloudflare e OpenRouter. Cada item é (nome, função) — chamadas uma por vez.
    """
    testes = [
        ("gemini", lambda: testar_gemini("Gemini principal", "GEMINI_API_KEY")),
        ("gemini", lambda: testar_gemini("Gemini reserva 1", "GEMINI_API_KEY_2")),
        ("gemini", lambda: testar_gemini("Gemini reserva 2", "GEMINI_API_KEY_3")),
        ("mistral", testar_mistral),
        ("cloudflare", testar_cloudflare),
        ("openrouter", lambda: testar_openrouter(modelo_openrouter)),
    ]
    if somente:
        testes = [item for item in testes if item[0] in somente]
    return testes


def main():
    parser = argparse.ArgumentParser(
        description="Testa, uma requisição por API, todos os provedores de IA da rotação."
    )
    parser.add_argument("--pausa", type=float, default=PAUSA_PADRAO,
                        help=f"segundos entre provedores (padrão {PAUSA_PADRAO})")
    parser.add_argument("--somente", default="",
                        help="lista separada por vírgula: gemini,mistral,cloudflare,openrouter")
    parser.add_argument("--modelo-openrouter", default=MODELO_OPENROUTER,
                        help=f"modelo do OpenRouter (padrão {MODELO_OPENROUTER})")
    parser.add_argument("--sem-cores", action="store_true", help="desliga as cores")
    argumentos = parser.parse_args()

    global CORES
    if argumentos.sem_cores:
        CORES = False

    so_estes = {nome.strip().lower() for nome in argumentos.somente.split(",") if nome.strip()}
    testes = montar_testes(so_estes, argumentos.modelo_openrouter)

    print(f"Testando {len(testes)} chamada(s) de IA, uma por vez, com a pergunta: {PERGUNTA!r}")
    print(cinza("Cada teste consome 1 requisição da cota real do provedor."))
    print()

    resultados = []
    for indice, (nome, funcao) in enumerate(testes, start=1):
        if indice > 1 and argumentos.pausa > 0:
            time.sleep(argumentos.pausa)  # respeita limite por minuto dos planos gratuitos

        print(f"[{indice}/{len(testes)}] {nome} ... ", end="", flush=True)
        inicio = time.time()
        try:
            resultado = funcao()
        except Exception as erro:  # nenhuma surpresa derruba o teste
            resultado = {"rotulo": nome, "ok": False, "status": None,
                         "detalhe": f"{type(erro).__name__}: {erro}"}

        resultado["segundos"] = round(time.time() - inicio, 1)
        resultados.append(resultado)

        if resultado["ok"]:
            print(verde(f"OK ({resultado['segundos']}s)"))
        else:
            status = f"HTTP {resultado['status']}" if resultado["status"] else "sem resposta"
            print(vermelho(f"FALHOU — {status}"))
        print(f"    {resultado['rotulo']}: {resultado['detalhe']}")

    # resumo final
    aprovados = [r for r in resultados if r["ok"]]
    reprovados = [r for r in resultados if not r["ok"]]

    print()
    print("=" * 68)
    print(f"Resumo: {verde(str(len(aprovados)) + ' ok')} | "
          f"{vermelho(str(len(reprovados)) + ' falha(s)')} de {len(resultados)}")
    for resultado in reprovados:
        print(f"  {vermelho('x')} {resultado['rotulo']}: {resultado['detalhe']}")

    if reprovados:
        print(amarelo("Dica: 401/403 = chave inválida ou sem permissão; 429 = cota/limite; "
                      "404 = modelo fora do ar ou token sem acesso a ele."))

    return 0 if not reprovados else 1


if __name__ == "__main__":
    sys.exit(main())
