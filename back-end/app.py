"""
Servidor Flask — serve o frontend estático (index.html, paginas/, css/, assets/)
e faz a ponte com o agente financeiro (agente.py).

Execução padrão, a partir da raiz do projeto:
    python back-end/app.py
"""

import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAIZ_PROJETO = os.path.dirname(BASE_DIR)

# carrega o .env do back-end pelo caminho do arquivo, independentemente do
# diretório de execução (necessário para rodar "python back-end/app.py" na raiz)
load_dotenv(os.path.join(BASE_DIR, ".env"))

from agente import GRAFICOS_DIR, processar_mensagem

BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/chat", methods=["POST"])
def chat():
    dados = request.get_json(silent=True) or {}
    mensagem = dados.get("mensagem", "")
    uid = dados.get("uid")

    if not str(mensagem).strip():
        return jsonify({"erro": "Campo 'mensagem' é obrigatório."}), 400

    try:
        resultado = processar_mensagem(mensagem, uid=uid)
    except Exception as erro:
        # nunca derruba o servidor: responde ao frontend com o erro tratado
        return jsonify({
            "tipo": "texto",
            "resposta": f"Erro interno ao processar a mensagem: {erro}",
        }), 500

    if resultado.get("tipo") == "imagem":
        nome_arquivo = resultado["arquivo"]
        return jsonify({
            "tipo": "imagem",
            "url": f"{BASE_URL}/static/graficos/{nome_arquivo}",
            "texto": resultado.get("texto", ""),
        })

    resposta = {"tipo": "texto", "resposta": resultado.get("resposta", "")}
    if resultado.get("pode_reenviar"):
        resposta["pode_reenviar"] = True    # habilita o botão "Tentar novamente" no chat
    return jsonify(resposta)


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# FRONTEND ESTÁTICO
# O Flask também entrega o site, para rodar tudo em uma única porta (5000),
# sem precisar de um servidor estático separado.
# ---------------------------------------------------------------------------

@app.route("/")
def inicio():
    return send_from_directory(RAIZ_PROJETO, "index.html")


@app.route("/<path:arquivo>")
def arquivos_do_site(arquivo):
    return send_from_directory(RAIZ_PROJETO, arquivo)


if __name__ == "__main__":
    os.makedirs(GRAFICOS_DIR, exist_ok=True)
    print("OnTrack rodando em http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)
