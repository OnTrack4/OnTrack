"""
Entrypoint do backend na Vercel.

A Vercel carrega este arquivo como a Vercel Function de /api e usa o `app` do WSGI.
A aplicação Flask de verdade continua em back-end/app.py — o mesmo arquivo que roda
no desenvolvimento local com `python back-end/app.py`, sem código duplicado.

Duas particularidades de deploy são resolvidas aqui:

1. `back-end` tem hífen no nome, então não é um pacote importável. O diretório entra
   no começo do sys.path para que `import app` ache back-end/app.py (não existe
   nenhum módulo chamado "app" na raiz, então não há colisão de nome).

2. A Vercel reescreve /api/chat para /api/index?caminho=chat, porque o destino de um
   rewrite recebe o caminho do destino e não o original. O caminho pedido pelo
   usuário é devolvido ao Flask antes de ele rotear, então /api/chat, /api/health,
   /api/limpar-memoria e /api/graficos/<arquivo> funcionam normalmente.
"""

import os
import sys
import urllib.parse

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(RAIZ, "back-end")

if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from app import app as aplicacao_flask  # noqa: E402  (o sys.path vem antes de propósito)


class CaminhoOriginal:
    """
    Middleware WSGI que restaura o PATH_INFO a partir do parâmetro `caminho`.

    Sem ele, o Flask receberia sempre /api/index e responderia 404 em tudo. Se o
    parâmetro não vier (chamada direta a /api, ou execução local), nada muda e o
    caminho segue como está.
    """

    def __init__(self, aplicacao):
        self.aplicacao = aplicacao

    def __call__(self, environ, iniciar_resposta):
        parametros = urllib.parse.parse_qs(
            environ.get("QUERY_STRING", ""), keep_blank_values=True
        )
        caminho = (parametros.pop("caminho", [""]) or [""])[0]

        if caminho:
            environ["PATH_INFO"] = "/api/" + caminho.lstrip("/")
            environ["QUERY_STRING"] = urllib.parse.urlencode(parametros, doseq=True)
            environ["RAW_URI"] = environ["PATH_INFO"] + (
                f"?{environ['QUERY_STRING']}" if environ["QUERY_STRING"] else ""
            )

        return self.aplicacao(environ, iniciar_resposta)


app = CaminhoOriginal(aplicacao_flask)

__all__ = ["app"]
