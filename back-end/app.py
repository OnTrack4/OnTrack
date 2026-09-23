"""
Servidor Flask do OnTrack — serve o frontend estático (index.html, paginas/,
css/, assets/) e concentra TODA a lógica do agente financeiro.

Execução padrão, a partir da raiz do projeto:
    python back-end/app.py

Este arquivo é autossuficiente e mantém, sem quebrar contrato com o frontend:

  * processar_mensagem(mensagem, uid=None) -> {"tipo": "texto", ...}
                                              {"tipo": "imagem", ...}
  * memória persistente por usuário (memoria_<uid>.json / memoria.json)
  * contexto público do blog da Economatica (buscar_contexto_economatica)
  * geração de gráficos em PNG com Matplotlib (gerar_grafico)
  * SYSTEM_INSTRUCTION e CHART_SYSTEM_INSTRUCTION

Camadas novas:
  1. Dados de mercado multi-fonte — yfinance, HG Brasil Finance, Finnhub e
     Twelve Data. Cada consulta fica isolada em try/except próprio: cota
     estourada (403/429/quota exceeded), timeout ou falha de conexão apenas
     descartam aquela fonte, e a resposta segue com as demais. Se todas
     falharem, o prompt continua limpo e a IA responde com conhecimento geral.
  2. Rotação sequencial de IAs — a cadeia começa no Gemini principal e segue para
     as duas chaves reservas do Gemini, a Mistral, o Cloudflare Workers AI e o
     OpenRouter (com lista de modelos); o Claude (Anthropic, POST /v1/messages)
     fecha a fila, como último recurso. Nunca há requisições paralelas: cada
     provedor só é acionado quando o anterior realmente não conseguiu responder
     (HTTP 429/503, erro de chave/modelo, 5xx ou falha de rede). A ordem está em
     _candidatos_ia() e o conjunto que a rotação PODE acionar é
     PROVEDORES_IA_AUTORIZADOS — hoje com todos os provedores; quem sair dessa
     tupla fica desligado (sem apagar o código) na rotação e nas travas de entrada.
  3. Estado das IAs para diagnóstico — GET /api/status-ias devolve o estado local dos
     provedores (chamadas do dia + cooldowns) a partir de uso_ias.json, sem chamar
     nenhuma API. É endpoint interno: nenhum número de cota é exibido na interface do
     chat, porque o contador é por instância (em serverless vive no /tmp e zera a frio)
     e não representa a cota real da conta.

Nenhum erro de API externa derruba o backend nem interrompe o chat.
"""

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone


# ---------------------------------------------------------------------------
# MEDIDOR DE CONSUMO (por mensagem)
#
# O custo em requisições era invisível: uma mensagem podia disparar 11 chamadas
# de IA e uma dúzia de consultas de mercado sem que nada na tela mostrasse isso.
# O medidor usa uma variável thread-local, porque o Flask atende cada mensagem
# em um thread — o que um usuário enviou não pode vazar para o de outro.
# ---------------------------------------------------------------------------
_medidor_local = threading.local()


def _medidor():
    atual = getattr(_medidor_local, "atual", None)
    if atual is None:
        atual = {"ia": 0, "fontes": 0}
        _medidor_local.atual = atual
    return atual


def _medidor_iniciar():
    _medidor_local.atual = {"ia": 0, "fontes": 0}


def _medidor_contar(tipo):
    _medidor()["ia" if tipo == "ia" else "fontes"] += 1
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAIZ_PROJETO = os.path.dirname(BASE_DIR)

# carrega o .env do back-end pelo caminho do arquivo, independentemente do
# diretório de execução (necessário para rodar "python back-end/app.py" na raiz)
load_dotenv(os.path.join(BASE_DIR, ".env"))

import matplotlib

matplotlib.use("Agg")  # não precisa de tela/GUI para gerar o PNG
import matplotlib.pyplot as plt

# yfinance é opcional: se não estiver instalado, as outras três fontes de
# mercado continuam funcionando e o servidor inicia normalmente.
try:
    import yfinance as yf
except Exception:  # ImportError ou dependência incompatível
    yf = None

# ---------------------------------------------------------------------------
# CAMINHOS
# ---------------------------------------------------------------------------

# Em serverless (Vercel) o diretório do projeto é SOMENTE LEITURA: só /tmp grava, e o
# /tmp é apagado a cada instância nova. Então o que é estado de execução — contador de
# cota, cooldown dos provedores, PNG dos gráficos — passa a ficar lá. O que é dado do
# usuário (memória) vem do Firestore, enviado pelo frontend, então não depende disso.
# Rodando local ("python back-end/app.py") os caminhos continuam os de sempre.
SOMENTE_LEITURA = bool(os.environ.get("VERCEL") or os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
DIR_ESTADO = "/tmp" if SOMENTE_LEITURA else BASE_DIR

MEMORY_FILE = os.path.join(DIR_ESTADO, "memoria.json")
GRAFICOS_DIR = (
    os.path.join(DIR_ESTADO, "graficos") if SOMENTE_LEITURA
    else os.path.join(BASE_DIR, "static", "graficos")
)
ARQUIVO_USO_GEMINI = os.path.join(DIR_ESTADO, "uso_gemini.json")
# estado dos provedores de IA (quem está em cooldown) entre reinícios do servidor
ARQUIVO_ESTADO_IAS = os.path.join(DIR_ESTADO, "estado_ias.json")

# ---------------------------------------------------------------------------
# ARQUIVOS DO SITE QUE PODEM SER SERVIDOS
#
# O servidor entrega o frontend estático, e antes ele entregava QUALQUER arquivo do
# repositório por causa da rota coringa: um GET /back-end/.env devolvia as chaves de
# API em texto claro, e /back-end/app.py devolvia o código do backend. Agora só o que
# o site realmente usa é servido; o resto responde 404.
# ---------------------------------------------------------------------------

ARQUIVOS_DO_SITE = {"index.html", "script.js", "firebase.js", "style.css"}
DIRETORIOS_DO_SITE = ("css/", "paginas/", "assets/")
EXTENSOES_DO_SITE = {
    ".html", ".css", ".js", ".map", ".ico", ".png", ".jpg", ".jpeg", ".svg",
    ".webp", ".gif", ".woff", ".woff2", ".ttf",
}


def arquivo_do_site_permitido(caminho):
    """
    True só para arquivo do frontend. Fecha, em especial, back-end/ (código e .env),
    .git/, documentação, *.py e os JSON de estado — nada disso é conteúdo público.
    """
    caminho = str(caminho or "").strip().lstrip("/")
    if not caminho or caminho.startswith(".") or ".." in caminho or "\\" in caminho:
        return False
    if caminho in ARQUIVOS_DO_SITE:
        return True
    if not caminho.startswith(DIRETORIOS_DO_SITE):
        return False
    return os.path.splitext(caminho)[1].lower() in EXTENSOES_DO_SITE

ECONOMATICA_URL = "https://insight.economatica.com/"
CACHE_TTL_SEGUNDOS = 30 * 60
TIMEOUT_ECONOMATICA = 5  # era 15s: o site responde em ~1,5s, não vale travar o chat
_cache_economatica = {"texto": "", "timestamp": 0.0}
_economatica_buscando = False  # evita buscar o mesmo blog duas vezes ao mesmo tempo


# ---------------------------------------------------------------------------
# CHAVES DE API
#
# Todas vêm de back-end/.env, que é ignorado pelo Git. Nenhum valor fica
# embutido no código: se uma variável estiver ausente, apenas aquele
# provedor/fonte fica indisponível e o restante do chat continua funcionando.
# ---------------------------------------------------------------------------

def _chave(nome_variavel):
    """Lê a variável de ambiente; ausente ou vazia devolve "" (fonte desativada)."""
    return (os.getenv(nome_variavel) or "").strip()


# fontes de dados de mercado
HGBRASIL_API_KEY = _chave("HGBRASIL_API_KEY")
FINNHUB_API_KEY = _chave("FINNHUB_API_KEY")
TWELVE_DATA_API_KEY = _chave("TWELVE_DATA_API_KEY")

# notícias de mercado (GNews + Marketaux): lidas SÓ pelo backend — a rota
# /api/noticias consulta as duas APIs e devolve o resultado já unificado, então a
# chave nunca é enviada ao navegador (onde qualquer visitante a veria no DevTools)
# nem fica no repositório.
GNEWS_API_KEY = _chave("GNEWS_API_KEY")
MARKETAUX_API_KEY = _chave("MARKETAUX_API_KEY")

# IAs da rotação, na ordem da cadeia: primeiro as três chaves do Gemini, depois a
# Mistral, o Cloudflare Workers AI e o OpenRouter e, por último, o Claude — que só
# é acionado se todos os anteriores falharem. Trocar a ordem é só mover as linhas
# de _candidatos_ia().
OPENROUTER_API_KEY = _chave("OPENROUTER_API_KEY")
GEMINI_API_KEY = _chave("GEMINI_API_KEY")
GEMINI_API_KEY_2 = _chave("GEMINI_API_KEY_2")
GEMINI_API_KEY_3 = _chave("GEMINI_API_KEY_3")
MISTRAL_API_KEY = _chave("MISTRAL_API_KEY")
# Claude (Anthropic) — provedor ativo do chat. A chave vive no back-end/.env
# (ignorado pelo Git) e nunca no código: sem ela o provedor fica desativado e o
# chat avisa, em vez de acionar qualquer outra IA.
CLAUDE_API_KEY = _chave("CLAUDE_API_KEY")
# Cloudflare Workers AI: token e account id andam juntos — sem os dois o provedor
# fica desativado e a rotação simplesmente segue para o próximo.
CLOUDFLARE_API_TOKEN = _chave("CLOUDFLARE_API_TOKEN")
CLOUDFLARE_ACCOUNT_ID = _chave("CLOUDFLARE_ACCOUNT_ID")


# ---------------------------------------------------------------------------
# INSTRUÇÕES DO SISTEMA
# ---------------------------------------------------------------------------

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
    "6. Quando falar sobre investimentos, sempre mencione os riscos envolvidos.\n"
    "7. Quando um bloco 'Dados de mercado' for fornecido como contexto, esses "
    "são cotações recentes coletadas de fontes públicas nesta mesma mensagem: "
    "use esses números em vez de estimar, informe a fonte e a hora da coleta e, "
    "se as fontes divergirem entre si, diga isso. Nunca invente uma cotação que "
    "não esteja no bloco nem no seu conhecimento.\n"
    "8. Quando a resposta for longa, divida-a em blocos curtos e progressivos "
    "(um tópico ou passo por bloco), em vez de escrever um texto único e extenso.\n"
    "9. Se a pergunta citar EXPLICITAMENTE uma fonte de dados de mercado "
    "(yfinance/Yahoo Finance, HG Brasil, Finnhub, Twelve Data ou o blog da "
    "Economatica), use SOMENTE os números dessa fonte no bloco 'Dados de mercado' "
    "e diga na resposta qual fonte você usou. Não misture nem substitua pela "
    "cotação de outro provedor: se a fonte citada não aparecer no bloco, avise que "
    "ela não trouxe dado nesta consulta em vez de usar o número de outra. Sem "
    "nenhuma fonte citada, siga o padrão: use todos os dados disponíveis no bloco.\n"
    "10. NUNCA escreva código de programação na resposta — nada de Python, "
    "JavaScript, SQL, nome de biblioteca (pandas, yfinance, yahooquery) nem trecho "
    "como 'ticker.dividends'. O usuário é investidor, não programador: toda "
    "explicação sai em linguagem de mercado, mesmo quando ele pede um dado que "
    "você não tem.\n"
    "11. Faltando no contexto um dado HISTÓRICO (dividendos já pagos, série de "
    "fechamentos, balanços), não invente número e não mostre caminho técnico para "
    "chegar nele: diga em uma frase que esse histórico não faz parte do contexto "
    "desta conversa, explique o conceito em nível comercial — quem decide e aprova "
    "o provento, as datas de anúncio e de pagamento, a diferença entre dividendo, "
    "juros sobre capital próprio (JCP) e bonificação, e o que o dividend yield "
    "representa — e indique onde o usuário encontra a série oficial completa: o "
    "site Status Invest ou a área de Relações com Investidores (RI) da própria "
    "empresa, com os avisos aos acionistas. Se o bloco 'Dados de mercado' trouxer "
    "o dividend yield de 12 meses, use esse número citando a fonte."
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
    "formato e nunca escreva texto fora do JSON. Se a pergunta citar "
    "explicitamente uma fonte de dados de mercado, use no gráfico SOMENTE os "
    "números dessa fonte e, se ela não aparecer no contexto, siga a regra acima "
    "de melhor estimativa aproximada."
)

PALAVRAS_GRAFICO = ["gráfico", "grafico", "chart", "plot", "plotar", "plote"]
PALAVRAS_IMAGEM_GERAL = PALAVRAS_GRAFICO + [
    "imagem", "foto", "figura", "desenha", "desenhe", "desenhar", "png", "ilustração",
]


# ---------------------------------------------------------------------------
# MEMÓRIA PERSISTENTE
# ---------------------------------------------------------------------------

def _uid_seguro(valor):
    """Deixa no uid apenas o que é seguro dentro de um nome de arquivo.

    Também impede path traversal: nada de barras, pontos ou '..' no nome.
    """
    return re.sub(r"[^A-Za-z0-9_-]", "", str(valor or ""))[:64]


def caminho_memoria(uid=None):
    uid_limpo = _uid_seguro(uid)
    if uid_limpo:
        return os.path.join(DIR_ESTADO, f"memoria_{uid_limpo}.json")
    return MEMORY_FILE


def caminho_memoria_conversa(uid, conversa_id):
    """
    Memória opcional por conversa (memoria_<uid>_<conversa>.json).

    Usada pela exclusão de conversa: o histórico da barra lateral fica no
    navegador, mas se houver arquivo de memória daquela conversa, ele sai junto.
    """
    uid_limpo = _uid_seguro(uid)
    conversa_limpa = _uid_seguro(conversa_id)
    if not uid_limpo or not conversa_limpa:
        return None
    return os.path.join(DIR_ESTADO, f"memoria_{uid_limpo}_{conversa_limpa}.json")


def apagar_arquivo_memoria(caminho):
    """
    Remove um arquivo de memória — nunca fora do diretório do backend e nunca
    algo que não comece com 'memoria'. Devolve True só quando algo foi apagado.
    """
    if not caminho:
        return False

    destino = os.path.abspath(caminho)
    if not destino.startswith(os.path.abspath(DIR_ESTADO) + os.sep):
        return False
    if not os.path.basename(destino).startswith("memoria"):
        return False

    try:
        os.remove(destino)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


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
    try:
        with open(arquivo, "w", encoding="utf-8") as f:
            json.dump(memoria, f, ensure_ascii=False, indent=2)
    except OSError:
        # disco somente leitura (serverless): a memória do usuário é do Firestore
        pass


def montar_contexto_memoria(memoria):
    """
    Monta o bloco de memória. O conteúdo vem do frontend (Firestore) ou do arquivo
    local, então é tratado como entrada não confiável: o que não for lista de textos
    vira lista vazia, e quantidade e tamanho são limitados para o prompt não inflar.
    """
    fatos = memoria.get("fatos") if isinstance(memoria, dict) else None
    if not isinstance(fatos, list):
        return ""

    fatos = [str(fato).strip() for fato in fatos
             if fato is not None and str(fato).strip()][:100]
    if not fatos:
        return ""

    linhas = "\n".join(f"- {fato[:500]}" for fato in fatos)
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


def _baixar_contexto_economatica(timeout=TIMEOUT_ECONOMATICA):
    """Busca o blog e guarda no cache. Nunca levanta exceção."""
    global _economatica_buscando

    try:
        resp = requests.get(
            ECONOMATICA_URL,
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (agente-economia-python)"},
        )
        resp.raise_for_status()
        texto = limpar_html(resp.text)[:4000]
        _cache_economatica["texto"] = texto
        _cache_economatica["timestamp"] = time.time()
        return texto
    except Exception:
        # Se o site estiver fora do ar ou bloquear a requisição, segue sem
        # esse contexto extra em vez de travar o agente.
        return ""
    finally:
        _economatica_buscando = False


def aquecer_contexto_economatica():
    """
    Busca o blog em segundo plano (chamada no start do servidor).

    Sem isso, a PRIMEIRA mensagem de cada 30 min pagaria a busca inteira.
    """
    global _economatica_buscando

    if _economatica_buscando:
        return
    _economatica_buscando = True
    try:
        threading.Thread(target=_baixar_contexto_economatica, daemon=True).start()
    except Exception:
        _economatica_buscando = False


