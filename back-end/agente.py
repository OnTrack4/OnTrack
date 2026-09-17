"""
Módulo do agente financeiro (Gemini + Economatica + gráficos Matplotlib).
Extraído de message.py.txt para uso pelo servidor Flask (app.py).
"""

import os
import json
import re
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# carrega o .env do back-end pelo caminho do arquivo, independentemente do
# diretório de execução (necessário para rodar "python back-end/app.py" na raiz)
load_dotenv(os.path.join(BASE_DIR, ".env"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO
# ---------------------------------------------------------------------------

API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = "gemini-3.5-flash"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL_NAME}:generateContent"

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

# erros transitórios do Gemini (indisponibilidade do serviço, não cota de uso)
STATUS_TRANSITORIOS = {500, 502, 503, 504}
MAX_TENTATIVAS_GEMINI = 3
ESPERA_INICIAL_SEGUNDOS = 2

# HTTP 429 é limite de uso: insistir só piora. Enquanto estiver bloqueado, nenhuma
# chamada é enviada à API — a mensagem recebe a explicação na hora.
COOLDOWN_QUOTA_PADRAO = 60
COOLDOWN_QUOTA_DIARIA = 5 * 60  # a cota "diária" do Gemini costuma liberar bem antes do dia virar
_quota_bloqueada_ate = 0.0


class CotaDoGemini(RuntimeError):
    """A API do Gemini recusou a chamada por limite de uso/cota (HTTP 429)."""


# ---------------------------------------------------------------------------
# COTA DIÁRIA (contagem local para avisar antes de estourar)
# ---------------------------------------------------------------------------

ARQUIVO_USO_GEMINI = os.path.join(BASE_DIR, "uso_gemini.json")
LIMITE_DIARIO_PADRAO = 20  # plano gratuito; é corrigido quando o 429 informa o limite real
FUSO_RENOVACAO = "America/Los_Angeles"  # a cota diária do Gemini renova à meia-noite do Pacífico
FUSO_EXIBICAO = "America/Sao_Paulo"


def _fuso(nome, offset_reserva):
    """Fuso IANA com reserva de deslocamento fixo (o Windows pode não ter a base IANA)."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(nome)
    except Exception:
        return timezone(timedelta(hours=offset_reserva))


def renovacao_em_texto():
    """Próxima renovação da cota diária, no horário local do usuário."""
    agora = datetime.now(_fuso(FUSO_RENOVACAO, -7))
    meia_noite = (agora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return meia_noite.astimezone(_fuso(FUSO_EXIBICAO, -3)).strftime("%d/%m/%Y às %H:%M")


def _dia_da_cota():
    """Dia de referência da cota (o mesmo fuso em que o Gemini faz a renovação)."""
    return datetime.now(_fuso(FUSO_RENOVACAO, -7)).strftime("%Y-%m-%d")


def _gravar_uso(dados):
    try:
        # grava em arquivo temporário e troca de uma vez, para nunca deixar o
        # arquivo pela metade caso haja leitura concorrente
        temporario = f"{ARQUIVO_USO_GEMINI}.tmp"
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        os.replace(temporario, ARQUIVO_USO_GEMINI)
    except OSError:
        pass


def ler_uso_gemini():
    """Contador do dia; zera sozinho quando a cota renova (virada do dia no Pacífico)."""
    dados = {}
    try:
        with open(ARQUIVO_USO_GEMINI, "r", encoding="utf-8") as f:
            dados = json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        dados = {}

    limite = int(dados.get("limite") or LIMITE_DIARIO_PADRAO)
    if dados.get("dia") != _dia_da_cota():
        dados = {"dia": _dia_da_cota(), "chamadas": 0, "limite": limite}
        _gravar_uso(dados)

    dados["limite"] = limite
    dados["chamadas"] = int(dados.get("chamadas") or 0)
    return dados


def registrar_chamada_gemini():
    """Conta a chamada que consumiu cota (apenas as bem-sucedidas)."""
    dados = ler_uso_gemini()
    dados["chamadas"] += 1
    _gravar_uso(dados)
    return dados


def aprender_limite_diario(limite):
    """
    Guarda o limite informado pelo próprio Gemini (o plano pode não ser o gratuito).
    Não marca o dia como esgotado: um 429 pode ser só a janela de requisições cheia.
    """
    dados = ler_uso_gemini()
    dados["limite"] = max(int(limite), 1)
    _gravar_uso(dados)
    return dados


def aviso_de_cota():
    """
    Aviso preventivo para o chat quando a cota diária está acabando
    (faltando 20% ou menos, ou 3 chamadas ou menos). Vazio enquanto há folga.
    """
    dados = ler_uso_gemini()
    limite = max(dados["limite"], 1)
    restantes = max(limite - dados["chamadas"], 0)

    if restantes > max(3, round(limite * 0.2)):
        return ""

    if restantes == 0:
        return (
            f"⚠️ Cota diária do Gemini esgotada ({dados['chamadas']} de {limite} chamadas). "
            f"A próxima renovação é em {renovacao_em_texto()} (horário de Brasília)."
        )

    return (
        f"⚠️ Cota diária do Gemini quase no fim: restam {restantes} de {limite} chamadas. "
        f"A próxima renovação é em {renovacao_em_texto()} (horário de Brasília)."
    )


def resposta_com_aviso_de_cota(texto):
    """Acrescenta o aviso preventivo de cota ao texto que vai para o chat."""
    aviso = aviso_de_cota()
    return f"{texto}\n\n{aviso}" if aviso else texto


def _segundos_ate_liberar(resposta):
    """Lê o retryDelay sugerido pelo Gemini (ex.: '50s'), quando existir."""
    try:
        for detalhe in resposta.json().get("error", {}).get("details", []):
            atraso = detalhe.get("retryDelay")
            if atraso:
                return int(float(str(atraso).rstrip("s")))
    except (ValueError, AttributeError):
        pass
    return 0


def mensagem_e_cooldown_de_quota(resposta):
    """
    Converte o HTTP 429 do Gemini em uma mensagem curta para o chat, no tempo de espera
    antes de tentar de novo (cota diária espera bem mais que a por minuto) e, quando for
    cota diária, no limite de requisições por dia identificado.
    """
    espera_api = _segundos_ate_liberar(resposta)

    identificador = ""
    limite = None
    try:
        for detalhe in resposta.json().get("error", {}).get("details", []):
            for violacao in detalhe.get("violations") or []:
                identificador = (violacao.get("quotaId") or "").lower()
                limite = violacao.get("quotaValue")
                break
    except (ValueError, AttributeError):
        pass

    if "perday" in identificador:
        limite_diario = int(limite) if limite else LIMITE_DIARIO_PADRAO
        return (
            "A cota diária da API do Gemini no plano gratuito foi atingida "
            f"(limite de {limite_diario} requisições por dia neste modelo). "
            f"A próxima renovação é em {renovacao_em_texto()} (horário de Brasília).",
            max(espera_api, COOLDOWN_QUOTA_DIARIA),
            limite_diario,
        )

    espera = max(espera_api, COOLDOWN_QUOTA_PADRAO)
    return (
        "O limite de requisições da API do Gemini foi atingido (HTTP 429). "
        f"Aguarde cerca de {espera} segundos e envie a mensagem novamente.",
        espera,
        None,
    )


def chamar_gemini(prompt_usuario, instrucao_sistema):
    global _quota_bloqueada_ate

    if not API_KEY:
        raise ValueError(
            "GEMINI_API_KEY não configurada. Defina a variável no arquivo back-end/.env"
        )

    restante = int(_quota_bloqueada_ate - time.time())
    if restante > 0:
        # cota estourada: nenhuma chamada é feita, só a explicação ao usuário
        raise CotaDoGemini(
            "A cota da API do Gemini está temporariamente esgotada. "
            f"Tente novamente em cerca de {restante} segundos."
        )

    payload = {
        "contents": [{"parts": [{"text": prompt_usuario}]}],
        "systemInstruction": {"parts": [{"text": instrucao_sistema}]},
        "generationConfig": {"temperature": 0.4},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": API_KEY}

    espera = ESPERA_INICIAL_SEGUNDOS

    for tentativa in range(1, MAX_TENTATIVAS_GEMINI + 1):
        try:
            resposta = requests.post(API_URL, headers=headers, json=payload, timeout=60)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            # sem resposta da API: espera e tenta novamente antes de desistir
            if tentativa == MAX_TENTATIVAS_GEMINI:
                raise requests.exceptions.RequestException(
                    "A API do Gemini não respondeu (tempo esgotado ou falha de conexão). "
                    "Tente novamente em alguns instantes."
                )
            time.sleep(espera)
            espera *= 2
            continue

        if resposta.status_code == 429:
            # limite de uso/cota: uma única tentativa, sem insistir na API
            mensagem, cooldown, limite_diario = mensagem_e_cooldown_de_quota(resposta)
            _quota_bloqueada_ate = time.time() + cooldown
            if limite_diario:
                aprender_limite_diario(limite_diario)
            raise CotaDoGemini(mensagem)

        if resposta.status_code in STATUS_TRANSITORIOS:
            # indisponibilidade temporária (503 etc.): backoff e nova tentativa
            if tentativa < MAX_TENTATIVAS_GEMINI:
                time.sleep(espera)
                espera *= 2
                continue
            raise requests.exceptions.HTTPError(
                "A API do Gemini está temporariamente indisponível "
                f"(HTTP {resposta.status_code}). Tente novamente em alguns instantes.",
                response=resposta,
            )

        resposta.raise_for_status()
        registrar_chamada_gemini()  # só a chamada bem-sucedida consome cota
        dados = resposta.json()

        try:
            return dados["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError):
            return f"[Resposta inesperada da API: {dados}]"

    raise requests.exceptions.RequestException(
        "Não foi possível obter resposta da API do Gemini."
    )


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


def montar_linha_de_fontes(memoria=None, contexto_economatica=""):
    """
    Monta a linha que mostra de onde vieram os dados de um gráfico ou resposta,
    para o usuário saber o que é estimativa e o que vem de fonte pública.
    """
    fontes = [f"conhecimento geral do modelo {MODEL_NAME}"]
    if contexto_economatica:
        fontes.append("conteúdo público do blog da Economatica (insight.economatica.com)")
    if memoria and memoria.get("fatos"):
        fontes.append("informações fornecidas por você em conversas anteriores")

    rotulo = "Fonte" if len(fontes) == 1 else "Fontes"
    return f"{rotulo}: " + "; ".join(fontes) + "."


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
    # rodapé no próprio PNG: a estimativa não deve circular sem data e contexto
    agora = datetime.now()
    plt.figtext(
        0.5,
        0.01,
        f"Gerado por IA em {agora.strftime('%d/%m/%Y às %H:%M')} — "
        "estimativas, não são dados oficiais em tempo real.",
        ha="center",
        fontsize=8,
        color="#6b7280",
    )
    plt.tight_layout(rect=(0, 0.05, 1, 1))

    nome_arquivo = f"grafico_{agora.strftime('%Y%m%d_%H%M%S')}.png"
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
                "texto": resposta_com_aviso_de_cota(
                    f'Gráfico gerado: "{titulo}".\n\n'
                    f"Gerado em {datetime.now().strftime('%d/%m/%Y às %H:%M')}.\n"
                    f"{montar_linha_de_fontes(memoria, contexto_economatica)}\n\n"
                    "Aviso: são estimativas geradas por IA — não são dados oficiais em tempo real."
                ),
            }
        except (json.JSONDecodeError, KeyError) as erro:
            return {
                "tipo": "texto",
                "resposta": f"Não consegui montar o gráfico a partir da resposta da IA: {erro}",
            }
        except CotaDoGemini as erro:
            # pode_reenviar: o frontend mostra o botão "Tentar novamente"
            return {"tipo": "texto", "resposta": str(erro), "pode_reenviar": True}
        except Exception as erro:
            return {"tipo": "texto", "resposta": f"Erro ao gerar o gráfico: {erro}"}

    try:
        resposta = perguntar_ao_gemini(mensagem, memoria, contexto_economatica)
        return {"tipo": "texto", "resposta": resposta_com_aviso_de_cota(resposta)}
    except CotaDoGemini as erro:
        # pode_reenviar: o frontend mostra o botão "Tentar novamente"
        return {"tipo": "texto", "resposta": str(erro), "pode_reenviar": True}
    except requests.exceptions.HTTPError as erro:
        # erros transitórios já vêm com mensagem amigável; os demais mostram o detalhe
        detalhe = str(erro)
        if erro.response is not None and erro.response.status_code not in STATUS_TRANSITORIOS:
            detalhe = f"{detalhe} — {erro.response.text[:300]}"
        return {"tipo": "texto", "resposta": f"Erro HTTP ao consultar o Gemini: {detalhe}"}
    except Exception as erro:
        return {"tipo": "texto", "resposta": f"Erro ao consultar o Gemini: {erro}"}
