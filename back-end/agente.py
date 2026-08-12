"""
Módulo do agente financeiro (Gemini + Economatica + gráficos Matplotlib).
Extraído de message.py.txt para uso pelo servidor Flask (app.py).
"""

import os
import json
import re
import time
from datetime import datetime

from dotenv import load_dotenv
import requests
import matplotlib

load_dotenv()

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------------

API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-3.5-flash"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(BASE_DIR, "memoria.json")
GRAFICOS_DIR = os.path.join(BASE_DIR, "static", "graficos")

ECONOMATICA_URL = "https://insight.economatica.com/"
CACHE_TTL_SEGUNDOS = 30 * 60
_cache_economatica = {"texto": "", "timestamp": 0.0}

SYSTEM_INSTRUCTION = (
    "Você é um assistente especializado exclusivamente em ECONOMIA "
    "(macroeconomia, microeconomia, finanças pessoais, mercado financeiro, "
    "inflação, juros, câmbio, política econômica, investimentos, etc.).\n\n"
    "Regras:\n"
    "1. Se a pergunta do usuário NÃO tiver relação com economia, responda "
    "de forma breve e educada que você só responde perguntas sobre "
    "economia, e não tente respondê-la.\n"
    "2. Quando informações pessoais do usuário forem fornecidas como "
    "contexto, use-as para personalizar sua resposta sempre que forem "
    "relevantes.\n"
    "3. Quando um trecho do blog público da Economatica (insight.economatica.com) "
    "for fornecido como contexto, use-o como referência PREFERENCIAL sobre "
    "mercado financeiro preferencial brasileiro. Deixe claro que é conteúdo público de "
    "research/blog, e não um dado de cotação em tempo real (você não tem "
    "acesso à plataforma paga da Economatica).\n"
    "4. Seja claro, objetivo e cite números/exemplos quando ajudar a "
    "entender. Se não tiver certeza de um número, diga isso explicitamente "
    "em vez de inventar.\n"
    "5. Responda sempre em português brasileiro.\n"
    "6. Quando falar sobre investimentos, sempre mencione os riscos envolvidos."
)

CHART_SYSTEM_INSTRUCTION = (
    "Você gera SOMENTE dados estruturados para um gráfico econômico, com "
    "base na pergunta do usuário e no contexto fornecido (que pode incluir "
    "trechos do blog da Economatica e informações guardadas pelo usuário).\n"
    "Responda ESTRITAMENTE em JSON válido, sem nenhum texto antes ou depois, "
    "sem blocos de markdown (sem ```), exatamente neste formato:\n"
    '{"titulo": "string", "tipo": "linha" ou "barra", "eixo_x": "string", '
    '"eixo_y": "string", "labels": ["a", "b", "..."], "valores": [0.0, 0.0]}\n'
    "Se não houver dados confiáveis suficientes, ainda assim devolva sua "
    "melhor estimativa aproximada nesse mesmo formato JSON — nunca fuja do "
    "formato e nunca escreva texto fora do JSON."
)

PALAVRAS_GRAFICO = ["gráfico", "grafico", "chart", "plot", "plotar", "plote"]
PALAVRAS_IMAGEM_GERAL = PALAVRAS_GRAFICO + [
    "imagem", "foto", "figura", "desenha", "desenhe", "desenhar", "png", "ilustração",
]


# ---------------------------------------------------------------------------
# MEMÓRIA PERSISTENTE
# ---------------------------------------------------------------------------

def caminho_memoria(uid=None):
    if uid:
        return os.path.join(BASE_DIR, f"memoria_{uid}.json")
    return MEMORY_FILE