def buscar_contexto_economatica():
    global _economatica_buscando

    agora = time.time()
    if _cache_economatica["texto"] and (agora - _cache_economatica["timestamp"] < CACHE_TTL_SEGUNDOS):
        return _cache_economatica["texto"]

    # já existe uma busca em andamento (start do servidor ou outra mensagem): não
    # duplica a espera — devolve "" e a próxima mensagem já encontra o cache cheio
    if _economatica_buscando:
        return ""

    _economatica_buscando = True
    return _baixar_contexto_economatica()


# ---------------------------------------------------------------------------
# COTA DIÁRIA DO GEMINI (contagem local para avisar antes de estourar)
# ---------------------------------------------------------------------------

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


def _meia_noite_da_cota():
    """Agora e a próxima virada do dia no fuso em que a cota do Gemini renova."""
    agora = datetime.now(_fuso(FUSO_RENOVACAO, -7))
    meia_noite = (agora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return agora, meia_noite


def renovacao_em_texto():
    """Próxima renovação da cota diária, no horário local do usuário."""
    _, meia_noite = _meia_noite_da_cota()
    return meia_noite.astimezone(_fuso(FUSO_EXIBICAO, -3)).strftime("%d/%m/%Y às %H:%M")


def _segundos_ate_renovar():
    """
    Segundos até a cota diária renovar DE VERDADE (meia-noite do Pacífico).

    Era aqui que o consumo se multiplicava: a cota diária esgotada recebia um
    cooldown de 5 minutos, então a cada 5 minutos cada mensagem voltava a tentar as
    3 chaves do Gemini e os 8 modelos do OpenRouter — requisições condenadas, sem
    parar, até o dia virar.
    """
    agora, meia_noite = _meia_noite_da_cota()
    return max(int((meia_noite - agora).total_seconds()), 60)


def _segundos_ate_renovar_utc():
    """O teto diário dos modelos gratuitos do OpenRouter renova à meia-noite UTC."""
    agora = datetime.now(timezone.utc)
    meia_noite = (agora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((meia_noite - agora).total_seconds()), 60)


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
    """Conta a chamada que consumiu cota (apenas as bem-sucedidas na chave principal)."""
    dados = ler_uso_gemini()
    dados["chamadas"] += 1
    _gravar_uso(dados)
    return dados


def aprender_limite_diario(limite, esgotado=False):
    """
    Guarda o limite informado pelo próprio Gemini (o plano pode não ser o gratuito).

    `esgotado=True` (429 de cota DIÁRIA): o contador local passa a refletir a
    realidade — antes ele continuava dizendo "13 de 20 chamadas" com o dia já morto,
    e o aviso preventivo nunca aparecia. Um 429 de janela por minuto NÃO marca o dia.
    """
    dados = ler_uso_gemini()
    dados["limite"] = max(int(limite), 1)
    if esgotado:
        dados["chamadas"] = max(dados["chamadas"], dados["limite"])
    _gravar_uso(dados)
    return dados


# ---------------------------------------------------------------------------
# COTA DOS PROVEDORES DA ROTAÇÃO (contador local por dia de renovação)
#
# Esse contador serve só ao diagnóstico interno (rota /api/status-ias): nenhum número
# de cota é exibido no chat nem anexado à resposta da IA — o contador é por instância,
# não reflete a cota real da conta. O consumo de todos os provedores é contado aqui, em
# arquivo próprio (uso_ias.json). O "dia" de cada contador é o dia
# em que a cota daquele provedor realmente renova: meia-noite do Pacífico para o
# Gemini e meia-noite UTC para Mistral, Cloudflare e OpenRouter.
# ---------------------------------------------------------------------------

ARQUIVO_USO_IAS = os.path.join(DIR_ESTADO, "uso_ias.json")

# Tetos diários conhecidos dos planos gratuitos. None = o provedor não limita por
# requisição/dia; nesse caso o diagnóstico devolve consumido + a explicação de
# NOTAS_LIMITE_IA em vez de um número de "restantes" enganoso.
LIMITES_DIARIOS_IA = {
    # o Claude cobra por token e limita por minuto, não por requisições/dia:
    # None = não há teto fixo por requisição (o diagnóstico mostra a nota abaixo)
    "claude": None,
    "gemini_principal": LIMITE_DIARIO_PADRAO,
    "gemini_reserva_1": LIMITE_DIARIO_PADRAO,
    "gemini_reserva_2": LIMITE_DIARIO_PADRAO,
    "mistral": None,
    "cloudflare": None,
    "openrouter": int(os.getenv("OPENROUTER_LIMITE_DIARIO") or 50),
}

FUSOS_RENOVACAO_IA = {
    "claude": ("UTC", 0),
    "gemini_principal": ("America/Los_Angeles", -7),
    "gemini_reserva_1": ("America/Los_Angeles", -7),
    "gemini_reserva_2": ("America/Los_Angeles", -7),
    "mistral": ("UTC", 0),
    "cloudflare": ("UTC", 0),
    "openrouter": ("UTC", 0),
}

NOTAS_LIMITE_IA = {
    "claude": "limite por minuto de tokens; sem teto fixo de requisições por dia",
    "mistral": "plano gratuito limita por minuto (1 req/s), sem teto diário fixo",
    "cloudflare": "gratuito: 10.000 neurons/dia (o teto é por neuron, não por requisição)",
    "openrouter": "teto dos modelos gratuitos; ajuste em OPENROUTER_LIMITE_DIARIO",
}

# leitura + gravação do contador precisam ser atômicas entre threads: o Flask atende
# cada mensagem em um thread próprio e duas respostas podem terminar juntas
_trava_uso_ias = threading.Lock()


def _dia_do_provedor(provedor):
    """Dia de referência do contador, no fuso em que a cota daquele provedor renova."""
    nome_fuso, offset = FUSOS_RENOVACAO_IA.get(provedor, ("UTC", 0))
    return datetime.now(_fuso(nome_fuso, offset)).strftime("%Y-%m-%d")


def _ler_uso_ias():
    try:
        with open(ARQUIVO_USO_IAS, "r", encoding="utf-8") as f:
            dados = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(dados, dict):
        return {}
    return {nome: valor for nome, valor in dados.items() if isinstance(valor, dict)}


def _gravar_uso_ias(dados):
    try:
        temporario = f"{ARQUIVO_USO_IAS}.tmp"
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        os.replace(temporario, ARQUIVO_USO_IAS)
    except OSError:
        pass


def registrar_chamada_ia(provedor):
    """
    Conta a chamada bem-sucedida de um provedor da rotação. O contador zera sozinho
    quando o dia de renovação daquele provedor vira; falha de disco (serverless)
    apenas deixa o painel sem número, nunca derruba o chat.
    """
    try:
        with _trava_uso_ias:
            dados = _ler_uso_ias()
            dia = _dia_do_provedor(provedor)
            atual = dados.get(provedor) or {}
            chamadas = int(atual.get("chamadas") or 0) if atual.get("dia") == dia else 0
            dados[provedor] = {"dia": dia, "chamadas": chamadas + 1}
            _gravar_uso_ias(dados)
            return dados[provedor]
    except Exception:
        return {}


def renovacao_em_texto_utc():
    """Próxima virada do dia UTC (quando renovam Mistral, Cloudflare e OpenRouter)."""
    agora = datetime.now(timezone.utc)
    meia_noite = (agora + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return meia_noite.astimezone(_fuso(FUSO_EXIBICAO, -3)).strftime("%d/%m/%Y às %H:%M")


def status_provedores_ia():
    """
    Situação de cada API da rotação para o painel do chat: chamadas feitas hoje no
    contador local, teto conhecido do plano gratuito, quanto ainda resta e se o
    provedor está em espera (cooldown) neste momento. Lê apenas estado local — não
    faz nenhuma requisição aos provedores, então consultar isso não consome cota.
    """
    dados = _ler_uso_ias()
    limite_gemini = ler_uso_gemini()["limite"]  # aprendido com o próprio 429/plano
    agora = time.time()

    provedores = []
    for identificador, rotulo, configurado in (
        ("claude", f"Claude ({CLAUDE_MODEL})", bool(CLAUDE_API_KEY)),
        ("gemini_principal", "Gemini principal", bool(GEMINI_API_KEY)),
        ("gemini_reserva_1", "Gemini reserva 1", bool(GEMINI_API_KEY_2)),
        ("gemini_reserva_2", "Gemini reserva 2", bool(GEMINI_API_KEY_3)),
        ("mistral", f"Mistral ({MISTRAL_MODEL})", bool(MISTRAL_API_KEY)),
        (
            "cloudflare",
            f"Cloudflare ({CLOUDFLARE_MODEL})",
            bool(CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID),
        ),
        (
            "openrouter",
            f"OpenRouter ({len(MODELOS_OPENROUTER_FALLBACK)} modelos)",
            bool(OPENROUTER_API_KEY),
        ),
    ):
        registro = dados.get(identificador) or {}
        chamadas = (
            int(registro.get("chamadas") or 0)
            if registro.get("dia") == _dia_do_provedor(identificador)
            else 0
        )
        # o limite aprendido vem do modelo PRINCIPAL; as chaves de reserva usam o Flash
        # Lite, cujo balde de cota é próprio — então aqui o número é aproximado
        limite = (
            limite_gemini if identificador.startswith("gemini")
            else LIMITES_DIARIOS_IA.get(identificador)
        )
        limite = int(limite) if limite else None
        provedores.append({
            "id": identificador,
            "rotulo": rotulo,
            "configurado": configurado,
            # fora da lista autorizada o provedor está configurado, mas inerte
            "ativo": _ia_autorizada(identificador),
            "chamadas": chamadas,
            "limite": limite,
            "restantes": max(limite - chamadas, 0) if limite else None,
            "em_espera_segundos": int(
                max(_quota_bloqueada_ate.get(identificador, 0.0) - agora, 0)
            ),
            "renovacao": (
                renovacao_em_texto() if identificador.startswith("gemini")
                else renovacao_em_texto_utc()
            ),
            "nota": NOTAS_LIMITE_IA.get(identificador, ""),
        })
    return provedores


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
            max(espera_api, COOLDOWN_QUOTA_DIARIA, _segundos_ate_renovar()),
            limite_diario,
        )

    espera = max(espera_api, COOLDOWN_QUOTA_PADRAO)
    return (
        "O limite de requisições da API do Gemini foi atingido (HTTP 429). "
        f"Aguarde cerca de {espera} segundos e envie a mensagem novamente.",
        espera,
        None,
    )


# ===========================================================================
# 1) CAMADA DE DADOS FINANCEIROS MULTI-FONTE
#
# Cada fonte é consultada de forma independente e silenciosa. Qualquer falha
# (HTTP 403/429/quota exceeded, timeout, falha de conexão, símbolo fora do
# plano, JSON inesperado) vira um FonteIndisponivel interno que é descartado
# sem aparecer para o usuário e sem interromper a resposta do chat.
# ===========================================================================

TIMEOUT_FONTES = 5            # era 8s: fonte lenta não pode travar o chat
# Endpoints do HG Brasil. O painel clássico (/finance) responde câmbio, índices, BTC
# e Selic/CDI numa requisição só; o catálogo v2 (/v2/finance/quotes) é o caminho
# atual para ação da B3 (B3:PETR4), par de moeda (FOREX:USDBRL) e índice
# (INDEXNYSE:SPX) e é o único que devolve o dividend yield de 12 meses da ação.
HG_PAINEL_URL = "https://api.hgbrasil.com/finance"
HG_QUOTES_URL = "https://api.hgbrasil.com/v2/finance/quotes"
# Twelve Data: /quote é a cotação corrente (e o caminho para ação da B3)
URL_TWELVE_QUOTE = "https://api.twelvedata.com/quote"
CACHE_TTL_COTACOES = 300      # era 60s: cotação de 5 min em 5 min já basta para o chat
                              # e reduz 5x o consumo de cota das APIs gratuitas
CACHE_TTL_FALHAS = 120        # fonte que recusou um ativo não é reconsultada por 2 min
                              # (era o HG Brasil sendo chamado à toa a cada mensagem)
COOLDOWN_QUOTA_FINANCEIRA = 10 * 60   # após 403/429, a fonte fica em silêncio por 10 min
MAX_ATIVOS_POR_MENSAGEM = 4
# A primeira fonte que responde encerra a busca do ativo — e essa primeira é a da fila
# do tipo do ativo (ORDEM_POR_TIPO), não sempre o yfinance. Antes as QUATRO fontes eram
# consultadas para cada ativo — 16 requisições numa mensagem de 4 ativos, com o yfinance
# já tendo respondido. Twelve Data free = 8 req/min e 800/dia, Finnhub = 60/min: era
# cota gratuita queimada sem ganho de informação. As demais fontes continuam sendo
# consultadas quando a primeira falha (é o fallback, não a supressão).
UMA_FONTE_POR_ATIVO = True


class FonteIndisponivel(Exception):
    """
    Falha silenciosa de uma fonte de mercado. `cooldown` > 0 coloca a fonte em
    espera (usado quando a API responde 403/429, ou seja, cota/plano estourado).
    """

    def __init__(self, motivo="", cooldown=0.0):
        super().__init__(motivo)
        self.motivo = motivo
        self.cooldown = cooldown


_cache_financeiro = {}
_fontes_bloqueadas = {}
# teto do cache em memória (ver _podar_cache) e trava para a poda não competir com
# a escrita de outro thread do Flask
MAX_ENTRADAS_CACHE = 400
_trava_cache = threading.Lock()


def _cache_ler(chave):
    item = _cache_financeiro.get(chave)
    if not item:
        return None
    valor, expira_em = item
    if expira_em < time.time():
        with _trava_cache:
            _cache_financeiro.pop(chave, None)
        return None
    return valor


def _cache_guardar(chave, valor, ttl=CACHE_TTL_COTACOES):
    with _trava_cache:
        _cache_financeiro[chave] = (valor, time.time() + ttl)
        # O dicionário antes só crescia: uma entrada saía apenas quando a MESMA chave
        # era relida depois do TTL. Termo de busca livre (/api/noticias?q=...) e
        # combinação fonte+ativo que ninguém repetia ficavam para sempre — vazamento
        # lento em processo de vida longa e um jeito barato de inflar a memória à
        # distância. Agora, ao passar do teto, o vencido sai primeiro e depois saem as
        # entradas que vencem mais cedo (as menos úteis).
        if len(_cache_financeiro) > MAX_ENTRADAS_CACHE:
            _podar_cache()
    return valor


def _podar_cache():
    """
    Descarta o vencido e, se ainda faltar espaço, o que vence mais cedo. Só é
    chamada de dentro de _cache_guardar, com a trava já tomada.
    """
    agora = time.time()
    for chave in [c for c, (_, expira) in _cache_financeiro.items() if expira < agora]:
        _cache_financeiro.pop(chave, None)

    excedente = len(_cache_financeiro) - MAX_ENTRADAS_CACHE
    if excedente > 0:
        mais_proximas_de_vencer = sorted(_cache_financeiro, key=lambda c: _cache_financeiro[c][1])
        for chave in mais_proximas_de_vencer[:excedente]:
            _cache_financeiro.pop(chave, None)


def _float(valor):
    """Converte para float aceitando strings das APIs; devolve None se não der."""
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return None
    if numero != numero or numero in (float("inf"), float("-inf")):  # NaN/inf
        return None
    return numero


def _normalizar(texto):
    """Minúsculas e sem acentos — para casar 'dólar', 'PETR4' e 'petr4' igual."""
    sem_acento = unicodedata.normalize("NFKD", str(texto or ""))
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return sem_acento.lower()


def _formatar_numero(valor, casas=2):
    if valor is None:
        return "n/d"
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "@").replace(".", ",").replace("@", ".")


def _formatar_variacao(valor):
    if valor is None:
        return "n/d"
    sinal = "+" if valor > 0 else ""
    return f"{sinal}{_formatar_numero(valor, 2)}%"


def _pedir_json(url, params=None, timeout=TIMEOUT_FONTES, headers=None):
    """
    GET isolado: erro de rede, cota estourada (403/429) ou resposta estranha
    viram FonteIndisponivel — nunca exceção que chegue ao chat.
    """
    try:
        resposta = requests.get(
            url,
            params=params,
            timeout=timeout,
            headers=headers or {"User-Agent": "OnTrack/1.0 (agente-economia-python)"},
        )
    except requests.exceptions.RequestException as erro:
        raise FonteIndisponivel(f"falha de conexão ({type(erro).__name__})") from None

    if resposta.status_code in (403, 429):
        # cota estourada / plano sem acesso ao recurso: descarta a fonte por um tempo
        raise FonteIndisponivel(
            f"HTTP {resposta.status_code} (cota ou plano da API)",
            cooldown=COOLDOWN_QUOTA_FINANCEIRA,
        )
    if resposta.status_code >= 400:
        raise FonteIndisponivel(f"HTTP {resposta.status_code}")

    try:
        return resposta.json()
    except ValueError:
        raise FonteIndisponivel("resposta em formato inesperado") from None


