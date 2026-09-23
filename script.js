
// ---------------------------------------------------------------------------
// CONFIGURAÇÃO DAS APIs DE NOTÍCIAS
//
// As chaves da GNews e da Marketaux NÃO ficam aqui — nem neste arquivo, nem em
// nenhum outro servido ao navegador. Elas vivem no back-end (back-end/.env) e
// quem conversa com as duas APIs é a rota /api/noticias, que devolve todas as
// notícias já unificadas. Assim nenhuma chave é versionada no GitHub nem
// aparece no DevTools de quem visita o site.
// ---------------------------------------------------------------------------

const API_BASE =
    location.protocol === 'file:'
        ? 'http://localhost:5000'
        : '';
const NOTICIAS_URL = `${API_BASE}/api/noticias`;

const NOTICIAS_POR_PAGINA = 10;
const MAX_PAGINAS = 10; // teto da paginação exibida

let paginaAtual = 1;
let termoBusca = 'economia';
let totalPaginas = 1;


// ---------------------------------------------------------------
// utilidades de exibição
// ---------------------------------------------------------------

// o texto vem de APIs externas: entra como texto, nunca como HTML
function escaparHtml(texto) {
    const elemento = document.createElement('div');
    elemento.textContent = String(texto ?? '');
    return elemento.innerHTML;
}

// só aceita link http(s) — evita que qualquer outra coisa vire URL do card
function urlSegura(link) {
    try {
        const alvo = new URL(String(link ?? ''), window.location.origin);
        return (alvo.protocol === 'http:' || alvo.protocol === 'https:') ? alvo.href : '#';
    } catch (erro) {
        return '#';
    }
}


// buscar e mostrar as noticias (GNews + Marketaux, unificadas pelo backend)


async function carregarNoticias() {

    const container = document.getElementById('lista-noticias');
    if (!container) return;

    container.innerHTML = '<p>Carregando notícias...</p>';

    const URL = `${NOTICIAS_URL}?q=${encodeURIComponent(termoBusca)}` +
        `&page=${paginaAtual}&max=${NOTICIAS_POR_PAGINA}`;

    try {

        const resposta = await fetch(URL);
        if (!resposta.ok) throw new Error(`HTTP ${resposta.status}`);

        const dados = await resposta.json();

        container.innerHTML = '';

        const noticias = dados.noticias || [];

        // 'ok: false' = nenhuma das duas fontes respondeu (é falha, não ausência
        // de resultado). O motivo de cada uma fica no console, para diagnóstico.
        if (dados.ok === false || (noticias.length === 0 && (dados.falhas || []).length)) {
            console.warn('Fontes de notícias indisponíveis:', dados.falhas || dados);
            container.innerHTML = '<p>Erro ao carregar notícias. Tente novamente mais tarde.</p>';
            return;
        }

        if (noticias.length === 0) {
            container.innerHTML = '<p>Nenhuma notícia encontrada. Tente outro termo de busca.</p>';
            return;
        }

        if (dados.falhas && dados.falhas.length) {
            console.warn('Uma fonte de notícias falhou nesta busca:', dados.falhas);
        }

        // calcula total de paginas (com teto de 10 paginas)
        const total = Number(dados.total) || noticias.length;
        totalPaginas = Math.min(Math.ceil(total / NOTICIAS_POR_PAGINA), MAX_PAGINAS);
        if (totalPaginas < 1) totalPaginas = 1;

        // cria um card para cada noticia
        noticias.forEach(noticia => {

            const card = document.createElement('div');
            card.classList.add('card-noticia');

            const data = new Date(noticia.publishedAt);
            const dataValida = !isNaN(data.getTime());
            const dataFormatada = dataValida
                ? data.toLocaleDateString('pt-BR', {
                    day: '2-digit',
                    month: '2-digit',
                    year: 'numeric',
                    hour: '2-digit',
                    minute: '2-digit'
                })
                : '';

            // o selo mostra o veículo e de qual das duas APIs a notícia veio
            const nomeFonte = (noticia.source && noticia.source.name) || noticia.provedor || 'Notícia';
            const provedor = noticia.provedor ? ` · ${noticia.provedor}` : '';
            const link = urlSegura(noticia.url);

            card.innerHTML = `
                <div class="noticia-fonte">${escaparHtml(nomeFonte)}${escaparHtml(provedor)}</div>
                <h3 class="noticia-titulo">${escaparHtml(noticia.title)}</h3>
                <p class="noticia-descricao">${escaparHtml(noticia.description || '')}</p>
                <div class="noticia-rodape">
                    <span class="noticia-data">🕐 ${dataFormatada}</span>
                    <a href="${escaparHtml(link)}" target="_blank" rel="noopener" class="noticia-link">Ler mais →</a>
                </div>
            `;

            container.appendChild(card);
        });

        // atualiza a paginacao
        atualizarPaginacao();

        // volta ao topo da secao
        const secao = document.getElementById('noticias');
        if (secao) secao.scrollIntoView({ behavior: 'smooth' });

    } catch (erro) {
        container.innerHTML = '<p>Erro ao carregar notícias. Tente novamente mais tarde.</p>';
        console.error('Erro:', erro);
    }
}


// paginacao das noticias


function atualizarPaginacao() {
    const paginacao = document.getElementById('paginacao');
    if (!paginacao) return;

    paginacao.innerHTML = '';

    // botao anterior
    if (paginaAtual > 1) {
        const btnAnterior = document.createElement('button');
        btnAnterior.textContent = '← Anterior';
        btnAnterior.onclick = () => {
            paginaAtual--;
            carregarNoticias();
        };
        paginacao.appendChild(btnAnterior);
    }

    // botoes de pagina
    for (let i = 1; i <= totalPaginas; i++) {
        const btn = document.createElement('button');
        btn.textContent = i;
        btn.classList.toggle('pagina-ativa', i === paginaAtual);
        btn.onclick = () => {
            paginaAtual = i;
            carregarNoticias();
        };
        paginacao.appendChild(btn);
    }

    // botao proximo
    if (paginaAtual < totalPaginas) {
        const btnProximo = document.createElement('button');
        btnProximo.textContent = 'Próximo →';
        btnProximo.onclick = () => {
            paginaAtual++;
            carregarNoticias();
        };
        paginacao.appendChild(btnProximo);
    }
}


// pesquisa noticias


function pesquisar() {
    const input = document.getElementById('input-busca');
    if (!input) return;

    const termo = input.value.trim();
    if (termo === '') return;

    termoBusca = termo;
    paginaAtual = 1;
    carregarNoticias();
}

// permite pesquisar apertando Enter
document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('input-busca');
    if (input) {
        input.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') pesquisar();
        });
    }
});


// ABAS DO LOGIN


if (document.getElementById('aba-registro')) {
    document.getElementById('aba-registro').style.display = 'none';
    document.getElementById('btn-entrar').classList.add('ativo');
}

function mostrarAba(aba) {
    if (aba === 'entrar') {
        document.getElementById('aba-entrar').style.display = 'block';
        document.getElementById('aba-registro').style.display = 'none';
        document.getElementById('btn-entrar').classList.add('ativo');
        document.getElementById('btn-registro').classList.remove('ativo');
    } else {
        document.getElementById('aba-entrar').style.display = 'none';
        document.getElementById('aba-registro').style.display = 'block';
        document.getElementById('btn-entrar').classList.remove('ativo');
        document.getElementById('btn-registro').classList.add('ativo');
    }
}


// RODAR So na pagina de noticia


if (document.getElementById('lista-noticias')) {
    carregarNoticias();
}