def carregar_memoria(uid=None):
    arquivo = caminho_memoria(uid)
    if os.path.exists(arquivo):
        try:
            with open(arquivo, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {"fatos": []}
    return {"fatos": []}


def salvar_memoria(memoria, uid=None):
    arquivo = caminho_memoria(uid)
    with open(arquivo, "w", encoding="utf-8") as f:
        json.dump(memoria, f, ensure_ascii=False, indent=2)


def montar_contexto_memoria(memoria):
    fatos = memoria.get("fatos", [])
    if not fatos:
        return ""
    linhas = "\n".join(f"- {fato}" for fato in fatos)
    return f"Informações fornecidas anteriormente pelo usuário:\n{linhas}"


# ---------------------------------------------------------------------------
# CONTEXTO ECONOMATICA
# ---------------------------------------------------------------------------

def limpar_html(html):
    texto = re.sub(r"<script.*?>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    texto = re.sub(r"<style.*?>.*?</style>", " ", texto, flags=re.DOTALL | re.IGNORECASE)
    texto = re.sub(r"<[^>]+>", " ", texto)
    texto = re.sub(r"\s+", " ", texto).strip()
    return texto


def buscar_contexto_economatica():
    agora = time.time()
    if _cache_economatica["texto"] and (agora - _cache_economatica["timestamp"] < CACHE_TTL_SEGUNDOS):
        return _cache_economatica["texto"]

    try:
        resp = requests.get(
            ECONOMATICA_URL,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (agente-economia-python)"},
        )
        resp.raise_for_status()
        texto = limpar_html(resp.text)[:4000]
        _cache_economatica["texto"] = texto
        _cache_economatica["timestamp"] = agora
        return texto
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# GEMINI
# ---------------------------------------------------------------------------

def chamar_gemini(prompt_usuario, instrucao_sistema):
    if not API_KEY:
        raise ValueError(
            "GEMINI_API_KEY não configurada. Defina a variável no arquivo back-end/.env"
        )

    payload = {
        "contents": [{"parts": [{"text": prompt_usuario}]}],
        "systemInstruction": {"parts": [{"text": instrucao_sistema}]},
        "generationConfig": {"temperature": 0.4},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": API_KEY}

    resposta = requests.post(API_URL, headers=headers, json=payload, timeout=60)
    resposta.raise_for_status()
    dados = resposta.json()

    try:
        return dados["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        return f"[Resposta inesperada da API: {dados}]"


def montar_prompt(pergunta, memoria, contexto_economatica):
    blocos = []
    if contexto_economatica:
        blocos.append(
            "Trecho de conteúdo público do blog da Economatica "
            f"(insight.economatica.com):\n{contexto_economatica}"
        )
    contexto_memoria = montar_contexto_memoria(memoria)
    if contexto_memoria:
        blocos.append(contexto_memoria)

    if not blocos:
        return pergunta
    return "\n\n".join(blocos) + f"\n\nPergunta: {pergunta}"


def perguntar_ao_gemini(pergunta, memoria, contexto_economatica):
    prompt_final = montar_prompt(pergunta, memoria, contexto_economatica)
    return chamar_gemini(prompt_final, SYSTEM_INSTRUCTION)


# ---------------------------------------------------------------------------
# GRÁFICOS
# ---------------------------------------------------------------------------

def eh_pedido_de_grafico(texto):
    texto = texto.lower()
    return any(p in texto for p in PALAVRAS_GRAFICO)


def eh_pedido_de_imagem_nao_grafico(texto):
    texto = texto.lower()
    tem_palavra_imagem = any(p in texto for p in PALAVRAS_IMAGEM_GERAL)
    return tem_palavra_imagem and not eh_pedido_de_grafico(texto)


def extrair_json(texto):
    texto = texto.strip()
    texto = re.sub(r"^```(?:json)?", "", texto).strip()
    texto = re.sub(r"```$", "", texto).strip()
    return json.loads(texto)


def gerar_grafico(pergunta, memoria, contexto_economatica, output_dir=None):
    if output_dir is None:
        output_dir = GRAFICOS_DIR

    os.makedirs(output_dir, exist_ok=True)

    prompt_final = montar_prompt(pergunta, memoria, contexto_economatica)
    texto_resposta = chamar_gemini(prompt_final, CHART_SYSTEM_INSTRUCTION)
    dados = extrair_json(texto_resposta)

    titulo = dados.get("titulo", "Gráfico econômico")
    tipo = dados.get("tipo", "linha")
    eixo_x = dados.get("eixo_x", "")
    eixo_y = dados.get("eixo_y", "")
    labels = dados.get("labels", [])
    valores = dados.get("valores", [])

    plt.figure(figsize=(8, 5))
    if tipo == "barra":
        plt.bar(labels, valores, color="#2b6cb0")
    else:
        plt.plot(labels, valores, marker="o", color="#2b6cb0")

    plt.title(titulo)
    plt.xlabel(eixo_x)
    plt.ylabel(eixo_y)
    plt.xticks(rotation=45, ha="right")
    plt.grid(alpha=0.3)
    plt.tight_layout()

    nome_arquivo = f"grafico_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    caminho_completo = os.path.join(output_dir, nome_arquivo)
    plt.savefig(caminho_completo)
    plt.close()

    return nome_arquivo, titulo


# ---------------------------------------------------------------------------
# PONTO DE ENTRADA PARA O FLASK
# ---------------------------------------------------------------------------

def processar_mensagem(mensagem, uid=None):
    """
    Processa uma mensagem do chat web e retorna um dict pronto para JSON:
      {"tipo": "texto", "resposta": "..."}
      {"tipo": "imagem", "arquivo": "grafico_....png", "texto": "..."}
    """
    mensagem = (mensagem or "").strip()
    if not mensagem:
        return {"tipo": "texto", "resposta": "Por favor, envie uma pergunta sobre economia."}

    memoria = carregar_memoria(uid)

    if eh_pedido_de_imagem_nao_grafico(mensagem):
        return {
            "tipo": "texto",
            "resposta": (
                "Este assistente só gera gráficos (dados econômicos em formato de gráfico). "
                "Não gero fotos, desenhos ou outros tipos de imagem."
            ),
        }

    contexto_economatica = buscar_contexto_economatica()

    if eh_pedido_de_grafico(mensagem):
        try:
            nome_arquivo, titulo = gerar_grafico(
                mensagem, memoria, contexto_economatica, output_dir=GRAFICOS_DIR
            )
            return {
                "tipo": "imagem",
                "arquivo": nome_arquivo,
                "texto": (
                    f'Gráfico gerado: "{titulo}".\n\n'
                    "Aviso: os valores usados vêm do conhecimento geral da IA e/ou do blog "
                    "público da Economatica — não são dados oficiais em tempo real."
                ),
            }
        except (json.JSONDecodeError, KeyError) as erro:
            return {
                "tipo": "texto",
                "resposta": f"Não consegui montar o gráfico a partir da resposta da IA: {erro}",
            }
        except Exception as erro:
            return {"tipo": "texto", "resposta": f"Erro ao gerar o gráfico: {erro}"}

    try:
        resposta = perguntar_ao_gemini(mensagem, memoria, contexto_economatica)
        return {"tipo": "texto", "resposta": resposta}
    except requests.exceptions.HTTPError as erro:
        detalhe = erro.response.text if erro.response is not None else str(erro)
        return {"tipo": "texto", "resposta": f"Erro HTTP ao consultar o Gemini: {detalhe}"}
    except Exception as erro:
        return {"tipo": "texto", "resposta": f"Erro ao consultar o Gemini: {erro}"}