# ---------------------------------------------------------------------------
# ATIVOS CITADOS NA MENSAGEM
# ---------------------------------------------------------------------------

# nome normalizado (sem acento, minúsculo) -> (símbolo, tipo, rótulo)
ATIVOS_CONHECIDOS = {
    # ações brasileiras
    "petr4": ("PETR4", "acao_b3", "Petrobras PN"),
    "petrobras": ("PETR4", "acao_b3", "Petrobras PN"),
    "petr3": ("PETR3", "acao_b3", "Petrobras ON"),
    "vale3": ("VALE3", "acao_b3", "Vale ON"),
    "itub4": ("ITUB4", "acao_b3", "Itaú Unibanco PN"),
    "itau": ("ITUB4", "acao_b3", "Itaú Unibanco PN"),
    "bradesco": ("BBDC4", "acao_b3", "Bradesco PN"),
    "bbdc4": ("BBDC4", "acao_b3", "Bradesco PN"),
    "banco do brasil": ("BBAS3", "acao_b3", "Banco do Brasil ON"),
    "bbas3": ("BBAS3", "acao_b3", "Banco do Brasil ON"),
    "ambev": ("ABEV3", "acao_b3", "Ambev ON"),
    "abev3": ("ABEV3", "acao_b3", "Ambev ON"),
    "magazine luiza": ("MGLU3", "acao_b3", "Magazine Luiza ON"),
    "mglu3": ("MGLU3", "acao_b3", "Magazine Luiza ON"),
    "weg3": ("WEGE3", "acao_b3", "WEG ON"),
    "petrorio": ("PRIO3", "acao_b3", "PetroRio ON"),
    "prio3": ("PRIO3", "acao_b3", "PetroRio ON"),
    # ações internacionais
    "apple": ("AAPL", "acao_us", "Apple"),
    "aapl": ("AAPL", "acao_us", "Apple"),
    "microsoft": ("MSFT", "acao_us", "Microsoft"),
    "msft": ("MSFT", "acao_us", "Microsoft"),
    "google": ("GOOGL", "acao_us", "Alphabet/Google"),
    "alphabet": ("GOOGL", "acao_us", "Alphabet/Google"),
    "googl": ("GOOGL", "acao_us", "Alphabet/Google"),
    "amazon": ("AMZN", "acao_us", "Amazon"),
    "amzn": ("AMZN", "acao_us", "Amazon"),
    "tesla": ("TSLA", "acao_us", "Tesla"),
    "tsla": ("TSLA", "acao_us", "Tesla"),
    "nvidia": ("NVDA", "acao_us", "Nvidia"),
    "nvda": ("NVDA", "acao_us", "Nvidia"),
    "netflix": ("NFLX", "acao_us", "Netflix"),
    "nflx": ("NFLX", "acao_us", "Netflix"),
    "nubank": ("NU", "acao_us", "Nubank"),
    # índices
    "ibovespa": ("^BVSP", "indice", "Ibovespa"),
    "ibov": ("^BVSP", "indice", "Ibovespa"),
    "dow jones": ("^DJI", "indice", "Dow Jones"),
    "s&p 500": ("^GSPC", "indice", "S&P 500"),
    "s&p": ("^GSPC", "indice", "S&P 500"),
    "nasdaq": ("^IXIC", "indice", "Nasdaq Composite"),
    # moedas
    "dolar": ("USDBRL", "moeda", "Dólar comercial (USD/BRL)"),
    "euro": ("EURBRL", "moeda", "Euro (EUR/BRL)"),
    "libra esterlina": ("GBPBRL", "moeda", "Libra esterlina (GBP/BRL)"),
    # cripto
    "bitcoin": ("BTC-USD", "cripto", "Bitcoin (BTC/USD)"),
    "btc": ("BTC-USD", "cripto", "Bitcoin (BTC/USD)"),
    "ethereum": ("ETH-USD", "cripto", "Ethereum (ETH/USD)"),
    "eth": ("ETH-USD", "cripto", "Ethereum (ETH/USD)"),
    # indicadores macro (HG Brasil Finance)
    "selic": ("SELIC", "macro", "Taxa Selic"),
    "cdi": ("CDI", "macro", "CDI"),
}

# nomes que também são palavras comuns em português/inglês: só valem como ativo
# quando a mensagem tem contexto de mercado ("cotação de bitcoin" x "meta de inflação")
NOMES_AMBIGUOS = {"btc", "eth", "ibov", "vale", "s&p", "amazon", "apple"}

PALAVRAS_CONTEXTO_MERCADO = [
    "acao", "acoes", "cotacao", "cotacoes", "preco", "precos", "papel", "papeis",
    "mercado", "bolsa", "b3", "ticker", "grafico", "quanto", "subiu", "caiu",
    "alta", "baixa", "comprar", "vender", "investir", "investimento", "dividendo",
    "criptomoeda", "cripto", "valorizou", "desvalorizou", "hoje",
]

MOEDAS_ISO = {"USD", "EUR", "GBP", "JPY", "ARS", "CAD", "AUD", "CNY", "CHF", "BRL"}
PADRAO_TICKER_B3 = re.compile(r"^[A-Z]{4}\d{1,2}$")

ROTULOS_B3 = {
    "PETR4": "Petrobras PN", "PETR3": "Petrobras ON", "VALE3": "Vale ON",
    "ITUB4": "Itaú Unibanco PN", "BBDC4": "Bradesco PN", "BBAS3": "Banco do Brasil ON",
    "ABEV3": "Ambev ON", "MGLU3": "Magazine Luiza ON", "WEGE3": "WEG ON",
    "PRIO3": "PetroRio ON",
}

# ticker do catálogo v2 do HG Brasil (formato {fonte}:{símbolo}) para cada índice.
# O S&P 500 só existe aqui: o painel clássico não lista esse índice.
MAPA_HG_INDICES = {
    "^BVSP": "INDEXB3:IBOV",
    "IFIX": "INDEXB3:IFIX",
    "^IXIC": "INDEXNASDAQ:IXIC",
    "^DJI": "INDEXNYSE:DJI",
    "^GSPC": "INDEXNYSE:SPX",
}

# chave do MESMO índice no painel clássico (/finance -> results.stocks)
MAPA_HG_PAINEL = {
    "^BVSP": "IBOVESPA",
    "IFIX": "IFIX",
    "^IXIC": "NASDAQ",
    "^DJI": "DOWJONES",
}


def extrair_ativos(mensagem, maximo=MAX_ATIVOS_POR_MENSAGEM):
    """
    Descobre quais ativos a mensagem cita: tickers da B3 (petr4, VALE3), pares de
    moeda (USD/BRL), nomes conhecidos (dólar, ibovespa, bitcoin) e $AAPL.
    """
    original = str(mensagem or "")
    texto = _normalizar(original)
    tem_contexto_mercado = any(p in texto for p in PALAVRAS_CONTEXTO_MERCADO)
    ativos = []

    def adicionar(simbolo, tipo, rotulo):
        if len(ativos) >= maximo:
            return
        if all(atual["simbolo"] != simbolo for atual in ativos):
            ativos.append({"simbolo": simbolo, "tipo": tipo, "rotulo": rotulo})

    try:
        # 1) tickers da B3 escritos na mensagem (PETR4, mglu3, ...)
        for bruto in re.findall(r"\b([A-Za-z]{4}\d{1,2})\b", original):
            simbolo = bruto.upper()
            if PADRAO_TICKER_B3.match(simbolo):
                adicionar(simbolo, "acao_b3", ROTULOS_B3.get(simbolo, simbolo))

        # 2) pares de moeda com separador (USD/BRL, eur-brl)
        for base, _, cotacao in re.findall(r"\b([A-Za-z]{3})([-/])([A-Za-z]{3})\b", original):
            base, cotacao = base.upper(), cotacao.upper()
            if base in MOEDAS_ISO and cotacao in MOEDAS_ISO and cotacao == "BRL" and base != "BRL":
                adicionar(f"{base}BRL", "moeda", f"{base}/BRL")

        # 3) nomes conhecidos (dólar, petrobras, ibovespa, bitcoin, selic...)
        for nome, (simbolo, tipo, rotulo) in ATIVOS_CONHECIDOS.items():
            if not re.search(r"(?<![a-z0-9])" + re.escape(nome) + r"(?![a-z0-9])", texto):
                continue
            if nome in NOMES_AMBIGUOS and not tem_contexto_mercado:
                continue
            adicionar(simbolo, tipo, rotulo)

        # 4) tickers com cifrão no estilo americano ($AAPL)
        for bruto in re.findall(r"\$([A-Za-z]{1,5})\b", original):
            simbolo = bruto.upper()
            if len(simbolo) >= 2:
                adicionar(simbolo, "acao_us", ROTULOS_B3.get(simbolo, simbolo))
    except Exception:
        # extração é heurística: qualquer falha significa "nenhum ativo"
        return []

    return ativos


def _unidade_do_ativo(ativo):
    if ativo["tipo"] == "acao_b3":
        return "BRL"
    if ativo["tipo"] == "acao_us" or ativo["tipo"] == "cripto":
        return "USD"
    if ativo["tipo"] == "indice":
        return "pontos"
    if ativo["tipo"] == "macro":
        return "% ao ano"
    return "BRL"  # moeda cotada em reais


def _simbolo_yfinance(ativo):
    simbolo = ativo["simbolo"]
    if ativo["tipo"] == "acao_b3":
        return f"{simbolo}.SA"          # B3 no Yahoo Finance
    if ativo["tipo"] == "moeda":
        return f"{simbolo}=X"           # USDBRL -> USDBRL=X
    return simbolo                      # ^BVSP, AAPL, BTC-USD


def _simbolo_twelve_data(ativo):
    tipo, simbolo = ativo["tipo"], ativo["simbolo"]
    if tipo == "moeda":
        return f"{simbolo[:3]}/{simbolo[3:]}"
    if tipo == "cripto":
        return f"{simbolo.split('-')[0]}/USD"
    if tipo == "indice" or tipo == "macro":
        # índices brasileiros não estão disponíveis no plano gratuito
        raise FonteIndisponivel("tipo de ativo não suportado pelo Twelve Data")
    return simbolo  # PETR4 (Bovespa) e AAPL: a exchange da B3 entra só como retry


def _simbolo_finnhub(ativo):
    tipo, simbolo = ativo["tipo"], ativo["simbolo"]
    if tipo == "acao_us":
        return simbolo
    if tipo == "cripto":
        return f"BINANCE:{simbolo.split('-')[0]}USDT"
    # ações da B3 (403) e câmbio (403) não estão no plano gratuito do Finnhub
    raise FonteIndisponivel("ativo fora do plano gratuito do Finnhub")


# ---------------------------------------------------------------------------
# CONSULTA A CADA FONTE (cada uma com try/except próprio, falha silenciosa)
# ---------------------------------------------------------------------------

def _cotacao_yfinance(ativo):
    if yf is None:
        raise FonteIndisponivel("biblioteca yfinance não instalada")

    simbolo = _simbolo_yfinance(ativo)
    try:
        historico = yf.Ticker(simbolo).history(period="5d", interval="1d")
    except Exception as erro:
        raise FonteIndisponivel(f"yfinance falhou ({type(erro).__name__})") from None

    if historico is None or len(historico) == 0:
        raise FonteIndisponivel(f"sem histórico para {simbolo}")

    try:
        ultima = historico.iloc[-1]
        preco = _float(ultima.get("Close"))
        anterior = _float(historico.iloc[-2].get("Close")) if len(historico) > 1 else None
        alta = _float(ultima.get("High"))
        baixa = _float(ultima.get("Low"))
    except Exception as erro:
        raise FonteIndisponivel(f"yfinance: histórico ilegível ({type(erro).__name__})") from None

    if not preco:
        raise FonteIndisponivel(f"yfinance: preço inválido para {simbolo}")

    variacao = None
    if anterior:
        variacao = (preco / anterior - 1) * 100

    return {
        "fonte": "yfinance",
        "simbolo": simbolo,
        "preco": preco,
        "variacao_percentual": variacao,
        "alta": alta,
        "baixa": baixa,
        "unidade": _unidade_do_ativo(ativo),
    }


def _finance_hgbrasil():
    """Painel padrão do HG Brasil (câmbio, índices, bitcoin e Selic/CDI), com cache."""
    cache = _cache_ler(("hgbrasil", "finance"))
    if cache is not None:
        return cache

    if not HGBRASIL_API_KEY:
        raise FonteIndisponivel("HG Brasil: chave não configurada")

    dados = _pedir_json(HG_PAINEL_URL, params={"key": HGBRASIL_API_KEY})
    resultados = dados.get("results") or {}
    if not resultados:
        raise FonteIndisponivel("HG Brasil: painel vazio")
    return _cache_guardar(("hgbrasil", "finance"), resultados)


def _hg_quotes(tickers):
    """
    Uma consulta ao catálogo atual do HG Brasil (v2): 'B3:PETR4', 'FOREX:USDBRL',
    'INDEXNYSE:SPX'... A resposta vem em results[] com quote.value/change_percent,
    market.high/low e dividends.yield_12m_percent.
    """
    if not HGBRASIL_API_KEY:
        raise FonteIndisponivel("HG Brasil: chave não configurada")

    dados = _pedir_json(HG_QUOTES_URL, params={"key": HGBRASIL_API_KEY, "tickers": tickers})
    if not isinstance(dados, dict) or dados.get("valid_key") is False:
        raise FonteIndisponivel("HG Brasil: chave recusada")

    resultados = dados.get("results")
    if isinstance(resultados, dict) and resultados.get("error"):
        raise FonteIndisponivel(
            f"HG Brasil: {str(resultados.get('message') or 'consulta recusada')[:80]}"
        )
    if isinstance(resultados, list):
        for item in resultados:
            if isinstance(item, dict) and _float((item.get("quote") or {}).get("value")):
                return item
    raise FonteIndisponivel(f"HG Brasil: sem cotação para {tickers}")


def _cotacao_do_item_hg(item, ativo, simbolo=None):
    """Converte um item de results[] do HG Brasil na cotação interna do OnTrack."""
    quote = item.get("quote") or {}
    mercado = item.get("market") or {}
    cotacao = {
        "fonte": "HG Brasil",
        "simbolo": simbolo or str(item.get("symbol") or ativo["simbolo"]),
        "preco": _float(quote.get("value")),
        "variacao_percentual": _float(quote.get("change_percent")),
        "alta": _float(mercado.get("high")),
        "baixa": _float(mercado.get("low")),
        "unidade": _unidade_do_ativo(ativo),
    }
    if ativo["tipo"] in ("acao_b3", "acao_us"):
        # o HG manda o dividend yield de 12 meses junto da cotação: é o único dado de
        # provento que o contexto do chat tem, e evita a IA responder dividendo
        # "de cabeça" (ou pior, com trecho de código)
        cotacao["dividendos_12m"] = _float(
            (item.get("dividends") or {}).get("yield_12m_percent")
        )
    return cotacao


def _cotacao_hgbrasil(ativo):
    """
    Cotação pelo HG Brasil. Ação da B3, câmbio e índice saem do catálogo atual (v2),
    que é o caminho documentado hoje: a rota antiga de ações (/finance/stock_price)
    exigia plano pago e devolvia erro para toda ação da B3 — era o motivo de a
    integração do HG Brasil 'nunca trazer cotação'. O painel clássico fica como
    segunda tentativa de câmbio e de índice e como fonte de BTC e Selic/CDI.
    """
    tipo, simbolo = ativo["tipo"], ativo["simbolo"]

    if tipo == "acao_b3":
        return _cotacao_do_item_hg(_hg_quotes(f"B3:{simbolo}"), ativo)

    if tipo == "acao_us":
        # o catálogo do HG Brasil é a B3 (ação, FII, ETF e BDR): pedir ação dos EUA
        # aqui só queimaria uma requisição para receber erro
        raise FonteIndisponivel("HG Brasil: sem catálogo de ações internacionais")

    if tipo == "moeda":
        try:
            return _cotacao_do_item_hg(_hg_quotes(f"FOREX:{simbolo}"), ativo)
        except FonteIndisponivel:
            pass  # painel clássico: cobre as principais moedas contra o real

    if tipo == "indice":
        ticker = MAPA_HG_INDICES.get(simbolo)
        if not ticker:
            raise FonteIndisponivel("HG Brasil: índice não disponível")
        try:
            return _cotacao_do_item_hg(_hg_quotes(ticker), ativo)
        except FonteIndisponivel:
            pass  # painel clássico: Ibovespa, IFIX, Nasdaq e Dow Jones

    return _cotacao_do_painel_hgbrasil(_finance_hgbrasil(), ativo)


