"""
Servidor Flask — ponte entre consultor.html e o agente financeiro (agente.py).
"""

import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

load_dotenv()

from agente import GRAFICOS_DIR, processar_mensagem

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

app = Flask(__name__, static_folder="static", static_url_path="/static")
CORS(app)


@app.route("/api/chat", methods=["POST"])
def chat():
    dados = request.get_json(silent=True) or {}
    mensagem = dados.get("mensagem", "")
    uid = dados.get("uid")

    if not str(mensagem).strip():
        return jsonify({"erro": "Campo 'mensagem' é obrigatório."}), 400

    resultado = processar_mensagem(mensagem, uid=uid)

    if resultado["tipo"] == "imagem":
        nome_arquivo = resultado["arquivo"]
        return jsonify({
            "tipo": "imagem",
            "url": f"{BASE_URL}/static/graficos/{nome_arquivo}",
            "texto": resultado.get("texto", ""),
        })

    return jsonify({
        "tipo": "texto",
        "resposta": resultado.get("resposta", ""),
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    os.makedirs(GRAFICOS_DIR, exist_ok=True)
    app.run(host="0.0.0.0", port=5000, debug=True)
