// ===========================
// CONFIGURAÇÃO DA API
// ===========================

const API_KEY = '8cf0046413e4f1c9d04e835b31377c49';
const NOTICIAS_POR_PAGINA = 10;

let paginaAtual = 1;
let termoBusca = 'economia';
let totalPaginas = 1;

// ===========================
// BUSCAR E MOSTRAR NOTICIAS
// ===========================

async function carregarNoticias() {

    const container = document.getElementById('lista-noticias');
    if (!container) return;

    container.innerHTML = '<p>Carregando notícias...</p>';

    const URL = `https://gnews.io/api/v4/search?q=${encodeURIComponent(termoBusca)}&lang=pt&max=${NOTICIAS_POR_PAGINA}&page=${paginaAtual}&apikey=${API_KEY}`;

    try {

        const resposta = await fetch(URL);
        const dados = await resposta.json();

        container.innerHTML = '';

        if (!dados.articles || dados.articles.length === 0) {
            container.innerHTML = '<p>Nenhuma notícia encontrada. Tente outro termo de busca.</p>';
            return;
        }

        // calcula total de paginas
        totalPaginas = Math.ceil(dados.totalArticles / NOTICIAS_POR_PAGINA);
        if (totalPaginas > 10) totalPaginas = 10; // limite de 10 paginas

        // cria um card para cada noticia
        dados.articles.forEach(noticia => {

            const card = document.createElement('div');
            card.classList.add('card-noticia');

            const data = new Date(noticia.publishedAt);
            const dataFormatada = data.toLocaleDateString('pt-BR', {
                day: '2-digit',
                month: '2-digit',
                year: 'numeric',
                hour: '2-digit',
                minute: '2-digit'
            });

            card.innerHTML = `
                <div class="noticia-fonte">${noticia.source.name}</div>
                <h3 class="noticia-titulo">${noticia.title}</h3>
                <p class="noticia-descricao">${noticia.description || ''}</p>
                <div class="noticia-rodape">
                    <span class="noticia-data">🕐 ${dataFormatada}</span>
                    <a href="${noticia.url}" target="_blank" class="noticia-link">Ler mais →</a>
                </div>
            `;

            container.appendChild(card);
        });

        // atualiza a paginacao
        atualizarPaginacao();

        // volta ao topo da secao
        document.getElementById('noticias').scrollIntoView({ behavior: 'smooth' });

    } catch (erro) {
        container.innerHTML = '<p>Erro ao carregar notícias. Tente novamente mais tarde.</p>';
        console.error('Erro:', erro);
    }
}

// ===========================
// PAGINACAO
// ===========================

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

// ===========================
// PESQUISA
// ===========================

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

// ===========================
// ABAS DO LOGIN
// ===========================

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

// ===========================
// RODAR SÓ NA PAGINA DE NOTICIAS
// ===========================

if (document.getElementById('lista-noticias')) {
    carregarNoticias();
}