def _cotacao_do_painel_hgbrasil(painel, ativo):
    """Câmbio, índices, bitcoin e Selic/CDI do painel clássico /finance."""
    tipo, simbolo = ativo["tipo"], ativo["simbolo"]

    if tipo == "moeda":
        base, cotacao = simbolo[:3], simbolo[3:]
        if cotacao != "BRL":
            raise FonteIndisponivel("HG Brasil: câmbio disponível apenas contra o real")
        moeda = (painel.get("currencies") or {}).get(base)
        if not moeda:
            raise FonteIndisponivel(f"HG Brasil: moeda {base} não listada")
        preco = _float(moeda.get("buy")) or _float(moeda.get("sell"))
        if not preco:
            raise FonteIndisponivel("HG Brasil: cotação de moeda indisponível")
        return {
            "fonte": "HG Brasil",
            "simbolo": simbolo,
            "preco": preco,
            "variacao_percentual": _float(moeda.get("variation")),
            "alta": None,
            "baixa": None,
            "unidade": "BRL",
        }

    if tipo == "cripto":
        base = simbolo.split("-")[0]
        if base == "BTC":
            bitcoin = (painel.get("bitcoin") or {}).get("blockchain_info") or {}
            preco = _float(bitcoin.get("last")) or _float(bitcoin.get("buy"))
            variacao = _float(bitcoin.get("variation"))
        elif base in (painel.get("currencies") or {}):
            moeda = painel["currencies"][base]
            preco = _float(moeda.get("buy")) or _float(moeda.get("sell"))
            variacao = _float(moeda.get("variation"))
        else:
            raise FonteIndisponivel(f"HG Brasil: cripto {base} não listada")
        if not preco:
            raise FonteIndisponivel("HG Brasil: cotação de cripto indisponível")
        return {
            "fonte": "HG Brasil",
            "simbolo": simbolo,
            "preco": preco,
            "variacao_percentual": variacao,
            "alta": None,
            "baixa": None,
            "unidade": "USD",
        }

    if tipo == "indice":
        chave = MAPA_HG_PAINEL.get(simbolo)
        if not chave:
            raise FonteIndisponivel("HG Brasil: índice não disponível")
        indice = (painel.get("stocks") or {}).get(chave)
        if not indice:
            raise FonteIndisponivel(f"HG Brasil: índice {chave} não listado")
        pontos = _float(indice.get("points"))
        if not pontos:
            raise FonteIndisponivel("HG Brasil: índice sem pontuação")
        return {
            "fonte": "HG Brasil",
            "simbolo": simbolo,
            "preco": pontos,
            "variacao_percentual": _float(indice.get("variation")),
            "alta": None,
            "baixa": None,
            "unidade": "pontos",
        }

    if tipo == "macro":
        impostos = painel.get("taxes") or []
        if not impostos:
            raise FonteIndisponivel("HG Brasil: taxas indisponíveis")
        taxa = _float((impostos[0] or {}).get(simbolo.lower()))
        if taxa is None:
            raise FonteIndisponivel(f"HG Brasil: taxa {simbolo} indisponível")
        return {
            "fonte": "HG Brasil",
            "simbolo": simbolo,
            "preco": taxa,
            "variacao_percentual": None,
            "alta": None,
            "baixa": None,
            "unidade": "% ao ano",
        }

    raise FonteIndisponivel("HG Brasil: tipo de ativo não suportado")


def _cotacao_finnhub(ativo):
    if not FINNHUB_API_KEY:
        raise FonteIndisponivel("Finnhub: chave não configurada")

    simbolo = _simbolo_finnhub(ativo)  # pode levantar FonteIndisponivel (fora do plano)
    dados = _pedir_json(
        "https://finnhub.io/api/v1/quote",
        params={"symbol": simbolo, "token": FINNHUB_API_KEY},
    )

    if dados.get("error"):
        raise FonteIndisponivel(f"Finnhub: {str(dados.get('error'))[:80]}")

    preco = _float(dados.get("c"))
    if not preco:
        # acontece quando o plano não cobre o ativo (ex.: B3 devolve zeros)
        raise FonteIndisponivel(f"Finnhub: sem cotação para {simbolo}")

    return {
        "fonte": "Finnhub",
        "simbolo": simbolo,
        "preco": preco,
        "variacao_percentual": _float(dados.get("dp")),
        "alta": _float(dados.get("h")),
        "baixa": _float(dados.get("l")),
        "unidade": _unidade_do_ativo(ativo),
    }


def _cotacao_twelve_data(ativo):
    if not TWELVE_DATA_API_KEY:
        raise FonteIndisponivel("Twelve Data: chave não configurada")

    simbolo = _simbolo_twelve_data(ativo)  # pode levantar FonteIndisponivel
    parametros = {"symbol": simbolo, "apikey": TWELVE_DATA_API_KEY}
    dados = _pedir_json(URL_TWELVE_QUOTE, params=parametros)
    if not isinstance(dados, dict):
        raise FonteIndisponivel("Twelve Data: resposta em formato inesperado")

    if (dados.get("status") == "error" or dados.get("code")) and ativo["tipo"] == "acao_b3":
        # o ticker da B3 sozinho pode não ser suficiente para o Twelve Data resolver o
        # papel: a segunda tentativa diz a exchange pelo nome que a própria API usa
        # (B3 Bovespa, MIC BVMF). Só acontece quando a primeira recusa, então no caminho
        # normal continua sendo uma requisição por ativo — e a fonte deixa de depender
        # de o ticker nu resolver sozinho
        parametros["exchange"] = "Bovespa"
        dados = _pedir_json(URL_TWELVE_QUOTE, params=parametros)
        if not isinstance(dados, dict):
            raise FonteIndisponivel("Twelve Data: resposta em formato inesperado")

    if dados.get("status") == "error" or dados.get("code"):
        raise FonteIndisponivel(f"Twelve Data: {str(dados.get('message'))[:80]}")

    preco = _float(dados.get("close"))
    if not preco:
        raise FonteIndisponivel(f"Twelve Data: sem cotação para {simbolo}")

    return {
        "fonte": "Twelve Data",
        "simbolo": simbolo,
        "preco": preco,
        "variacao_percentual": _float(dados.get("percent_change")),
        "alta": _float(dados.get("high")),
        "baixa": _float(dados.get("low")),
        "unidade": _unidade_do_ativo(ativo),
    }


# Catálogo das fontes: nome canônico (o mesmo rótulo que aparece no bloco 'Dados de
# mercado') -> função de consulta. A ordem aqui é só a ordem de LEITURA do bloco.
FONTES_FINANCEIRAS = (
    ("yfinance", _cotacao_yfinance),
    ("HG Brasil", _cotacao_hgbrasil),
    ("Finnhub", _cotacao_finnhub),
    ("Twelve Data", _cotacao_twelve_data),
)
ORDEM_PADRAO = tuple(nome for nome, _ in FONTES_FINANCEIRAS)
FUNCOES_FONTES = dict(FONTES_FINANCEIRAS)

# Ordem de consulta POR TIPO DE ATIVO. Antes havia uma ordem única, com o yfinance
# sempre na frente, e como a primeira fonte que responde encerra a busca do ativo as
# outras praticamente nunca eram acionadas: o HG Brasil só entrava quando o yfinance
# falhava e o Twelve Data/Finnhub não eram chamados em pergunta de ação da B3 — daí a
# impressão (correta) de integração configurada que nunca é usada. A ordem é por
# competência de cada uma:
#
#   * HG Brasil — catálogo B3/FOREX/índices da API v2, que é o único que traz o
#     dividend yield de 12 meses da ação, e o painel clássico, única fonte de
#     Selic/CDI. Um painel só responde câmbio, índices e taxas, então ele vem
#     primeiro onde é forte.
#   * yfinance — gratuito e sem chave, bom em ação (B3 e EUA) e cripto; segue
#     primeiro na B3 para não queimar cota das APIs com chave em pergunta de preço.
#   * Finnhub — ação dos EUA e cripto (B3 e câmbio ficam fora do plano gratuito).
#   * Twelve Data — B3 e ação dos EUA; recusa índices e indicadores macro.
# Tipo que não esteja aqui cai em ORDEM_PADRAO e, em qualquer caso, as fontes que
# faltarem entram como fallback no fim da fila (ver _ordem_fontes): uma integração
# nova passa a ser consultada sem editar esta tabela.
ORDEM_POR_TIPO = {
    "moeda": ("HG Brasil", "yfinance", "Twelve Data"),
    "indice": ("HG Brasil", "yfinance"),
    "macro": ("HG Brasil",),
    "acao_b3": ("yfinance", "HG Brasil", "Twelve Data"),
    "acao_us": ("yfinance", "Finnhub", "Twelve Data"),
    "cripto": ("yfinance", "Finnhub", "Twelve Data", "HG Brasil"),
}

# Como o usuário pode pedir uma fonte na própria pergunta. O apelido aponta para o nome
# canônico — o mesmo rótulo que aparece no bloco 'Dados de mercado' — porque é por ele
# que a rotação de fontes e o prompt do sistema se entendem.
APELIDOS_FONTES = (
    ("finnhub", "Finnhub"),
    ("twelve data", "Twelve Data"),
    ("twelvedata", "Twelve Data"),
    ("twelve", "Twelve Data"),
    ("hg brasil", "HG Brasil"),
    ("hgbrasil", "HG Brasil"),
    ("hg finance", "HG Brasil"),
    ("hg", "HG Brasil"),
    ("yfinance", "yfinance"),
    ("yahoo finance", "yfinance"),
    ("yahoo", "yfinance"),
)


def fonte_citada(mensagem):
    """
    Nome canônico da fonte de mercado pedida explicitamente na mensagem, ou None.

    A busca é por palavra inteira de propósito: sem as fronteiras, o apelido "hg"
    casaria dentro de um ticker como HGLG11 (FII da B3) e o pedido viraria uma
    consulta ao HG Brasil sem ninguém ter pedido.
    """
    texto = _normalizar(mensagem)
    for apelido, canonico in APELIDOS_FONTES:
        if re.search(rf"\b{re.escape(apelido)}\b", texto):
            return canonico
    return None


# Pergunta sobre provento: é o caso em que o HG Brasil passa na frente para ação da
# B3, porque é a única integração do contexto que traz um número de dividendo — o
# yield de 12 meses que vem junto da cotação no catálogo v2.
PALAVRAS_PROVENTOS = (
    "dividend", "provento", "jcp", "juros sobre capital", "bonificacao", "data-com",
)


def pergunta_sobre_proventos(mensagem):
    """True quando a pergunta é sobre dividendo/JCP/bonificação (texto normalizado)."""
    texto = _normalizar(mensagem)
    return any(palavra in texto for palavra in PALAVRAS_PROVENTOS)


def _consultar_fonte(nome, funcao, ativo, forcar=False):
    """
    Executa UMA fonte para UM ativo, isolando toda falha: cota estourada,
    timeout, conexão, símbolo fora do plano ou resposta estranha apenas fazem
    esta consulta devolver None. Nada é mostrado ao usuário.

    `forcar` vale para a fonte pedida pelo usuário na própria pergunta: nesse caso a
    consulta ignora o cache negativo e o cooldown. Os dois são marcas de silêncio de
    uma tentativa anterior (rede, cota, plano) e não podem esconder justamente a fonte
    que ele mandou usar. O cache POSITIVO continua valendo — é o dado daquela mesma
    fonte, e reaproveitá-lo é o que segura o consumo das APIs gratuitas.
    """
    chave_cache = ("cotacao", nome, ativo["simbolo"], ativo["tipo"])
    em_cache = _cache_ler(chave_cache)
    if em_cache is not None:
        return em_cache

    chave_cooldown = (nome, ativo["simbolo"], ativo["tipo"])
    chave_falha = ("falha", nome, ativo["simbolo"], ativo["tipo"])
    if not forcar:
        if _fontes_bloqueadas.get(chave_cooldown, 0.0) > time.time():
            return None  # fonte em silêncio depois de 403/429

        # cache negativo: evita repetir a chamada de uma fonte que já recusou este ativo
        # (ex.: HG Brasil sem plano para ações), que era custo fixo em toda mensagem
        if _cache_ler(chave_falha) is not None:
            return None

    _medidor_contar("fontes")  # só a consulta que realmente sai conta

    try:
        cotacao = funcao(ativo)
    except FonteIndisponivel as erro:
        if erro.cooldown:
            # cota/plano estourado: vale mais que o cache negativo, já tem seu tempo
            _fontes_bloqueadas[chave_cooldown] = time.time() + erro.cooldown
        else:
            _cache_guardar(chave_falha, True, ttl=CACHE_TTL_FALHAS)
        return None
    except Exception:
        # rede, parsing, biblioteca de terceiros: nunca derruba o backend
        _cache_guardar(chave_falha, True, ttl=CACHE_TTL_FALHAS)
        return None

    if not cotacao or not cotacao.get("preco"):
        return None
    return _cache_guardar(chave_cache, cotacao)


def _ordem_fontes(ativo, preferida=None):
    """
    Fontes na ordem em que devem ser tentadas para ESTE ativo: primeiro a `preferida`
    (a fonte que o usuário pediu ou a que traz o dado que a pergunta precisa, como o
    dividend yield do HG Brasil em pergunta de proventos), depois a ordem do tipo do
    ativo (ORDEM_POR_TIPO) e, por fim, as fontes que não aparecem em nenhuma das duas
    — assim uma integração nova entra como fallback em todos os tipos em vez de ficar
    sem uso sem ninguém notar.
    """
    candidatas = (preferida,) + tuple(ORDEM_POR_TIPO.get(ativo["tipo"], ())) + ORDEM_PADRAO
    ordem = []
    for nome in candidatas:
        if nome and nome in FUNCOES_FONTES and nome not in ordem:
            ordem.append(nome)
    return ordem


def _coletar_cotacoes(ativo, fonte_unica=None, preferida=None):
    """
    Consulta as fontes de um ativo e devolve só o que respondeu, sempre na ordem de
    FONTES_FINANCEIRAS (o paralelismo não pode mudar a ordem do contexto).

    Sem `fonte_unica`, a primeira fonte da fila daquele tipo de ativo é consultada
    sozinha; com UMA_FONTE_POR_ATIVO ela encerra a busca, porque as demais são o
    fallback — é o que corta a maior parte das requisições às APIs gratuitas.

    Com `fonte_unica`, SÓ essa fonte é consultada e não há fallback: o usuário pediu
    por ela e o número tem de ser dela. Sem dado, o ativo simplesmente não entra no
    bloco (a IA é instruída a avisar, em vez de trocar de provedor em silêncio). Como
    o pedido foi explícito, essa consulta passa por cima do cache negativo e do
    cooldown — uma falha (ou uma recusa de plano) de antes não pode esconder a
    integração que ele mandou usar.
    """
    if fonte_unica:
        funcao = FUNCOES_FONTES.get(fonte_unica)
        if not funcao:
            return []
        try:
            cotacao = _consultar_fonte(fonte_unica, funcao, ativo, forcar=True)
        except Exception:
            cotacao = None
        return [cotacao] if cotacao else []

    resultados = {}
    fila = _ordem_fontes(ativo, preferida)

    # 1) a fonte preferida do ativo, sozinha: no caso comum é uma requisição só
    if fila:
        nome = fila[0]
        try:
            cotacao = _consultar_fonte(nome, FUNCOES_FONTES[nome], ativo)
        except Exception:
            cotacao = None
        if cotacao:
            resultados[nome] = cotacao
            if UMA_FONTE_POR_ATIVO:
                return [cotacao]

    # 2) o yfinance, quando não foi o preferido, roda sozinho no thread da requisição:
    # a biblioteca mantém sessão própria e não foi feita para dois threads ao mesmo tempo
    restantes = fila[1:]
    if "yfinance" in restantes:
        try:
            cotacao = _consultar_fonte("yfinance", _cotacao_yfinance, ativo)
        except Exception:
            cotacao = None
        if cotacao:
            resultados["yfinance"] = cotacao
            if UMA_FONTE_POR_ATIVO:
                return [cotacao]
        restantes = [nome for nome in restantes if nome != "yfinance"]

    # 3) as APIs REST restantes, em paralelo: são independentes entre si e a soma dos
    # timeouts delas era o que inflava a espera quando alguma fonte estava lenta
    if restantes:
        with ThreadPoolExecutor(max_workers=len(restantes)) as executor:
            futuros = {
                executor.submit(_consultar_fonte, nome, FUNCOES_FONTES[nome], ativo): nome
                for nome in restantes
            }
            for futuro, nome in futuros.items():
                try:
                    cotacao = futuro.result()
                except Exception:
                    cotacao = None
                if cotacao:
                    resultados[nome] = cotacao

    return [resultados[nome] for nome, _ in FONTES_FINANCEIRAS if nome in resultados]


def _linha_cotacao(cotacao):
    partes = [f"{_formatar_numero(cotacao.get('preco'))} {cotacao.get('unidade', '')}".strip()]
    partes.append(f"variação do dia {_formatar_variacao(cotacao.get('variacao_percentual'))}")
    if cotacao.get("alta") is not None:
        partes.append(f"alta {_formatar_numero(cotacao.get('alta'))}")
    if cotacao.get("baixa") is not None:
        partes.append(f"baixa {_formatar_numero(cotacao.get('baixa'))}")
    dividendos = cotacao.get("dividendos_12m")
    if dividendos:  # 0 significa "sem provento no período": não polui a linha
        partes.append(f"dividend yield de 12 meses {_formatar_numero(dividendos)}%")
    return " | ".join(partes)


def coletar_dados_financeiros(mensagem):
    """
    Consulta as fontes dos ativos citados e devolve:

      {"bloco": "<contexto para a IA>", "fontes": (nomes...), "ativos": [...]}

    Se a mensagem citar explicitamente uma fonte ("usando o Finnhub", "pelo Twelve"),
    só ela é consultada — ver fonte_citada(). Sem citação, vale o fluxo normal: a
    primeira fonte que responder dá o número e as outras ficam de fallback.

    Nunca levanta exceção: sem ativos reconhecidos, ou com todas as fontes
    falhando, devolve campos vazios e o prompt segue limpo para a IA.
    """
    resultado = {"bloco": "", "fontes": (), "ativos": []}

    try:
        ativos = extrair_ativos(mensagem)
    except Exception:
        return resultado
    if not ativos:
        return resultado

    fonte_unica = fonte_citada(mensagem)
    # pergunta de proventos: para ação da B3 o HG Brasil passa na frente (uma
    # requisição, e só nesse tipo de pergunta), porque é ele que traz o dividend yield
    # de 12 meses junto da cotação — sem isso o contexto não tem nenhum número de
    # dividendo e a IA responde sobre provento sem dado nenhum
    quer_proventos = pergunta_sobre_proventos(mensagem)
    dados_por_ativo = []
    fontes_usadas = []

    for ativo in ativos:
        preferida = "HG Brasil" if quer_proventos and ativo["tipo"] == "acao_b3" else None
        cotacoes = _coletar_cotacoes(ativo, fonte_unica, preferida)
        for cotacao in cotacoes:
            if cotacao["fonte"] not in fontes_usadas:
                fontes_usadas.append(cotacao["fonte"])
        if cotacoes:
            dados_por_ativo.append((ativo, cotacoes))

    if not dados_por_ativo:
        # todas as fontes falharam: a IA responde com conhecimento geral
        if fonte_unica:
            # fonte pedida pelo usuário sem dado: o bloco existe só para a IA avisar
            # isso, em vez de responder com o número de outro provedor
            return {
                "bloco": (
                    f"Dados de mercado: foi solicitada a fonte {fonte_unica} nesta "
                    "pergunta, mas ela não retornou cotação para os ativos citados. "
                    "Informe isso ao usuário e não substitua por números de outra fonte."
                ),
                "fontes": (),
                "ativos": ativos,
            }
        return {"bloco": "", "fontes": (), "ativos": ativos}

    # O bloco montado vai para o cache pelo conjunto de ativos: sem isso o timestamp
    # de minuto mudava o texto a cada 60s e invalidava o cache de resposta da IA — que
    # é justamente o passo caro da mensagem. Os números já vêm do cache de cotações.
    # a fonte pedida entra na chave: sem isso, um bloco só com números do Finnhub
    # (ou só com os do yfinance) seria reaproveitado de uma pergunta anterior. O
    # assunto entra pelo mesmo motivo: o bloco de uma pergunta de proventos traz um
    # dado (o dividend yield) que o bloco de uma pergunta de preço não tem
    chave_bloco = (
        "bloco",
        fonte_unica,
        "proventos" if quer_proventos else "cotacao",
        tuple((ativo["simbolo"], ativo["tipo"]) for ativo, _ in dados_por_ativo),
    )
    em_cache = _cache_ler(chave_bloco)
    if em_cache is not None:
        # cópia rasa: o dicionário guardado não pode ser alterado pelo chamador
        return dict(em_cache, ativos=ativos)

    agora = datetime.now().strftime("%d/%m/%Y às %H:%M")
    linhas = [
        f"Dados de mercado coletados agora ({agora}, horário local) em fontes "
        "públicas, com pequena defasagem natural de cada provedor:"
    ]
    for ativo, cotacoes in dados_por_ativo:
        tipo_legivel = {
            "acao_b3": "ação B3", "acao_us": "ação internacional", "indice": "índice",
            "moeda": "câmbio", "cripto": "criptoativo", "macro": "indicador macro",
        }.get(ativo["tipo"], ativo["tipo"])
        linhas.append(f"- {ativo['rotulo']} ({tipo_legivel}):")
        for cotacao in cotacoes:
            linhas.append(f"    • {cotacao['fonte']}: {_linha_cotacao(cotacao)}")
    linhas.append(
        "Use esses valores como cotação recente, cite a fonte de cada número e, "
        "onde as fontes divergirem, deixe claro que há divergência entre provedores. "
        "Quando a linha trouxer 'dividend yield de 12 meses', esse é o único número de "
        "provento disponível: use-o ao falar de dividendos e não invente a série "
        "histórica nem escreva código para obtê-la."
    )

    resultado["bloco"] = "\n".join(linhas)
    resultado["fontes"] = tuple(fontes_usadas)
    resultado["ativos"] = ativos
    # o timestamp fica congelado junto com os números: os dois descrevem a mesma
    # coleta, então a idade do dado nunca aparece menor do que é
    return _cache_guardar(chave_bloco, resultado, ttl=CACHE_TTL_COTACOES)


# ===========================================================================
# NOTÍCIAS DO MERCADO (GNews + Marketaux, agregadas no SERVIDOR)
#
# As duas APIs são consultadas aqui, com as chaves de back-end/.env: nenhuma chave
# de notícia vai para o navegador (que a veria no DevTools) nem para o repositório.
# O frontend pede GET /api/noticias?q=...&page=... e recebe a lista já unificada, no
# mesmo formato que ele desenhava (title/description/url/publishedAt/source) mais o
# campo 'provedor', ordenada da mais recente para a mais antiga e sem repetição — a
# mesma matéria costuma sair nas duas fontes.
#
# Como nas fontes de mercado, cada API é isolada: cota estourada (401/403/429),
# timeout ou JSON inesperado apenas tiram aquela fonte do resultado, e a outra segue
# respondendo. Nenhuma exceção chega ao navegador.
# ===========================================================================

GNEWS_URL = "https://gnews.io/api/v4/search"
MARKETAUX_URL = "https://api.marketaux.com/v1/news/all"
TIMEOUT_NOTICIAS = 8
# Teto por requisição de cada fonte: a GNews entrega no máximo 10 no plano gratuito
# e a Marketaux limita bem abaixo disso — os dois conjuntos são somados e ordenados.
MAX_NOTICIAS_POR_FONTE = 10
CACHE_TTL_NOTICIAS = 10 * 60   # 10 min: o feed não muda de minuto em minuto


def _noticias_json(url, params):
    """
    GET isolado das APIs de notícias: devolve None quando a fonte falha, em vez de
    levantar. Os parâmetros nunca aparecem em mensagem de erro, resposta ou log,
    porque a chave viaja na query string.
    """
    try:
        _medidor_contar("fontes")  # requisição externa desta consulta
        return _pedir_json(url, params=params, timeout=TIMEOUT_NOTICIAS)
    except Exception:
        return None


def _noticia_normalizada(titulo, descricao, url, publicado_em, fonte, provedor):
    """Card no formato que o frontend já sabia desenhar (o mesmo da GNews)."""
    return {
        "title": str(titulo or "").strip(),
        "description": str(descricao or "").strip(),
        "url": str(url or "").strip(),
        "publishedAt": str(publicado_em or "").strip(),
        "source": {"name": str(fonte or provedor).strip()},
        "provedor": provedor,   # selo da fonte no card: GNews ou Marketaux
    }


def _noticias_gnews(termo, pagina, maximo):
    """(notícias, total, erro) da GNews; erro None quando a fonte respondeu."""
    if not GNEWS_API_KEY:
        return [], 0, "chave não configurada"

    dados = _noticias_json(GNEWS_URL, {
        "q": termo,
        "lang": "pt",
        "max": maximo,
        "page": pagina,
        "apikey": GNEWS_API_KEY,
    })
    if not isinstance(dados, dict):
        return [], 0, "não respondeu"

    artigos = dados.get("articles")
    if not isinstance(artigos, list):
        return [], 0, str(dados.get("errors") or "resposta em formato inesperado")

    noticias = []
    for artigo in artigos:
        if not isinstance(artigo, dict):
            continue
        origem = artigo.get("source")
        noticias.append(_noticia_normalizada(
            artigo.get("title"),
            artigo.get("description"),
            artigo.get("url"),
            artigo.get("publishedAt"),
            origem.get("name") if isinstance(origem, dict) else origem,
            "GNews",
        ))
    return noticias, int(dados.get("totalArticles") or 0), None


def _noticias_marketaux(termo, pagina, maximo):
    """(notícias, total, erro) da Marketaux; erro None quando a fonte respondeu."""
    if not MARKETAUX_API_KEY:
        return [], 0, "chave não configurada"

    dados = _noticias_json(MARKETAUX_URL, {
        "search": termo,
        "language": "pt",
        "limit": maximo,
        "page": pagina,
        "api_token": MARKETAUX_API_KEY,
    })
    if not isinstance(dados, dict):
        return [], 0, "não respondeu"

    artigos = dados.get("data")
    if not isinstance(artigos, list):
        erro = dados.get("error")
        return [], 0, str(
            (erro.get("message") if isinstance(erro, dict) else erro)
            or "resposta em formato inesperado"
        )[:160]

    noticias = []
    for artigo in artigos:
        if not isinstance(artigo, dict):
            continue
        noticias.append(_noticia_normalizada(
            artigo.get("title"),
            artigo.get("description") or artigo.get("snippet"),
            artigo.get("url"),
            artigo.get("published_at"),
            artigo.get("source"),
            "Marketaux",
        ))

    meta = dados.get("meta") if isinstance(dados.get("meta"), dict) else {}
    return noticias, int(meta.get("found") or 0), None


def _referencia_da_noticia(noticia):
    """Chave de deduplicação: a URL sem protocolo/barra final (ou o próprio título)."""
    referencia = str(noticia.get("url") or noticia.get("title") or "").strip()
    return re.sub(r"^https?://", "", referencia, flags=re.IGNORECASE).rstrip("/").lower()


def buscar_noticias(termo="economia", pagina=1, maximo=MAX_NOTICIAS_POR_FONTE):
    """
    Junta GNews e Marketaux numa lista única: mais recentes primeiro, sem matéria
    repetida e com o provedor marcado em cada card. Uma fonte fora do ar não impede
    a resposta da outra; o resultado fica em cache por 10 minutos para não gastar
    cota a cada pesquisa repetida.
    """
    termo = str(termo or "").strip()[:120] or "economia"
    try:
        pagina = max(int(pagina), 1)
    except (TypeError, ValueError):
        pagina = 1
    try:
        maximo = min(max(int(maximo), 1), MAX_NOTICIAS_POR_FONTE)
    except (TypeError, ValueError):
        maximo = MAX_NOTICIAS_POR_FONTE

    chave_cache = ("noticias", _normalizar(termo), pagina, maximo)
    em_cache = _cache_ler(chave_cache)
    if em_cache is not None:
        return em_cache

    # sequencial, nunca em paralelo: a mesma regra das fontes de mercado
    noticias = []
    vistas = set()
    fontes = []
    falhas = []
    maior_total = 0

    for nome, buscar in (("GNews", _noticias_gnews), ("Marketaux", _noticias_marketaux)):
        lista, total, erro = buscar(termo, pagina, maximo)
        if erro:
            falhas.append({"fonte": nome, "motivo": erro})
            continue
        fontes.append(nome)
        maior_total = max(maior_total, total)
        for noticia in lista:
            referencia = _referencia_da_noticia(noticia)
            if not noticia["title"] or not noticia["url"] or referencia in vistas:
                continue
            vistas.add(referencia)
            noticias.append(noticia)

    # ISO 8601 ordena corretamente como texto; sem data a notícia vai para o fim
    noticias.sort(key=lambda item: item["publishedAt"], reverse=True)

    resultado = {
        "ok": bool(fontes),   # False apenas quando NENHUMA fonte respondeu
        "noticias": noticias,
        "total": maior_total,
        "pagina": pagina,
        "por_pagina": maximo,
        "fontes": fontes,
        "falhas": falhas,
    }
    if not fontes:
        # falha das duas não entra em cache: a próxima pesquisa tenta de novo
        return resultado
    return _cache_guardar(chave_cache, resultado, ttl=CACHE_TTL_NOTICIAS)


# ===========================================================================
# 2) MOTOR DE IAs (rotação sequencial, nunca em paralelo)
#
# A rotação continua existindo, mas hoje ela tem um único item: o Claude. Ordem
# histórica dos provedores (todos preservados no código e bloqueados na entrada):
# Gemini principal -> reservas -> Mistral -> Cloudflare Workers AI -> OpenRouter.
# ===========================================================================

# ---------------------------------------------------------------------------
# PROVEDORES DE IA AUTORIZADOS
#
# Só quem está listado aqui consegue fazer requisição: esta tupla é a trava de
# entrada de cada função de provedor, e a ordem da rotação sai de _candidatos_ia()
# (a tupla abaixo repete essa ordem, para a leitura bater com a cadeia real). Com
# todos os identificadores presentes o fallback funciona por inteiro: Gemini
# principal -> reservas -> Mistral -> Cloudflare -> OpenRouter -> Claude. Para tirar
# um provedor do ar sem apagar o código, basta remover o identificador dele daqui:
# a rotação deixa de acioná-lo e a trava de entrada dele passa a recusar qualquer
# chamada direta.
# ---------------------------------------------------------------------------

PROVEDORES_IA_AUTORIZADOS = (
    "gemini_principal",    # Gemini com a chave principal
    "gemini_reserva_1",    # Gemini com GEMINI_API_KEY_2
    "gemini_reserva_2",    # Gemini com GEMINI_API_KEY_3
    "mistral",             # Mistral
    "cloudflare",          # Cloudflare Workers AI
    "openrouter",          # OpenRouter (percorre MODELOS_OPENROUTER_FALLBACK)
    "claude",              # Anthropic — último da cadeia (último recurso)
)


def _ia_autorizada(identificador):
    """True só para o provedor explicitamente autorizado a fazer requisição."""
    return identificador in PROVEDORES_IA_AUTORIZADOS


# --- Claude (Anthropic): provedor ativo -------------------------------------
# O endpoint e a versão da API são fixos (a versão é exigida por contrato pelo
# serviço). O modelo é trocável por CLAUDE_MODEL no .env, sem mexer no código.
URL_CLAUDE = "https://api.anthropic.com/v1/messages"
CLAUDE_VERSION = "2023-06-01"
CLAUDE_MODEL = (os.getenv("CLAUDE_MODEL") or "").strip() or "claude-sonnet-5"
CLAUDE_MAX_TOKENS = 4096
TIMEOUT_CLAUDE = 15

MODEL_NAME = "gemini-3.5-flash"
# Modelo das duas chaves de RESERVA do Gemini: a versão Flash Lite. O mapeamento
# das chaves não muda — GEMINI_API_KEY_2 e GEMINI_API_KEY_3 continuam sendo as
# reservas 1 e 2 —, só o modelo que elas chamam. A cota gratuita do Google é por
# modelo, então a reserva em outro modelo ainda tem balde próprio quando o
# principal esgotou o dia. Trocável sem mexer no código: GEMINI_MODEL_RESERVA.
MODEL_NAME_RESERVA_GEMINI = (
    os.getenv("GEMINI_MODEL_RESERVA") or ""
).strip() or "gemini-2.5-flash-lite"
API_URL_GEMINI = (
    "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
)
URL_OPENROUTER = "https://openrouter.ai/api/v1/chat/completions"
URL_MISTRAL = "https://api.mistral.ai/v1/chat/completions"
# Cloudflare Workers AI: endpoint por conta. O modelo padrão é o Llama 3.1 8B
# Instruct (rápido e barato no plano gratuito), trocável por CLOUDFLARE_MODEL sem
# mexer no código.
URL_CLOUDFLARE = "https://api.cloudflare.com/client/v4/accounts/{conta}/ai/run/{modelo}"
CLOUDFLARE_MODEL = (
    os.getenv("CLOUDFLARE_MODEL") or ""
).strip() or "@cf/meta/llama-3.1-8b-instruct"
TIMEOUT_CLOUDFLARE = 15
# Modelo da Mistral na rotação: o alias "mistral-small-latest" aponta sempre para a
# versão atual, rápida e mais barata da família small. Pode ser trocado por outra
# (ex.: mistral-large-latest) só pela variável MISTRAL_MODEL, sem mexer no código.
MISTRAL_MODEL = os.getenv("MISTRAL_MODEL", "mistral-small-latest").strip() or "mistral-small-latest"

# Ordem de tentativa dos modelos gratuitos do OpenRouter: do mais rápido/especializado
# para o mais pesado. O primeiro que responde com sucesso vence e encerra a rotação;
# quem falha sai da lista por um tempo (404, que é condição permanente, sai por horas),
# então o custo de um modelo morto aqui é pago uma vez por reinício, não por mensagem.
MODELOS_OPENROUTER_FALLBACK = [
    "inclusionai/ling-3.0-flash-fin:free",     # ~2.0s — especializado em mercado e finanças
    "deepseek/deepseek-v4-flash-0731:free",    # alta capacidade analítica, contexto e matemática
    "google/gemma-4-26b-a4b-it:free",          # resposta rápida e leve
    "z-ai/glm-5.2:free",                       # raciocínio denso e tabelas
    "google/gemma-4-31b-it:free",              # Gemma 4 robusto
    "cohere/north-mini-code:free",             # leve e eficiente para estruturação/JSON
    "openrouter/free",                         # roteador automático de menor fila
    "nvidia/nemotron-3-ultra-550b-a55b:free",  # fallback de suporte de grande porte
    # "meta-llama/llama-3.3-70b-instruct:free",  # fora da rotação: responde 404
    #                                              "This model is unavailable for free"
]

# indisponibilidade temporária do próprio serviço (exige nova tentativa)
STATUS_TRANSITORIOS = {500, 502, 503, 504}
# status que liberam a rotação para o próximo provedor: cota/indisponibilidade
# (429/503), chave inválida ou modelo inexistente (401/403/404) e 5xx
STATUS_QUE_ATIVAM_ROTACAO = {400, 401, 403, 404, 429} | STATUS_TRANSITORIOS

MAX_TENTATIVAS_GEMINI = 2      # era 3: com 3 chaves do Gemini + OpenRouter, repetir
                               # a mesma chave raramente é o melhor uso do tempo
ESPERA_INICIAL_SEGUNDOS = 1.5
TIMEOUT_CONEXAO = 5            # abrir conexão é sempre rápido: esperar mais que isso
                               # só acontece quando a rede/serviço está fora
# Tempo máximo por tentativa no Gemini. Medição: com a chave saudável ele responde
# em 1-6s, e a chave reserva lenta levava ~11s — logo 8s já entrega a resposta
# saudável e corta o custo da chave que está pendurada (era 12s).
TIMEOUT_GEMINI = 8
# O OpenRouter roda o prompt real (system + contexto + cotações, ~6 mil caracteres)
# e os modelos gratuitos levam de 5 a 13s nele.
TIMEOUT_OPENROUTER = 15
# A Mistral roda o mesmo prompt real; a família small costuma responder em 2 a 8s.
TIMEOUT_MISTRAL = 12
# Orçamento da rotação inteira. Nenhuma tentativa começa se não couber aqui, então
# este é o teto real da espera da IA (era 60s, enquanto o cliente tolerava 180s).
LIMITE_TOTAL_ROTACAO = 30
# Não vale iniciar uma tentativa que não tenha esta folga no orçamento: ela morreria
# no timeout e a resposta chegaria depois de o usuário desistir.
FOLGA_MINIMA = 3
# Modelo que respondeu 404 saiu do ar de vez (ex.: deixou de ser gratuito): sai da
# lista por horas em vez de ser descoberto de novo a cada mensagem.
COOLDOWN_MODELO_MORTO = 6 * 60 * 60
# Resposta idêntica reaproveitada (mesma memória, mesmo contexto, mesmas cotações no
# prompt): a chamada de IA era o passo mais caro da mensagem, de 5 a 30s. O bloco de
# cotações já expira em 5 min, então 2 min de cache é conservador para dado de mercado.
CACHE_TTL_RESPOSTA_IA = 120
# Provedor que estourou timeout ou erro sai da rotação por um tempo. Sem isso, cada
# mensagem pagava de novo os 30s da chave pendurada — era esse o maior gargalo.
COOLDOWN_PROVEDOR_FALHA = 90

COOLDOWN_QUOTA_PADRAO = 60
COOLDOWN_QUOTA_DIARIA = 5 * 60  # PISO de espera: cota diária esgotada agora espera até a
                                # renovação real (meia-noite do Pacífico), não 5 minutos

# Cooldown por provedor: quando um devolve 429/timeout/erro, ele fica fora da
# rotação por um tempo e as próximas mensagens começam tentando os outros.
#
# Esse estado é gravado em disco entre execuções: descobrir de novo, a cada
# reinício, que uma chave está pendurada custava ~40s na PRIMEIRA mensagem.
def _carregar_estado_ias():
    estado = {"provedores": {}, "modelos_openrouter": {}}
    try:
        with open(ARQUIVO_ESTADO_IAS, "r", encoding="utf-8") as f:
            salvo = json.load(f) or {}
    except (OSError, json.JSONDecodeError):
        return estado

    agora = time.time()
    for secao in estado:
        for chave, valor in (salvo.get(secao) or {}).items():
            try:
                expira_em = float(valor)
            except (TypeError, ValueError):
                continue
            if expira_em > agora:  # só o que ainda está valendo
                estado[secao][chave] = expira_em
    return estado


def _salvar_estado_ias():
    """Grava os cooldowns ativos (uma escrita por mensagem, no fim da rotação)."""
    try:
        agora = time.time()
        dados = {
            "provedores": {k: v for k, v in _quota_bloqueada_ate.items() if v > agora},
            "modelos_openrouter": {k: v for k, v in _modelos_openrouter_bloqueados.items() if v > agora},
        }
        temporario = f"{ARQUIVO_ESTADO_IAS}.tmp"
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        os.replace(temporario, ARQUIVO_ESTADO_IAS)
    except (OSError, ValueError):
        pass


_estado_ias_inicial = _carregar_estado_ias()
_quota_bloqueada_ate = _estado_ias_inicial["provedores"]
_ultimo_provedor = ""  # rótulo do provedor que respondeu por último (diagnóstico)


class ProvedorIndisponivel(RuntimeError):
    """O provedor não conseguiu responder; a rotação deve tentar o próximo."""

    def __init__(self, motivo="", quota=False):
        super().__init__(motivo)
        self.motivo = motivo
        self.quota = quota


class CotaDoGemini(RuntimeError):
    """Todos os provedores com cota disponível recusaram por limite de uso (HTTP 429)."""


def _restante(prazo):
    """Segundos que ainda cabem no orçamento da rotação (None = sem prazo definido)."""
    if prazo is None:
        return None
    return prazo - time.time()


def _cabe_no_orcamento(prazo):
    """True quando ainda vale iniciar uma tentativa (None = sem prazo definido)."""
    restante = _restante(prazo)
    return restante is None or restante >= FOLGA_MINIMA


def _timeout_da_tentativa(teto, prazo):
    """Timeout (conexão, leitura) nunca maior que o orçamento que ainda resta."""
    restante = _restante(prazo)
    if restante is None:
        return (TIMEOUT_CONEXAO, teto)
    return (TIMEOUT_CONEXAO, max(1.0, min(teto, restante)))


class _RespostaCurta:
    """
    Resposta mínima (status + corpo já lido) com a mesma cara da resposta do
    `requests`, para que o tratamento de erro/cota continue igual.
    """

    def __init__(self, status, corpo):
        self.status_code = status
        self._corpo = corpo

    @property
    def text(self):
        return self._corpo.decode("utf-8", "replace")

    def json(self):
        return json.loads(self._corpo)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(
                f"{self.status_code} Error", response=self
            )


def _post_em_pedacos(url, cabecalhos, payload, teto, prazo):
    """
    POST limitado pelo relógio, não só pelo timeout do socket.

    `requests` só oferece timeout por operação (conexão/leitura), e provedor que
    envia o corpo aos poucos enquanto o modelo gera — é o que o OpenRouter faz —
    segurava a conexão por mais de 50s mesmo com timeout de 15s. Aqui o corpo é
    lido em pedaços e a conexão é fechada assim que o prazo acaba, o que também
    cancela a geração do outro lado em vez de deixá-la rodando à toa.
    """
    limite = _timeout_da_tentativa(teto, prazo)
    fim = time.time() + limite[1]
    _medidor_contar("ia")
    resposta = requests.post(
        url, headers=cabecalhos, json=payload, timeout=limite, stream=True
    )
    try:
        pedacos = []
        for pedaco in resposta.iter_content(chunk_size=8192):
            pedacos.append(pedaco)
            if time.time() > fim:
                raise requests.exceptions.Timeout("prazo da tentativa esgotado")
        return _RespostaCurta(resposta.status_code, b"".join(pedacos))
    finally:
        resposta.close()


def _chave_cache_ia(prompt_usuario, instrucao_sistema):
    """Chave do cache de resposta: mesmo system + mesmo prompt final = mesma resposta."""
    resumo = hashlib.sha256(
        f"{instrucao_sistema}\x00{prompt_usuario}".encode("utf-8")
    ).hexdigest()
    return ("resposta_ia", resumo)


def _candidatos_ia():
    """
    Provedores que a rotação sabe acionar, na ordem de tentativa, com o identificador
    que a trava de entrada usa. Os gratuitos vêm primeiro e o Claude fecha a fila;
    cada chamada recebe o prazo da rotação para não estourar o orçamento total.
    """
    return (
        (
            "gemini_principal",
            "Gemini principal",
            lambda prompt, instrucao, prazo: _chamar_gemini_com_chave(
                prompt, instrucao, GEMINI_API_KEY, "gemini_principal",
                contar_cota=True, prazo=prazo,
            ),
        ),
        (
            "gemini_reserva_1",
            "Gemini reserva 1",
            lambda prompt, instrucao, prazo: _chamar_gemini_com_chave(
                prompt, instrucao, GEMINI_API_KEY_2, "gemini_reserva_1", prazo=prazo, modelo=MODEL_NAME_RESERVA_GEMINI
            ),
        ),
        (
            "gemini_reserva_2",
            "Gemini reserva 2",
            lambda prompt, instrucao, prazo: _chamar_gemini_com_chave(
                prompt, instrucao, GEMINI_API_KEY_3, "gemini_reserva_2", prazo=prazo, modelo=MODEL_NAME_RESERVA_GEMINI
            ),
        ),
        ("mistral", "Mistral", _chamar_mistral),
        ("cloudflare", "Cloudflare", _chamar_cloudflare),
        ("openrouter", "OpenRouter", _chamar_openrouter),
        # último da cadeia: só é acionado se todos os gratuitos não responderem
        ("claude", "Claude", _chamar_claude),
    )


def _provedores_ia():
    """
    Rotação efetiva: os candidatos na ordem de _candidatos_ia(), menos os que
    estiverem fora de PROVEDORES_IA_AUTORIZADOS. Com a tupla completa, a cadeia
    inteira de fallback vale: Gemini principal -> reservas -> Mistral -> Cloudflare
    -> OpenRouter -> Claude, sempre um provedor por vez (nunca em paralelo).
    """
    return tuple(
        (rotulo, chamada)
        for identificador, rotulo, chamada in _candidatos_ia()
        if _ia_autorizada(identificador)
    )


def _espera_do_429_claude(resposta):
    """
    Espera do 429 do Claude: a API manda o retry-after em segundos no cabeçalho.
    Sem cabeçalho legível vale o piso padrão de cota.
    """
    try:
        segundos = int(float(resposta.headers.get("retry-after") or 0))
    except (AttributeError, TypeError, ValueError):
        segundos = 0
    return max(segundos, COOLDOWN_QUOTA_PADRAO)


def _chamar_claude(prompt_usuario, instrucao_sistema, prazo=None):
    """
    Chamada à API do Claude (Anthropic, POST /v1/messages) — o provedor ativo.

    Uma única requisição por mensagem, no mesmo desenho da Mistral: erro de rede,
    cota (429), erro de chave/modelo ou resposta ilegível viram
    ProvedorIndisponivel com cooldown, sem repetir a chamada — repetir aqui
    duplicaria tokens em cima de uma falha. O corpo é lido em pedaços por
    _post_em_pedacos, então a requisição morre junto com o orçamento da rotação.
    """
    if not _ia_autorizada("claude"):
        raise ProvedorIndisponivel("Claude desativado por configuração")

    if not CLAUDE_API_KEY:
        raise ProvedorIndisponivel(
            "CLAUDE_API_KEY não configurada no .env do back-end"
        )

    if _quota_bloqueada_ate.get("claude", 0.0) > time.time():
        raise ProvedorIndisponivel("em espera por cota do Claude", quota=True)

    if not _cabe_no_orcamento(prazo):
        raise ProvedorIndisponivel("orçamento de tempo da rotação esgotado")

    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": CLAUDE_VERSION,
        "Content-Type": "application/json",
    }
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": CLAUDE_MAX_TOKENS,
        "temperature": 0.4,
        "system": instrucao_sistema,
        "messages": [{"role": "user", "content": prompt_usuario}],
    }

    try:
        resposta = _post_em_pedacos(URL_CLAUDE, headers, payload, TIMEOUT_CLAUDE, prazo)
    except requests.exceptions.RequestException as erro:
        _quota_bloqueada_ate["claude"] = time.time() + COOLDOWN_PROVEDOR_FALHA
        raise ProvedorIndisponivel(f"não respondeu ({type(erro).__name__})") from None

    detalhe = (resposta.text or "").strip()[:200]
    if resposta.status_code in STATUS_QUE_ATIVAM_ROTACAO or resposta.status_code >= 400:
        _quota_bloqueada_ate["claude"] = time.time() + (
            _espera_do_429_claude(resposta) if resposta.status_code == 429
            else COOLDOWN_PROVEDOR_FALHA
        )
        raise ProvedorIndisponivel(
            f"HTTP {resposta.status_code}: {detalhe}",
            quota=(resposta.status_code == 429),
        )

    try:
        dados = resposta.json()
    except ValueError:
        _quota_bloqueada_ate["claude"] = time.time() + COOLDOWN_PROVEDOR_FALHA
        raise ProvedorIndisponivel(f"resposta não é JSON: {detalhe}") from None

    # formato de sucesso: {"content": [{"type": "text", "text": "..."}, ...]}
    conteudo = dados.get("content")
    texto = ""
    if isinstance(conteudo, list):
        texto = "".join(
            str(bloco.get("text") or "")
            for bloco in conteudo
            if isinstance(bloco, dict) and bloco.get("type") == "text"
        )

    if not texto.strip():
        erro = dados.get("error")
        motivo = (erro.get("message") if isinstance(erro, dict) else erro) or detalhe
        _quota_bloqueada_ate["claude"] = time.time() + COOLDOWN_PROVEDOR_FALHA
        raise ProvedorIndisponivel(f"resposta vazia ou com erro: {str(motivo)[:160]}")

    registrar_chamada_ia("claude")  # contador do painel de diagnóstico
    return texto


def _chamar_gemini_com_chave(prompt_usuario, instrucao_sistema, chave, identificador, contar_cota=False, prazo=None, modelo=None):
    """
    Uma chamada ao Gemini com uma chave específica. Erros que indicam "esta
    chave não vai responder agora" viram ProvedorIndisponivel (a rotação
    assume); nenhuma exceção bruta sobe daqui.
    """
    if not _ia_autorizada(identificador):
        raise ProvedorIndisponivel(
            f"{identificador} desativado: só o Claude pode fazer requisição"
        )

    if not chave:
        raise ProvedorIndisponivel("chave não configurada", quota=False)

    bloqueado_ate = _quota_bloqueada_ate.get(identificador, 0.0)
    restante = int(bloqueado_ate - time.time())
    if restante > 0:
        raise ProvedorIndisponivel(
            f"em espera por cota (libera em ~{restante}s)", quota=True
        )

    if not _cabe_no_orcamento(prazo):
        raise ProvedorIndisponivel("orçamento de tempo da rotação esgotado")

    payload = {
        "contents": [{"parts": [{"text": prompt_usuario}]}],
        "systemInstruction": {"parts": [{"text": instrucao_sistema}]},
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": chave}
    url = API_URL_GEMINI.format(modelo=modelo or MODEL_NAME)

    espera = ESPERA_INICIAL_SEGUNDOS

    for tentativa in range(1, MAX_TENTATIVAS_GEMINI + 1):
        # repetir a mesma chave precisa caber no orçamento: sem isso, o backoff de
        # uma chave em 503 consumia mais tempo que a resposta inteira
        if tentativa > 1 and not _cabe_no_orcamento(prazo):
            break
        try:
            resposta = _post_em_pedacos(url, headers, payload, TIMEOUT_GEMINI, prazo)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as erro:
            # sem resposta: não vale insistir. Sai da rotação por um tempo, senão
            # toda mensagem seguinte paga o mesmo timeout inteiro de novo.
            _quota_bloqueada_ate[identificador] = time.time() + COOLDOWN_PROVEDOR_FALHA
            raise ProvedorIndisponivel(
                f"não respondeu ({type(erro).__name__})"
            ) from None
        except requests.exceptions.RequestException as erro:
            _quota_bloqueada_ate[identificador] = time.time() + COOLDOWN_PROVEDOR_FALHA
            raise ProvedorIndisponivel(f"falha de rede ({type(erro).__name__})") from None

        if resposta.status_code == 429:
            # limite de uso/cota desta chave: entra em cooldown e a rotação continua
            mensagem, cooldown, limite_diario = mensagem_e_cooldown_de_quota(resposta)
            _quota_bloqueada_ate[identificador] = time.time() + cooldown
            if limite_diario:
                aprender_limite_diario(
                    limite_diario, esgotado=(identificador == "gemini_principal")
                )
            raise ProvedorIndisponivel(mensagem, quota=True)

        if resposta.status_code in STATUS_TRANSITORIOS:
            # indisponibilidade temporária (503 etc.): backoff e nova tentativa
            if tentativa < MAX_TENTATIVAS_GEMINI:
                time.sleep(espera)
                espera *= 2
                continue
            _quota_bloqueada_ate[identificador] = time.time() + COOLDOWN_PROVEDOR_FALHA
            raise ProvedorIndisponivel(f"indisponível (HTTP {resposta.status_code})")

        if resposta.status_code in STATUS_QUE_ATIVAM_ROTACAO:
            # 401/403 (chave sem permissão), 404 (modelo inexistente), 400: é erro de
            # configuração — repetir a cada mensagem só gasta tempo
            _quota_bloqueada_ate[identificador] = time.time() + COOLDOWN_PROVEDOR_FALHA
            raise ProvedorIndisponivel(
                f"recusou a chamada (HTTP {resposta.status_code})"
            )

        try:
            resposta.raise_for_status()
        except requests.exceptions.HTTPError:
            raise ProvedorIndisponivel(f"HTTP {resposta.status_code}") from None

        if contar_cota:
            registrar_chamada_gemini()  # só a chamada bem-sucedida consome a cota local
        # contador do painel do chat: vale para as três chaves do Gemini
        registrar_chamada_ia(identificador)

        try:
            dados = resposta.json()
        except ValueError:
            raise ProvedorIndisponivel("resposta não é JSON") from None

        try:
            texto = dados["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise ProvedorIndisponivel("resposta em formato inesperado") from None

        if not str(texto).strip():
            raise ProvedorIndisponivel("resposta vazia")

        return str(texto)

    raise ProvedorIndisponivel("não foi possível obter resposta")


_ultimo_modelo_openrouter = ""
# modelo que falhou sai da lista por um tempo (evita pagar de novo o timeout dele);
# recuperado do disco junto com os cooldowns dos provedores
_modelos_openrouter_bloqueados = _estado_ias_inicial["modelos_openrouter"]


def _chamar_openrouter(prompt_usuario, instrucao_sistema, prazo=None):
    """
    Última tentativa da rotação: OpenRouter, um modelo por vez, na ordem da
    lista, até um responder com sucesso.
    """
    global _ultimo_modelo_openrouter

    if not _ia_autorizada("openrouter"):
        raise ProvedorIndisponivel(
            "OpenRouter desativado: só o Claude pode fazer requisição"
        )

    if not OPENROUTER_API_KEY:
        raise ProvedorIndisponivel("chave não configurada")

    if _quota_bloqueada_ate.get("openrouter", 0.0) > time.time():
        raise ProvedorIndisponivel("em espera por cota do OpenRouter", quota=True)

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        # metadados de atribuição do OpenRouter: o domínio de produção, não localhost
        "HTTP-Referer": os.environ.get("SITE_URL", "https://on-track-sage.vercel.app"),
        "X-Title": "OnTrack",
    }

    for modelo in MODELOS_OPENROUTER_FALLBACK:
        if _modelos_openrouter_bloqueados.get(modelo, 0.0) > time.time():
            continue
        # cada modelo gratuita leva de 5 a 13s neste prompt: não adianta começar um
        # que não caiba no que resta do orçamento
        if not _cabe_no_orcamento(prazo):
            break
        payload = {
            "model": modelo,
            "messages": [
                {"role": "system", "content": instrucao_sistema},
                {"role": "user", "content": prompt_usuario},
            ],
            "temperature": 0.4,
            "max_tokens": 4096,
        }
        try:
            resposta = _post_em_pedacos(
                URL_OPENROUTER, headers, payload, TIMEOUT_OPENROUTER, prazo
            )
        except requests.exceptions.RequestException:
            # este modelo falhou: sai da lista por um tempo e tenta o próximo
            _modelos_openrouter_bloqueados[modelo] = time.time() + COOLDOWN_PROVEDOR_FALHA
            continue

        if resposta.status_code in STATUS_QUE_ATIVAM_ROTACAO:
            if resposta.status_code == 429:
                # O 429 do OpenRouter pode ser cota DIÁRIA ("Rate limit exceeded:
                # free-models-per-day"): insistir de minuto em minuto com os 8 modelos
                # é o mesmo desperdício que existia no Gemini
                detalhe = (resposta.text or "").lower()
                diario = "per-day" in detalhe or "per day" in detalhe
                _quota_bloqueada_ate["openrouter"] = time.time() + (
                    _segundos_ate_renovar_utc() if diario else COOLDOWN_QUOTA_PADRAO
                )
            # 404 de modelo é condição permanente ("unavailable for free"): sai da
            # lista por horas, senão a rotação redescobre isso a cada mensagem
            espera = (
                COOLDOWN_MODELO_MORTO
                if resposta.status_code == 404
                else COOLDOWN_PROVEDOR_FALHA
            )
            _modelos_openrouter_bloqueados[modelo] = time.time() + espera
            continue

        if resposta.status_code >= 400:
            _modelos_openrouter_bloqueados[modelo] = time.time() + COOLDOWN_PROVEDOR_FALHA
            continue

        try:
            dados = resposta.json()
        except ValueError:
            continue

        if dados.get("error"):
            continue

        try:
            texto = dados["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            continue

        if str(texto or "").strip():
            _ultimo_modelo_openrouter = modelo
            registrar_chamada_ia("openrouter")  # contador do painel de cotas do chat
            return str(texto)

    raise ProvedorIndisponivel("nenhum modelo do OpenRouter respondeu")


def _chamar_mistral(prompt_usuario, instrucao_sistema, prazo=None):
    """
    Penúltima tentativa da rotação: Mistral (API compatível com o formato OpenAI).
    Uma única chamada, sem repetir a requisição: 429/5xx/erro de rede entram em
    cooldown e a rotação segue para o próximo provedor — o mesmo desenho do
    OpenRouter, que evita gastar cota insistindo em quem acabou de falhar.
    """
    if not _ia_autorizada("mistral"):
        raise ProvedorIndisponivel(
            "Mistral desativada: só o Claude pode fazer requisição"
        )

    if not MISTRAL_API_KEY:
        raise ProvedorIndisponivel("chave não configurada")

    if _quota_bloqueada_ate.get("mistral", 0.0) > time.time():
        raise ProvedorIndisponivel("em espera por cota da Mistral", quota=True)

    if not _cabe_no_orcamento(prazo):
        raise ProvedorIndisponivel("orçamento de tempo da rotação esgotado")

    headers = {
        "Authorization": f"Bearer {MISTRAL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": instrucao_sistema},
            {"role": "user", "content": prompt_usuario},
        ],
        "temperature": 0.4,
        "max_tokens": 4096,
    }
    try:
        resposta = _post_em_pedacos(URL_MISTRAL, headers, payload, TIMEOUT_MISTRAL, prazo)
    except requests.exceptions.RequestException as erro:
        _quota_bloqueada_ate["mistral"] = time.time() + COOLDOWN_PROVEDOR_FALHA
        raise ProvedorIndisponivel(f"não respondeu ({type(erro).__name__})") from None

    detalhe = (resposta.text or "")[:120]
    if resposta.status_code in STATUS_QUE_ATIVAM_ROTACAO or resposta.status_code >= 400:
        _quota_bloqueada_ate["mistral"] = time.time() + (
            COOLDOWN_QUOTA_PADRAO if resposta.status_code == 429
            else COOLDOWN_PROVEDOR_FALHA
        )
        raise ProvedorIndisponivel(
            f"HTTP {resposta.status_code}: {detalhe}",
            quota=(resposta.status_code == 429),
        )

    try:
        dados = resposta.json()
        texto = dados["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        _quota_bloqueada_ate["mistral"] = time.time() + COOLDOWN_PROVEDOR_FALHA
        raise ProvedorIndisponivel(
            f"resposta em formato inesperado: {detalhe}"
        ) from None

    if not str(texto or "").strip():
        raise ProvedorIndisponivel("resposta vazia")
    registrar_chamada_ia("mistral")  # contador do painel de cotas do chat
    return str(texto)


def _chamar_cloudflare(prompt_usuario, instrucao_sistema, prazo=None):
    """
    Cloudflare Workers AI (formato de mensagens igual ao da OpenAI). Mesmo desenho da
    Mistral: UMA única chamada, sem repetir a requisição — 429/5xx/erro de rede entram
    em cooldown e a rotação segue para o próximo provedor, em vez de queimar a cota
    gratuita insistindo em quem acabou de falhar.
    """
    if not _ia_autorizada("cloudflare"):
        raise ProvedorIndisponivel(
            "Cloudflare desativado: só o Claude pode fazer requisição"
        )

    if not CLOUDFLARE_API_TOKEN or not CLOUDFLARE_ACCOUNT_ID:
        raise ProvedorIndisponivel("token/account id do Cloudflare não configurados")

    if _quota_bloqueada_ate.get("cloudflare", 0.0) > time.time():
        raise ProvedorIndisponivel("em espera por cota do Cloudflare", quota=True)

    if not _cabe_no_orcamento(prazo):
        raise ProvedorIndisponivel("orçamento de tempo da rotação esgotado")

    headers = {
        "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messages": [
            {"role": "system", "content": instrucao_sistema},
            {"role": "user", "content": prompt_usuario},
        ],
        "temperature": 0.4,
        "max_tokens": 4096,
    }
    url = URL_CLOUDFLARE.format(conta=CLOUDFLARE_ACCOUNT_ID, modelo=CLOUDFLARE_MODEL)

    try:
        resposta = _post_em_pedacos(url, headers, payload, TIMEOUT_CLOUDFLARE, prazo)
    except requests.exceptions.RequestException as erro:
        _quota_bloqueada_ate["cloudflare"] = time.time() + COOLDOWN_PROVEDOR_FALHA
        raise ProvedorIndisponivel(f"não respondeu ({type(erro).__name__})") from None

    detalhe = (resposta.text or "").strip()[:160]
    if resposta.status_code in STATUS_QUE_ATIVAM_ROTACAO or resposta.status_code >= 400:
        _quota_bloqueada_ate["cloudflare"] = time.time() + (
            COOLDOWN_QUOTA_PADRAO if resposta.status_code == 429
            else COOLDOWN_PROVEDOR_FALHA
        )
        raise ProvedorIndisponivel(
            f"HTTP {resposta.status_code}: {detalhe}",
            quota=(resposta.status_code == 429),
        )

    try:
        dados = resposta.json()
    except ValueError:
        _quota_bloqueada_ate["cloudflare"] = time.time() + COOLDOWN_PROVEDOR_FALHA
        raise ProvedorIndisponivel(f"resposta não é JSON: {detalhe}") from None

    # a API responde {"result": {"response": "..."}}; algumas contas devolvem o
    # formato da OpenAI na raiz — os dois casos são aceitos aqui
    resultado = dados.get("result")
    texto = ""
    if isinstance(resultado, str):
        texto = resultado
    elif isinstance(resultado, dict):
        texto = resultado.get("response") or ""
        if not texto and isinstance(resultado.get("choices"), list) and resultado["choices"]:
            texto = ((resultado["choices"][0] or {}).get("message") or {}).get("content") or ""
    elif isinstance(dados.get("choices"), list) and dados["choices"]:
        texto = ((dados["choices"][0] or {}).get("message") or {}).get("content") or ""

    if not str(texto or "").strip():
        motivo = str((dados.get("errors") or [{}])[0].get("message") or detalhe)[:120]
        _quota_bloqueada_ate["cloudflare"] = time.time() + COOLDOWN_PROVEDOR_FALHA
        raise ProvedorIndisponivel(f"resposta vazia ou com erro: {motivo}")

    registrar_chamada_ia("cloudflare")  # contador do painel de cotas do chat
    return str(texto)


def _mensagem_todos_esgotados(falhas):
    # o detalhe por provedor (quem falhou e por quê) NÃO vai para a resposta: o
    # usuário recebe só um aviso curto e o frontend já oferece "Tentar novamente"
    return "Não consegui responder agora. Tente novamente em alguns minutos."


def chamar_ia(prompt_usuario, instrucao_sistema):
    """
    Percorre os provedores UM POR VEZ, na ordem da rotação, e devolve a resposta
    do primeiro que funcionar. Nunca envia requisições em paralelo: cada
    provedor só é acionado depois de o anterior falhar.
    """
    global _ultimo_provedor

    # pergunta idêntica (mesma memória, mesmo contexto, mesmas cotações no prompt)
    # reaproveita a resposta: era o passo mais caro da mensagem
    chave_cache = _chave_cache_ia(prompt_usuario, instrucao_sistema)
    em_cache = _cache_ler(chave_cache)
    if em_cache is not None:
        return em_cache

    inicio = time.time()
    prazo = inicio + LIMITE_TOTAL_ROTACAO
    falhas = []

    for rotulo, chamada in _provedores_ia():
        if not _cabe_no_orcamento(prazo):
            falhas.append((rotulo, "orçamento de tempo da rotação esgotado", False))
            break
        try:
            texto = chamada(prompt_usuario, instrucao_sistema, prazo)
        except ProvedorIndisponivel as erro:
            falhas.append((rotulo, str(erro), erro.quota))
            continue
        except Exception as erro:
            # qualquer surpresa de um provedor não interrompe a rotação
            falhas.append((rotulo, f"{type(erro).__name__}: {str(erro)[:120]}", False))
            continue

        if str(texto or "").strip():
            _salvar_estado_ias()  # guarda quem ficou de fora para a próxima execução
            modelo_do_rotulo = (
                _ultimo_modelo_openrouter
                if rotulo == "OpenRouter" and _ultimo_modelo_openrouter
                else MISTRAL_MODEL if rotulo == "Mistral"
                else CLAUDE_MODEL if rotulo == "Claude"
                else CLOUDFLARE_MODEL if rotulo == "Cloudflare"
                else MODEL_NAME_RESERVA_GEMINI if rotulo.startswith("Gemini reserva")
                else MODEL_NAME
            )
            _ultimo_provedor = f"{rotulo} · {modelo_do_rotulo}"
            _cache_guardar(chave_cache, str(texto), ttl=CACHE_TTL_RESPOSTA_IA)
            return str(texto)

        falhas.append((rotulo, "resposta vazia", False))

    _salvar_estado_ias()  # guarda quem ficou de fora para a próxima execução

    if not falhas:
        raise ProvedorIndisponivel("nenhum provedor de IA configurado")

    mensagem = _mensagem_todos_esgotados(falhas)
    if any(quota for _, _, quota in falhas):
        raise CotaDoGemini(mensagem)
    raise ProvedorIndisponivel(mensagem)


def chamar_gemini(prompt_usuario, instrucao_sistema):
    """Nome mantido por compatibilidade: agora passa pela rotação de IAs."""
    return chamar_ia(prompt_usuario, instrucao_sistema)


def rotulo_ia_usada():
    """Rótulo do provedor/modelo que respondeu por último (para a linha de fontes)."""
    return _ultimo_provedor or MODEL_NAME


# ---------------------------------------------------------------------------
# PROMPT
# ---------------------------------------------------------------------------

def montar_prompt(pergunta, memoria, contexto_economatica, bloco_financeiro=""):
    blocos = []
    if bloco_financeiro:
        blocos.append(bloco_financeiro)
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


def perguntar_ao_ia(pergunta, memoria, contexto_economatica, bloco_financeiro=""):
    prompt_final = montar_prompt(pergunta, memoria, contexto_economatica, bloco_financeiro)
    return chamar_ia(prompt_final, SYSTEM_INSTRUCTION)


# nome antigo preservado (o fluxo interno agora é a rotação de IAs)
perguntar_ao_gemini = perguntar_ao_ia


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


def montar_linha_de_fontes(memoria=None, contexto_economatica="", fontes_mercado=()):
    """
    Monta a linha que mostra de onde vieram os dados de um gráfico ou resposta,
    para o usuário saber o que é estimativa e o que vem de fonte pública.
    """
    fontes = []
    if fontes_mercado:
        fontes.append("cotações coletadas nesta mensagem em " + ", ".join(fontes_mercado))
    fontes.append(f"conhecimento geral do modelo {rotulo_ia_usada()}")
    if contexto_economatica:
        fontes.append("conteúdo público do blog da Economatica (insight.economatica.com)")
    if memoria and memoria.get("fatos"):
        fontes.append("informações fornecidas por você em conversas anteriores")

    rotulo = "Fonte" if len(fontes) == 1 else "Fontes"
    return f"{rotulo}: " + "; ".join(fontes) + "."


def gerar_grafico(pergunta, memoria, contexto_economatica, output_dir=None, bloco_financeiro=""):
    if output_dir is None:
        output_dir = GRAFICOS_DIR

    # em serverless o diretório gravável é /tmp; se nem ele aceitar, o erro sobe
    # como falha do gráfico em vez de derrubar o servidor
    os.makedirs(output_dir, exist_ok=True)

    prompt_final = montar_prompt(pergunta, memoria, contexto_economatica, bloco_financeiro)
    texto_resposta = chamar_ia(prompt_final, CHART_SYSTEM_INSTRUCTION)
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

def processar_mensagem(mensagem, uid=None, memoria=None):
    """
    Processa uma mensagem do chat web e retorna um dict pronto para JSON:
      {"tipo": "texto", "resposta": "..."}
      {"tipo": "imagem", "arquivo": "grafico_....png", "texto": "..."}

    `memoria` é opcional: quando o chamador já tem a memória do usuário (o frontend
    a lê do Firestore), ela é usada e o servidor não toca em disco. Sem ela, o
    comportamento é o de sempre — ler o arquivo local memoria_<uid>.json.
    """
    mensagem = (mensagem or "").strip()
    if not mensagem:
        return {"tipo": "texto", "resposta": "Por favor, envie uma pergunta sobre economia."}

    _medidor_iniciar()

    if not isinstance(memoria, dict):
        try:
            memoria = carregar_memoria(uid)
        except Exception:
            memoria = {"fatos": []}

    if eh_pedido_de_imagem_nao_grafico(mensagem):
        return {
            "tipo": "texto",
            "resposta": (
                "Este assistente só gera gráficos (dados econômicos em formato de gráfico). "
                "Não gero fotos, desenhos ou outros tipos de imagem."
            ),
        }

    contexto_economatica = buscar_contexto_economatica()

    # cotações: cada fonte falha em silêncio; sem dado nenhum o prompt fica limpo
    dados_mercado = coletar_dados_financeiros(mensagem)
    bloco_financeiro = dados_mercado.get("bloco", "")
    fontes_mercado = dados_mercado.get("fontes", ())

    if eh_pedido_de_grafico(mensagem):
        try:
            nome_arquivo, titulo = gerar_grafico(
                mensagem, memoria, contexto_economatica,
                output_dir=GRAFICOS_DIR, bloco_financeiro=bloco_financeiro,
            )
            aviso = (
                "Aviso: os valores partem das cotações externas coletadas nesta mensagem "
                "e do conhecimento geral da IA — confira antes de decidir."
                if fontes_mercado
                else "Aviso: são estimativas geradas por IA — não são dados oficiais em tempo real."
            )
            return {
                "tipo": "imagem",
                "arquivo": nome_arquivo,
                "texto": (
                    f'Gráfico gerado: "{titulo}".\n\n'
                    f"Gerado em {datetime.now().strftime('%d/%m/%Y às %H:%M')}.\n"
                    f"{montar_linha_de_fontes(memoria, contexto_economatica, fontes_mercado)}\n\n"
                    f"{aviso}"
                ),
            }
        except (json.JSONDecodeError, KeyError) as erro:
            return {
                "tipo": "texto",
                "resposta": f"Não consegui montar o gráfico a partir da resposta da IA: {erro}",
            }
        except (CotaDoGemini, ProvedorIndisponivel) as erro:
            # pode_reenviar: o frontend mostra o botão "Tentar novamente"
            return {"tipo": "texto", "resposta": str(erro), "pode_reenviar": True}
        except Exception as erro:
            return {"tipo": "texto", "resposta": f"Erro ao gerar o gráfico: {erro}"}

    try:
        resposta = perguntar_ao_ia(mensagem, memoria, contexto_economatica, bloco_financeiro)
        return {"tipo": "texto", "resposta": resposta}
    except CotaDoGemini as erro:
        # pode_reenviar: o frontend mostra o botão "Tentar novamente"
        return {"tipo": "texto", "resposta": str(erro), "pode_reenviar": True}
    except ProvedorIndisponivel as erro:
        return {"tipo": "texto", "resposta": str(erro), "pode_reenviar": True}
    except requests.exceptions.HTTPError as erro:
        detalhe = str(erro)
        if erro.response is not None and erro.response.status_code not in STATUS_TRANSITORIOS:
            detalhe = f"{detalhe} — {erro.response.text[:300]}"
        return {"tipo": "texto", "resposta": f"Erro HTTP ao consultar a IA: {detalhe}"}
    except Exception as erro:
        return {"tipo": "texto", "resposta": f"Erro ao consultar a IA: {erro}"}


# ---------------------------------------------------------------------------
# DIAGNÓSTICO DE INICIALIZAÇÃO
# ---------------------------------------------------------------------------

def custo_da_mensagem():
    """Leitura do medidor (IA + mercado) para incluir na resposta do /api/chat."""
    return dict(_medidor())


def _resumo_configuracao():
    fontes = ["yfinance" if yf is not None else "yfinance (não instalado)"]
    fontes += ["HG Brasil", "Finnhub", "Twelve Data"]
    # notícias: as chaves ficam no servidor, o navegador nunca as recebe
    fontes += [
        "GNews (notícias)" if GNEWS_API_KEY else "GNews (notícias, sem chave)",
        "Marketaux (notícias)" if MARKETAUX_API_KEY else "Marketaux (notícias, sem chave)",
    ]
    # o banner reflete o que a rotação realmente pode acionar, na ordem da cadeia
    ia = []
    if GEMINI_API_KEY:
        ia.append("Gemini principal")
    if GEMINI_API_KEY_2:
        ia.append(f"Gemini reserva 1 ({MODEL_NAME_RESERVA_GEMINI})")
    if GEMINI_API_KEY_3:
        ia.append(f"Gemini reserva 2 ({MODEL_NAME_RESERVA_GEMINI})")
    if MISTRAL_API_KEY:
        ia.append(f"Mistral ({MISTRAL_MODEL})")
    if CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID:
        ia.append(f"Cloudflare Workers AI ({CLOUDFLARE_MODEL})")
    if OPENROUTER_API_KEY:
        ia.append(f"OpenRouter ({len(MODELOS_OPENROUTER_FALLBACK)} modelos)")
    # o Claude fecha a cadeia: só é acionado se todos os anteriores falharem
    ia.append(
        f"Claude ({CLAUDE_MODEL})" if CLAUDE_API_KEY
        else "Claude (sem chave — defina CLAUDE_API_KEY em back-end/.env)"
    )

    # provedor que uma restrição de PROVEDORES_IA_AUTORIZADOS deixa fora da cadeia
    desativadas = [
        rotulo for identificador, rotulo, _ in _candidatos_ia()
        if not _ia_autorizada(identificador)
    ]
    if desativadas:
        ia.append("desativados por configuração: " + ", ".join(desativadas))

    # quem está fora da rotação neste momento, para não dar a impressão de que o
    # provedor foi removido do código
    agora = time.time()
    em_espera = [
        f"{nome} ({int(expira_em - agora)}s)"
        for nome, expira_em in _quota_bloqueada_ate.items() if expira_em > agora
    ]
    if em_espera:
        ia.append("em espera: " + ", ".join(em_espera))

    return " · ".join(fontes), " · ".join(ia)


# ---------------------------------------------------------------------------
# API (rotas usadas pelo frontend)
# ---------------------------------------------------------------------------

# Vazio por padrão: as URLs saem relativas, então o mesmo código funciona no local,
# na Vercel e em qualquer domínio, sem precisar configurar nada. Só defina BASE_URL
# se o frontend for servido de OUTRA origem que não a do backend.
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

# Origens autorizadas a chamar a API pelo navegador. Em produção o frontend é
# servido da mesma origem, então o navegador nem faz requisição cross-origin; a lista
# existe para o caso de o site e a API ficarem em domínios diferentes. Antes o CORS
# estava liberado para QUALQUER origem, o que deixava qualquer site gastar a sua cota.
ORIGENS_PERMITIDAS = [
    origem.strip() for origem in os.environ.get(
        "ORIGENS_PERMITIDAS",
        "http://localhost:5000,http://127.0.0.1:5000,https://on-track-sage.vercel.app",
    ).split(",") if origem.strip()
]


def criar_app():
    """Cria a aplicação Flask: /api/chat, /api/health, /api/graficos e o site estático."""
    # static_folder=None de propósito: ninguém no site usa /static/, e a rota
    # embutida do Flask serviria qualquer arquivo de back-end/static/ sem passar
    # pela lista branca. Os PNGs saem por /api/graficos/, que é explícito.
    aplicacao = Flask(__name__, static_folder=None)
    CORS(aplicacao, resources={r"/api/*": {"origins": ORIGENS_PERMITIDAS}})

    @aplicacao.route("/api/chat", methods=["POST"])
    def chat():
        dados = request.get_json(silent=True) or {}
        mensagem = dados.get("mensagem", "")
        uid = dados.get("uid")
        # memória do usuário, quando o frontend a manda (Firestore). Sem ela, o
        # servidor cai no arquivo local — é o que roda em desenvolvimento.
        memoria = dados.get("memoria")

        if not str(mensagem).strip():
            return jsonify({"erro": "Campo 'mensagem' é obrigatório."}), 400

        try:
            resultado = processar_mensagem(mensagem, uid=uid, memoria=memoria)
        except Exception as erro:
            # nunca derruba o servidor: responde ao frontend com o erro tratado
            return jsonify({
                "tipo": "texto",
                "resposta": f"Erro interno ao processar a mensagem: {erro}",
            }), 500

        # quanto esta mensagem custou em requisições externas (IA + fontes de
        # mercado). O medidor é reiniciado no início de cada processar_mensagem,
        # então a leitura aqui é exatamente o custo desta resposta.
        custo = custo_da_mensagem()

        if resultado.get("tipo") == "imagem":
            # O PNG passa por uma rota da API em vez de /static: em serverless o
            # arquivo nasce em /tmp, que não é servido pelo CDN do site.
            nome_arquivo = resultado["arquivo"]
            caminho = f"/api/graficos/{nome_arquivo}"
            return jsonify({
                "tipo": "imagem",
                "url": f"{BASE_URL}{caminho}" if BASE_URL else caminho,
                "texto": resultado.get("texto", ""),
                "custo": custo,
            })

        resposta = {"tipo": "texto", "resposta": resultado.get("resposta", ""), "custo": custo}
        if resultado.get("pode_reenviar"):
            resposta["pode_reenviar"] = True    # habilita o botão "Tentar novamente" no chat
        return jsonify(resposta)

    @aplicacao.route("/api", methods=["GET"])
    def api_raiz():
        """A Vercel serve a função em /api; aqui ela responde o que existe por lá."""
        return jsonify({
            "status": "ok",
            "rotas": [
                "POST /api/chat",
                "GET /api/noticias",
                "GET /api/status-ias",
                "GET /api/health",
                "DELETE|POST /api/limpar-memoria",
                "GET /api/graficos/<arquivo>",
            ],
        })

    @aplicacao.route("/api/status-ias", methods=["GET"])
    def status_ias():
        """
        Diagnóstico interno: quanto já saiu hoje em cada API da rotação. A interface do
        chat não mostra mais esses números (eles são por instância, não a cota da conta).
        Responde só com estado local (contadores e cooldowns) — não chama nenhum
        provedor, então consultar isso não consome cota.
        """
        return jsonify({
            "provedores": status_provedores_ia(),
            "renovacao_gemini": renovacao_em_texto(),
            "renovacao_utc": renovacao_em_texto_utc(),
        })

    @aplicacao.route("/api/noticias", methods=["GET"])
    def noticias():
        """
        Feed de notícias do site: consulta a GNews e a Marketaux NO SERVIDOR e devolve
        as duas listas já unificadas. As chaves ficam em back-end/.env, então nem o
        navegador (DevTools/rede) nem o repositório chegam a vê-las.
        """
        _medidor_iniciar()
        try:
            resultado = buscar_noticias(
                request.args.get("q", "economia"),
                request.args.get("page", 1, type=int) or 1,
                request.args.get("max", MAX_NOTICIAS_POR_FONTE, type=int)
                or MAX_NOTICIAS_POR_FONTE,
            )
        except Exception:
            # problema de notícia nunca derruba o servidor nem quebra o chat
            resultado = {
                "ok": False,
                "noticias": [],
                "total": 0,
                "pagina": 1,
                "por_pagina": MAX_NOTICIAS_POR_FONTE,
                "fontes": [],
                "falhas": [{"fonte": "servidor", "motivo": "erro inesperado"}],
            }

        resultado["custo"] = custo_da_mensagem()
        return jsonify(resultado)

    @aplicacao.route("/api/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    @aplicacao.route("/api/limpar-memoria", methods=["DELETE", "POST"])
    def limpar_memoria():
        """
        Apaga a memória guardada no servidor para o usuário (e para uma conversa).

        O histórico que aparece na barra lateral vive no localStorage do navegador
        — quem o remove é o frontend. Este endpoint cuida do lado do servidor:
          {"uid": "...", "conversa_id": "c123"}  -> só a memória daquela conversa
          {"uid": "...", "apagar_tudo": true}    -> a memória geral do usuário

        É idempotente: apagar o que não existe responde ok com lista vazia.
        """
        dados = request.get_json(silent=True) or {}
        uid = _uid_seguro(dados.get("uid"))
        conversa_id = _uid_seguro(dados.get("conversa_id"))
        apagar_tudo = bool(dados.get("apagar_tudo"))

        if not uid and not apagar_tudo:
            return jsonify({"ok": False, "erro": "Informe o 'uid' do usuário."}), 400

        alvos = []
        if conversa_id:
            alvos.append(caminho_memoria_conversa(uid, conversa_id))
        if apagar_tudo and uid:
            alvos.append(caminho_memoria(uid))

        removidos = [os.path.basename(caminho) for caminho in alvos
                     if apagar_arquivo_memoria(caminho)]

        return jsonify({
            "ok": True,
            "removidos": removidos,
            # o frontend precisa saber disso: a lista de conversas é do navegador
            "historico_e_local": True,
        })

    # -----------------------------------------------------------------------
    # FRONTEND ESTÁTICO
    # O Flask também entrega o site, para rodar tudo em uma única porta (5000),
    # sem precisar de um servidor estático separado.
    # -----------------------------------------------------------------------

    @aplicacao.route("/")
    def inicio():
        return send_from_directory(RAIZ_PROJETO, "index.html")

    @aplicacao.route("/api/graficos/<path:nome_arquivo>")
    def grafico_gerado(nome_arquivo):
        """Serve o PNG gerado, que em serverless mora em /tmp (fora do site estático)."""
        return send_from_directory(GRAFICOS_DIR, nome_arquivo)

    @aplicacao.route("/<path:arquivo>")
    def arquivos_do_site(arquivo):
        # só frontend: back-end/, .env, *.py e JSONs de estado não são conteúdo público
        if not arquivo_do_site_permitido(arquivo):
            return jsonify({"erro": "Arquivo não encontrado."}), 404
        return send_from_directory(RAIZ_PROJETO, arquivo)

    return aplicacao


app = criar_app()


if __name__ == "__main__":
    try:
        os.makedirs(GRAFICOS_DIR, exist_ok=True)
    except OSError:
        pass  # disco somente leitura: os gráficos caem no /tmp tratado em GRAFICOS_DIR
    print("OnTrack rodando em http://localhost:5000")
    fontes, ia = _resumo_configuracao()
    print(f"Fontes de mercado: {fontes}")
    print(f"Rotação de IAs: {ia}")
    # deixa o contexto da Economatica pronto antes da primeira mensagem
    aquecer_contexto_economatica()
    app.run(host="0.0.0.0", port=5000, debug=True)
