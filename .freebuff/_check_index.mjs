
    import {
        verificarLogin, buscarDadosUsuario, normalizarMemoria,
        LIMITE_CONVERSAS, salvarConversa, apagarConversa, observarConversas,
        salvarSessao, observarSessao
    } from './firebase.js';

    // ============================================================
    // CONSULTOR IA NO INDEX — mesma API usada em paginas/consultor.html
    // ============================================================
    // Em produção a API fica na MESMA origem que o site (a Vercel serve o site e a
    // função Python no mesmo domínio), então a URL é relativa: funciona sem
    // configurar domínio e sem depender de localhost fixo no código. O backend local
    // da porta 5000 só entra quando a página é servida por outro servidor (Live
    // Server, arquivo aberto direto) — aí a API não está na mesma origem.
    const API_BASE =
        (location.hostname === 'localhost' || location.hostname === '127.0.0.1') &&
        location.port !== '5000'
            ? 'http://localhost:5000'
            : '';
    const BACKEND_URL = `${API_BASE}/api/chat`;

    // ---------- referências de tela ----------
    const chatSecao = document.getElementById('chat-secao');
    const boasVindas = document.querySelector('.boas-vindas');
    const sugestoes = document.querySelector('.sugestoes');
    const input = document.querySelector('.composer-input');
    const listaConversas = document.getElementById('lista-conversas');

    // ---------- estado ----------
    let usuarioUid = null;
    let conversaAtual = null;
    let mensagens = [];
    let enviando = false;
    // memória do assistente (vem do Firestore e é enviada em cada mensagem, porque
    // o backend em serverless não guarda estado)
    let memoriaDoUsuario = { fatos: [] };

    // ---------- histórico: espelho local + Firestore ----------
    // Duas camadas, de propósito:
    //   * espelho local (localStorage) — a lista aparece na hora, sem esperar a
    //     rede, e sobrevive a um recarregamento durante a resposta;
    //   * Firestore (usuarios/{uid}/conversas) — fonte da verdade de quem está
    //     logado: o histórico acompanha o usuário entre dispositivos e a exclusão
    //     vale no servidor, apagando para todos os aparelhos.
    // Visitante não tem conta, então o histórico dele fica só no navegador.
    const chaveLocal = () =>
        usuarioUid ? `ontrack_conversas_${usuarioUid}` : 'ontrack_conversas_visitante';

    let conversas = [];                 // exatamente o que a barra lateral mostra
    let pararDeObservar = null;         // encerra o tempo real do Firestore
    let listaServidor = [];             // último retorno do servidor
    let migracaoPendente = false;       // enquanto o que só existe aqui está subindo
    let recuperacaoFeita = false;
    // sessão (usuarios/{uid}/historico_chat/sessao): qual conversa está aberta
    let pararDeObservarSessao = null;
    let conversaDaSessao = null;        // o que o servidor diz estar aberto
    let seguindoSessao = true;          // para de seguir quando você age neste aparelho

    function lerEspelhoLocal() {
        try {
            const lista = JSON.parse(localStorage.getItem(chaveLocal()));
            return Array.isArray(lista) ? lista : [];
        } catch {
            return [];
        }
    }

    function gravarEspelhoLocal() {
        try {
            localStorage.setItem(chaveLocal(), JSON.stringify(conversas.slice(0, LIMITE_CONVERSAS)));
        } catch { /* quota cheia — o Firestore continua sendo a fonte da verdade */ }
    }

    // ---------- de onde a lista está vindo ----------
    // Fica visível o tempo todo de propósito: quando as regras do Firestore negam a
    // escrita, o app continua funcionando com o espelho local e parecia que estava
    // tudo certo — só que o histórico não acompanhava o usuário entre aparelhos.
    const ESTADO_HISTORICO = {
        conectando: { texto: '● Histórico: conectando…', cor: '#5c6b84' },
        nuvem: { texto: '● Histórico sincronizado com a nuvem', cor: '#3ddc97' },
        local: { texto: '● Só neste navegador — sem sincronização', cor: '#c9a227' },
        visitante: { texto: '● Só neste navegador — entre para sincronizar', cor: '#5c6b84' }
    };

    function atualizarStatusHistorico(estado) {
        const escolhido = ESTADO_HISTORICO[estado] || ESTADO_HISTORICO.conectando;
        let elemento = document.getElementById('status-historico');
        if (!elemento) {
            if (!listaConversas?.parentElement) return;
            elemento = document.createElement('p');
            elemento.id = 'status-historico';
            elemento.style.cssText = 'margin:6px 12px 0;font-size:11px;line-height:1.4;';
            listaConversas.parentElement.insertBefore(elemento, listaConversas);
        }
        elemento.textContent = escolhido.texto;
        elemento.style.color = escolhido.cor;
    }

    // falha de Firestore nunca trava o chat: registra o motivo e segue no local
    function avisarFalhaHistorico(erro) {
        console.warn('Histórico no Firestore indisponível:', erro);
        atualizarStatusHistorico('local');
    }

    function limparAvisoFalhaHistorico() {
        atualizarStatusHistorico('nuvem');
    }

    function encerrarObservacao() {
        if (pararDeObservar) { pararDeObservar(); pararDeObservar = null; }
    }

    function aplicarConversas(lista, aoGravar = true) {
        conversas = lista;
        if (aoGravar) gravarEspelhoLocal();
        renderizarListaConversas();
    }

    // Enquanto o envio do que só existe neste navegador não termina, a lista mostrada
    // é servidor + pendentes: sem isso o histórico antigo do usuário piscaria vazio
    // antes de subir. Depois disso o servidor passa a mandar sozinho — é o que faz
    // uma exclusão feita em outro aparelho valer aqui.
    function aplicarListaServidor(lista) {
        listaServidor = lista;
        if (!migracaoPendente) {
            aplicarConversas(lista);
            seguirSessaoDoServidor();
            return;
        }

        const ids = new Set(lista.map(c => String(c.id)));
        const pendentes = lerEspelhoLocal().filter(c => c && c.id && !ids.has(String(c.id)));
        const juntas = pendentes.length
            ? lista.concat(pendentes)
                .sort((a, b) => Number(b.atualizadaEm || 0) - Number(a.atualizadaEm || 0))
                .slice(0, LIMITE_CONVERSAS)
            : lista;
        aplicarConversas(juntas, false);
        seguirSessaoDoServidor();
    }

    // sobe para o Firestore o histórico que o usuário já tinha no navegador, ou uma
    // conversa criada sem internet. Compara por atualizadaEm: a versão mais recente vence.
    async function enviarConversasPendentes(uid) {
        const porId = new Map(listaServidor.map(c => [String(c.id), c]));
        const pendentes = lerEspelhoLocal().filter(c => {
            if (!c || !c.id || !c.mensagens?.length) return false;
            const remota = porId.get(String(c.id));
            return !remota || Number(c.atualizadaEm || 0) > Number(remota.atualizadaEm || 0);
        });

        let subiu = false;
        for (const conversa of pendentes) {
            try {
                await salvarConversa(uid, conversa);
                subiu = true;
            } catch (erro) {
                avisarFalhaHistorico(erro);
                break;
            }
        }
        return subiu;
    }

    // ---------- sessão: qual conversa está aberta ----------
    function encerrarObservacaoSessao() {
        if (pararDeObservarSessao) { pararDeObservarSessao(); pararDeObservarSessao = null; }
    }

    function registrarSessao(conversaId) {
        if (!usuarioUid) return;
        salvarSessao(usuarioUid, conversaId).catch(avisarFalhaHistorico);
    }

    // Abre aqui a conversa que está aberta em outro aparelho — mas só enquanto você
    // não abriu nada neste: um aparelho não deve puxar o outro para outra conversa no
    // meio de uma leitura ou de uma digitação.
    function seguirSessaoDoServidor() {
        if (!seguindoSessao || conversaAtual || enviando || !conversaDaSessao) return;
        const alvo = conversas.find(c => String(c.id) === String(conversaDaSessao));
        if (!alvo) return;   // ainda não chegou na lista, ou foi excluída no outro aparelho

        seguindoSessao = false;   // o que veio do servidor não volta para o servidor
        abrirConversa(alvo.id, true);
    }

    // liga o histórico do usuário logado ao Firestore
    async function conectarHistorico(uid) {
        encerrarObservacao();
        encerrarObservacaoSessao();
        conversaDaSessao = null;
        seguindoSessao = true;
        atualizarStatusHistorico('conectando');
        migracaoPendente = true;

        // 1) espelho local primeiro: a barra lateral já aparece preenchida
        aplicarConversas(lerEspelhoLocal(), false);
        recuperarPerguntaSemResposta();

        pararDeObservar = observarConversas(uid, async (lista) => {
            aplicarListaServidor(lista);
            if (!migracaoPendente) return;

            // 2) primeira resposta do servidor: sobe o que só existe aqui e passa a
            //    tratar a lista do servidor como a única verdadeira
            const subiu = await enviarConversasPendentes(uid);
            if (!subiu) {
                migracaoPendente = false;
                limparAvisoFalhaHistorico();
                aplicarConversas(listaServidor);
                seguirSessaoDoServidor();
            }
        }, (erro) => {
            migracaoPendente = false;
            avisarFalhaHistorico(erro);
        });

        // 3) sessão em tempo real: o celular abre na conversa que está aberta no PC
        pararDeObservarSessao = observarSessao(uid, (conversaId) => {
            conversaDaSessao = conversaId;
            seguirSessaoDoServidor();
        }, avisarFalhaHistorico);
    }

    function gerarTitulo(texto) {
        const t = String(texto).replace(/\s+/g, ' ').trim();
        return t.length > 38 ? t.slice(0, 38) + '…' : (t || 'Nova conversa');
    }

    function novaConversa() {
        conversaAtual = null;
        mensagens = [];
        chatSecao.hidden = true;
        chatSecao.innerHTML = '';
        boasVindas.hidden = false;
        sugestoes.hidden = false;
        input.value = '';
        // "Novo Chat" é uma decisão sua: para de seguir a sessão do servidor (senão
        // seria puxado de volta para a conversa do outro aparelho) e registra que
        // não há conversa aberta
        seguindoSessao = false;
        registrarSessao(null);
    }

    // ---------- renderização da conversa aberta ----------
    function abrirConversa(id, veioDoServidor = false) {
        const c = conversas.find(x => String(x.id) === String(id));
        if (!c) return;

        if (veioDoServidor) {
            seguindoSessao = false;   // já é a conversa do servidor: não precisa voltar
        } else {
            registrarSessao(id);      // abrir aqui é "é aqui que eu estou"
        }

        conversaAtual = id;
        mensagens = c.mensagens || [];

        boasVindas.hidden = true;
        sugestoes.hidden = true;
        chatSecao.hidden = false;
        chatSecao.innerHTML = '';

        mensagens.forEach((m, indice) => {
            if (m.tipo === 'imagem' && m.url) {
                chatAdicionarImagem(m.url, m.texto, m.hora);
                return;
            }
            // se a resposta anterior ficou sem cota, reapresenta o botão de reenvio
            const pergunta = m.podeReenviar
                ? (mensagens.slice(0, indice).reverse().find(x => x.papel === 'usuario') || {}).texto
                : null;
            chatAdicionarTexto(m.papel === 'usuario' ? 'usuario' : 'ia', m.texto, m.hora, pergunta);
        });

        chatSecao.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function persistirConversa() {
        if (!mensagens.length) return;
        const titulo = gerarTitulo(mensagens[0].texto);
        const agora = Date.now();
        let conversa = conversaAtual
            ? conversas.find(c => String(c.id) === String(conversaAtual))
            : null;

        if (conversa) {
            conversa.mensagens = mensagens;
            conversa.atualizadaEm = agora;
        } else {
            conversaAtual = 'c' + agora;
            conversa = { id: conversaAtual, titulo, mensagens, atualizadaEm: agora };
            conversas.unshift(conversa);
            if (conversas.length > LIMITE_CONVERSAS) conversas.length = LIMITE_CONVERSAS;
        }

        // espelho local na hora: recarregar a página durante a resposta não perde nada
        gravarEspelhoLocal();
        renderizarListaConversas();

        // o servidor recebe em seguida; falha aqui não interrompe o chat
        if (usuarioUid) {
            salvarConversa(usuarioUid, conversa)
                .then(limparAvisoFalhaHistorico)
                .catch(avisarFalhaHistorico);
            // a conversa em uso é a sessão: é o que o outro aparelho vai abrir
            registrarSessao(conversaAtual);
        }
    }

    function renderizarListaConversas() {
        if (!listaConversas) return;
        const lista = conversas;

        if (!lista.length) {
            // estado vazio: nenhum dado falso
            listaConversas.innerHTML =
                '<p class="conversa-vazia" id="conversas-vazia" style="padding: 12px; font-size: 13px; color: #5c6b84;">Nenhuma conversa recente</p>';
            return;
        }

        listaConversas.innerHTML = lista.map(c => {
            const rotulo = escapeHtml(String(c.titulo || 'Conversa'));
            const id = escapeHtml(String(c.id));
            // o botão ⋮ fica fora do <a> para o clique não abrir a conversa
            return `<div class="conversa-linha" data-linha="${id}">
                <a class="conversa-item" href="#" data-conversa="${id}">
                    <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"
                        stroke-linecap="round" stroke-linejoin="round">
                        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
                    </svg>
                    <span>${rotulo}</span>
                </a>
                <button class="conversa-menu" type="button" data-menu="${id}" aria-haspopup="menu"
                    aria-expanded="false" aria-label="Ações da conversa">⋮</button>
            </div>`;
        }).join('');

        listaConversas.querySelectorAll('[data-conversa]').forEach(a => {
            a.addEventListener('click', (e) => {
                e.preventDefault();
                abrirConversa(a.getAttribute('data-conversa'));
            });
        });

        listaConversas.querySelectorAll('[data-menu]').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                if (btn.getAttribute('aria-expanded') === 'true') fecharMenuConversa();
                else abrirMenuConversa(btn, btn.getAttribute('data-menu'));
            });
        });
    }

    // ---------- menu ⋮ da conversa (mesmo padrão do Gemini) ----------
    let menuConversa = null;      // um único menu, reaproveitado por todas as linhas
    let menuConversaId = null;    // conversa alvo do menu aberto
    let botaoMenuAtual = null;

    function garantirMenuConversa() {
        if (menuConversa) return menuConversa;

        menuConversa = document.createElement('div');
        menuConversa.className = 'menu-conversa';
        menuConversa.setAttribute('role', 'menu');
        menuConversa.hidden = true;
        menuConversa.innerHTML =
            '<button type="button" class="perigo" role="menuitem" data-acao="excluir">' +
            '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" ' +
            'stroke-linecap="round" stroke-linejoin="round">' +
            '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 11v6M14 11v6" /></svg>' +
            'Excluir conversa</button>';

        menuConversa.querySelector('[data-acao="excluir"]').addEventListener('click', () => {
            const id = menuConversaId;
            fecharMenuConversa();
            if (id) confirmarExclusaoConversa(id);
        });

        document.body.appendChild(menuConversa);
        return menuConversa;
    }

    function abrirMenuConversa(botao, id) {
        const menu = garantirMenuConversa();
        menuConversaId = id;
        botaoMenuAtual = botao;
        botao.setAttribute('aria-expanded', 'true');
        botao.closest('.conversa-linha')?.classList.add('menu-aberto');

        menu.hidden = false;

        // abre ancorado no botão, sem sair da janela
        const area = botao.getBoundingClientRect();
        const esquerda = Math.min(area.right + 6, window.innerWidth - menu.offsetWidth - 8);
        const topo = Math.min(area.top, window.innerHeight - menu.offsetHeight - 8);
        menu.style.left = Math.max(8, esquerda) + 'px';
        menu.style.top = Math.max(8, topo) + 'px';

        menu.querySelector('button')?.focus();
    }

    function fecharMenuConversa() {
        if (!menuConversa || menuConversa.hidden) return;
        menuConversa.hidden = true;
        menuConversaId = null;
        if (botaoMenuAtual) {
            botaoMenuAtual.setAttribute('aria-expanded', 'false');
            botaoMenuAtual.closest('.conversa-linha')?.classList.remove('menu-aberto');
            botaoMenuAtual = null;
        }
    }

    // ---------- confirmação e exclusão da conversa ----------
    let modalExclusao = null;
    let conversaParaExcluir = null;

    function garantirModalExclusao() {
        if (modalExclusao) return modalExclusao;

        modalExclusao = document.createElement('div');
        modalExclusao.className = 'modal-fundo';
        modalExclusao.hidden = true;
        modalExclusao.innerHTML =
            '<div class="modal-caixa" role="dialog" aria-modal="true" aria-labelledby="titulo-exclusao">' +
            '<h3 id="titulo-exclusao">Excluir esta conversa?</h3>' +
            '<p id="texto-exclusao"></p>' +
            '<div class="modal-acoes">' +
            '<button type="button" class="btn-neutro" data-acao="cancelar">Cancelar</button>' +
            '<button type="button" class="btn-perigo" data-acao="confirmar">Excluir</button>' +
            '</div></div>';

        // clique fora da caixa fecha o aviso
        modalExclusao.addEventListener('click', (e) => {
            if (e.target === modalExclusao) fecharModalExclusao();
        });
        modalExclusao.querySelector('[data-acao="cancelar"]').addEventListener('click', fecharModalExclusao);
        modalExclusao.querySelector('[data-acao="confirmar"]').addEventListener('click', () => {
            const id = conversaParaExcluir;
            fecharModalExclusao();
            if (id) excluirConversa(id);
        });

        document.body.appendChild(modalExclusao);
        return modalExclusao;
    }

    function confirmarExclusaoConversa(id) {
        const modal = garantirModalExclusao();
        const conversa = conversas.find(c => String(c.id) === String(id));
        conversaParaExcluir = id;
        modal.querySelector('#texto-exclusao').textContent =
            `"${conversa ? conversa.titulo : 'Conversa'}" sai do seu histórico em todos os ` +
            'dispositivos que usam esta conta. Esta ação não pode ser desfeita.';
        modal.hidden = false;
        modal.querySelector('[data-acao="confirmar"]')?.focus();
    }

    function fecharModalExclusao() {
        conversaParaExcluir = null;
        if (modalExclusao) modalExclusao.hidden = true;
    }

    // o histórico da barra lateral é do navegador; o servidor guarda a memória
    // do usuário, então avisamos o backend (e seguimos mesmo se ele falhar)
    const LIMPAR_MEMORIA_URL = BACKEND_URL.replace(/\/api\/chat$/, '/api/limpar-memoria');

    async function limparMemoriaNoServidor(id) {
        // visitante não tem memória no servidor: nem vale a requisição
        if (!usuarioUid) return;
        try {
            await fetch(LIMPAR_MEMORIA_URL, {
                method: 'DELETE',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ uid: usuarioUid, conversa_id: id })
            });
        } catch (erro) {
            console.warn('Não foi possível limpar a memória no servidor:', erro);
        }
    }

    async function excluirConversa(id) {
        const anterior = conversas.slice();
        const eraAConversaAberta = conversaAtual && String(conversaAtual) === String(id);

        // sai da tela e do espelho local na hora; o servidor confirma logo abaixo
        conversas = conversas.filter(c => String(c.id) !== String(id));
        gravarEspelhoLocal();

        // era a conversa aberta: volta para a tela inicial do assistente
        if (eraAConversaAberta) novaConversa();

        // comparação por dataset em vez de montar um seletor com o id
        const linha = Array.from(listaConversas?.querySelectorAll('.conversa-linha') || [])
            .find(l => l.dataset.linha === String(id));
        if (linha) {
            linha.classList.add('removendo');
            setTimeout(renderizarListaConversas, 260);
        } else {
            renderizarListaConversas();
        }

        // a memória do backend pertence à conversa, então sai junto
        limparMemoriaNoServidor(id);

        // exclusão no servidor: sem ela, a conversa voltaria no próximo carregamento
        // (ou em outro dispositivo, que é justamente o ponto de guardar no Firestore)
        if (!usuarioUid) return;
        try {
            await apagarConversa(usuarioUid, id);
            limparAvisoFalhaHistorico();
        } catch (erro) {
            // não deu: a conversa volta para a tela em vez de sumir sem ter sumido
            conversas = anterior;
            gravarEspelhoLocal();
            renderizarListaConversas();
            avisarFalhaHistorico(erro);
        }
    }

    // fecha o menu ao clicar fora, apertar Esc, rolar a página ou redimensionar
    document.addEventListener('mousedown', (e) => {
        if (!menuConversa || menuConversa.hidden) return;
        if (e.target.closest('.menu-conversa') || e.target.closest('[data-menu]')) return;
        fecharMenuConversa();
    });
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        fecharMenuConversa();
        fecharModalExclusao();
    });
    document.addEventListener('scroll', fecharMenuConversa, true);
    window.addEventListener('resize', fecharMenuConversa);

    // ---------- envio de mensagem ao consultor (mesmo contrato do consultor.html) ----------

    // teto de espera da resposta: o backend pode levar minutos (coleta de cotações
    // + rotação de IAs). Sem isso, uma conexão morta prende a tela para sempre.
    const LIMITE_ESPERA_MS = 180000;

    // o botão de enviar dá feedback e recusa duplo clique durante a espera
    function atualizarEstadoEnvio(ativo) {
        const btn = document.querySelector('.btn-consultar');
        if (!btn) return;
        const rotulo = Array.from(btn.childNodes).find(n => n.nodeType === Node.TEXT_NODE);
        if (rotulo) {
            if (!btn.dataset.rotuloOriginal) btn.dataset.rotuloOriginal = rotulo.textContent.trim();
            rotulo.textContent = ativo ? 'Analisando... ' : (btn.dataset.rotuloOriginal || 'Consultar') + ' ';
        }
        btn.disabled = ativo;
        btn.style.opacity = ativo ? '0.6' : '';
        btn.style.cursor = ativo ? 'progress' : '';
    }

    // mostra o tempo decorrido: a espera longa deixa de parecer página travada
    function animarEspera(indicador) {
        const inicio = Date.now();
        const alvo = indicador.querySelector('.texto-espera');
        const intervalo = setInterval(() => {
            if (alvo) alvo.textContent = `analisando... ${Math.round((Date.now() - inicio) / 1000)}s`;
        }, 1000);
        return () => clearInterval(intervalo);
    }

    // envio durante outra requisição: avisa, mas preserva o texto já digitado
    let avisoAguardeVisivel = false;
    function avisarAguarde() {
        if (avisoAguardeVisivel) return;
        avisoAguardeVisivel = true;
        const aviso = document.createElement('div');
        aviso.id = 'aviso-aguarde';
        aviso.style.cssText = 'margin-bottom:10px;padding:10px 14px;border-radius:12px;font-size:13px;' +
            'background:#0d2233;border:1px solid #1b4d3d;color:#8fe3c4;';
        aviso.textContent = '⏳ Ainda estou processando a pergunta anterior. Seu texto continua no campo — ' +
            'envie assim que a resposta chegar.';
        const caixa = document.querySelector('.composer-caixa');
        if (caixa) caixa.prepend(aviso);
    }

    function limparAvisoAguarde() {
        avisoAguardeVisivel = false;
        const aviso = document.getElementById('aviso-aguarde');
        if (aviso) aviso.remove();
    }

    async function enviarParaConsultor(texto) {
        if (enviando) { avisarAguarde(); return; }
        enviando = true;
        limparAvisoAguarde();

        boasVindas.hidden = true;
        sugestoes.hidden = true;
        chatSecao.hidden = false;

        // 1) a pergunta entra na tela imediatamente, antes de qualquer espera
        chatAdicionarTexto('usuario', texto);
        mensagens.push({ papel: 'usuario', texto, hora: horaAtual() });

        // 2) e é gravada na hora: se a página recarregar durante a espera, a
        // pergunta não se perde (antes só era salva depois da resposta chegar)
        persistirConversa();

        const indicador = mostrarIndicador();
        const pararAnimacaoEspera = animarEspera(indicador);
        atualizarEstadoEnvio(true);

        // 3) teto de espera no cliente, com cancelamento real da requisição
        const controlador = new AbortController();
        const temporizador = setTimeout(() => controlador.abort(), LIMITE_ESPERA_MS);

        try {
            const resposta = await fetch(BACKEND_URL, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    mensagem: texto,
                    uid: usuarioUid,
                    memoria: memoriaDoUsuario
                }),
                signal: controlador.signal
            });

            const dados = await resposta.json();
            indicador.remove();

            if (!resposta.ok) throw new Error(dados.erro || dados.message || 'Erro no servidor');

            if (dados.tipo === 'imagem' && dados.url) {
                const urlSegura = sanitizarUrlImagem(dados.url);
                if (urlSegura) {
                    chatAdicionarImagem(urlSegura, dados.texto || '');
                    mensagens.push({ papel: 'ia', tipo: 'imagem', url: urlSegura, texto: dados.texto || '', hora: horaAtual() });
                } else {
                    chatAdicionarTexto('ia', 'Não foi possível exibir o gráfico retornado pelo servidor.');
                    mensagens.push({ papel: 'ia', texto: 'Não foi possível exibir o gráfico retornado pelo servidor.', hora: horaAtual() });
                }
            } else if (dados.tipo === 'texto' && dados.resposta) {
                // cota esgotada/limite de uso: permite reenviar sem redigitar a pergunta
                const podeReenviar = dados.pode_reenviar === true;
                chatAdicionarTexto('ia', dados.resposta, null, podeReenviar ? texto : null);
                mensagens.push({
                    papel: 'ia',
                    texto: dados.resposta,
                    hora: horaAtual(),
                    podeReenviar,
                });
            } else {
                chatAdicionarTexto('ia', 'Recebi uma resposta inesperada do servidor. Tente novamente.');
                mensagens.push({ papel: 'ia', texto: 'Recebi uma resposta inesperada do servidor. Tente novamente.', hora: horaAtual() });
            }

            persistirConversa();

        } catch (erro) {
            indicador.remove();
            // AbortError = estourou o teto de espera; o resto é falha de rede/servidor
            const expirou = erro && erro.name === 'AbortError';
            const aviso = expirou
                ? 'A resposta demorou mais que o normal e a espera foi interrompida. Tente novamente.'
                : 'Desculpe, ocorreu um erro ao processar sua pergunta. Tente novamente.';
            // reaproveita o mesmo botão "Tentar novamente" do fluxo de cota esgotada
            chatAdicionarTexto('ia', aviso, null, texto);
            mensagens.push({ papel: 'ia', texto: aviso, hora: horaAtual(), podeReenviar: true });
            persistirConversa();
            console.error('Erro:', erro);
        } finally {
            clearTimeout(temporizador);
            pararAnimacaoEspera();
            enviando = false;
            atualizarEstadoEnvio(false);
            // a espera acabou: o aviso de "aguarde" não faz mais sentido na tela
            limparAvisoAguarde();
        }
    }

    // ---------- helpers de render (mesmos padrões visuais do consultor) ----------
    function chatAdicionarTexto(tipo, texto, hora = null, perguntaParaReenviar = null) {
        const div = document.createElement('div');
        div.className = 'mensagem ' + (tipo === 'usuario' ? 'mensagem-usuario' : 'mensagem-ia');
        div.style.cssText = 'display:flex;gap:12px;margin-bottom:18px;' +
            (tipo === 'usuario' ? 'flex-direction:row-reverse;' : '');

        const avatar = document.createElement('div');
        avatar.textContent = tipo === 'usuario' ? '👤' : '🤖';
        avatar.style.cssText = 'width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;' +
            (tipo === 'usuario' ? 'background:linear-gradient(135deg,#1b4d3d,#0f2f4d);' : 'background:#0d2233;border:1px solid #1b4d3d;');

        const conteudo = document.createElement('div');
        conteudo.style.cssText = 'max-width:75%;padding:14px 18px;border-radius:16px;font-size:14.5px;line-height:1.6;' +
            (tipo === 'usuario' ? 'background:linear-gradient(135deg,#34e3a1,#19b98a);color:#04120c;border-bottom-right-radius:4px;' : 'background:#0a0f1c;border:1px solid #131c2e;color:#e6ebf3;border-bottom-left-radius:4px;');

        const p = document.createElement('p');
        p.style.margin = '0';
        p.innerHTML = formatarTexto(texto);

        const horaSpan = document.createElement('span');
        horaSpan.textContent = hora || horaAtual();
        horaSpan.style.cssText = 'display:block;margin-top:6px;font-size:11px;opacity:.55;' +
            (tipo === 'usuario' ? 'text-align:right;' : '');

        conteudo.appendChild(p);
        conteudo.appendChild(horaSpan);

        if (tipo === 'ia' && perguntaParaReenviar) {
            const btnReenviar = document.createElement('button');
            btnReenviar.type = 'button';
            btnReenviar.textContent = 'Tentar novamente';
            btnReenviar.style.cssText = 'margin-top:10px;padding:7px 14px;border-radius:999px;' +
                'border:1px solid #1b4d3d;background-color:#0d2233;color:#2ee6a8;font-size:12.5px;' +
                'font-weight:600;font-family:inherit;cursor:pointer;';
            btnReenviar.addEventListener('click', () => {
                if (enviando) return;
                btnReenviar.disabled = true;
                btnReenviar.textContent = 'Reenviando...';
                btnReenviar.style.opacity = '0.6';
                btnReenviar.style.cursor = 'default';
                enviarParaConsultor(perguntaParaReenviar);
            });
            conteudo.appendChild(btnReenviar);
        }

        div.appendChild(avatar);
        div.appendChild(conteudo);
        chatSecao.appendChild(div);
        chatSecao.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }

    function chatAdicionarImagem(url, texto, hora = null) {
        const div = document.createElement('div');
        div.className = 'mensagem mensagem-ia';
        div.style.cssText = 'display:flex;gap:12px;margin-bottom:18px;';

        const avatar = document.createElement('div');
        avatar.textContent = '🤖';
        avatar.style.cssText = 'width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;background:#0d2233;border:1px solid #1b4d3d;';

        const conteudo = document.createElement('div');
        conteudo.style.cssText = 'max-width:75%;padding:14px 18px;border-radius:16px;background:#0a0f1c;border:1px solid #131c2e;color:#e6ebf3;border-bottom-left-radius:4px;';

        if (texto) {
            const p = document.createElement('p');
            p.style.margin = '0 0 8px';
            p.innerHTML = formatarTexto(texto);
            conteudo.appendChild(p);
        }

        const img = document.createElement('img');
        img.src = url;
        img.alt = 'Gráfico gerado pelo consultor';
        img.style.maxWidth = '100%';
        img.style.borderRadius = '8px';
        conteudo.appendChild(img);

        const horaSpan = document.createElement('span');
        horaSpan.textContent = hora || horaAtual();
        horaSpan.style.cssText = 'display:block;margin-top:6px;font-size:11px;opacity:.55;';
        conteudo.appendChild(horaSpan);

        div.appendChild(avatar);
        div.appendChild(conteudo);
        chatSecao.appendChild(div);
        chatSecao.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }

    function mostrarIndicador() {
        const div = document.createElement('div');
        div.className = 'mensagem mensagem-ia mensagem-digitando';
        div.style.cssText = 'display:flex;gap:12px;margin-bottom:18px;';

        const avatar = document.createElement('div');
        avatar.textContent = '🤖';
        avatar.style.cssText = 'width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0;background:#0d2233;border:1px solid #1b4d3d;';

        const conteudo = document.createElement('div');
        conteudo.style.cssText = 'padding:14px 18px;border-radius:16px;background:#0a0f1c;border:1px solid #131c2e;';
        conteudo.innerHTML = '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#2ee6a8;margin:0 2px;animation:pulsar-dot 1s infinite;"></span>' +
            '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#2ee6a8;margin:0 2px;animation:pulsar-dot 1s .2s infinite;"></span>' +
            '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#2ee6a8;margin:0 2px;animation:pulsar-dot 1s .4s infinite;"></span>' +
            '<span class="texto-espera" style="display:block;margin-top:8px;font-size:12px;color:#7b8aa3;"></span>';

        div.appendChild(avatar);
        div.appendChild(conteudo);
        chatSecao.appendChild(div);
        chatSecao.scrollIntoView({ behavior: 'smooth', block: 'end' });
        return div;
    }

    // ---------- utilitários (mesmos nomes/padrões do consultor.html) ----------
    function horaAtual() {
        return new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
    }

    function escapeHtml(texto) {
        return String(texto)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    function formatarTexto(texto) {
        return escapeHtml(texto)
            .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
            .replace(/\*(.+?)\*/g, '<em>$1</em>')
            .replace(/\n/g, '<br>');
    }

    function sanitizarUrlImagem(url) {
        try {
            const parsed = new URL(url, window.location.origin);
            if (!['http:', 'https:'].includes(parsed.protocol)) return null;
            return parsed.href;
        } catch {
            return null;
        }
    }

    // ---------- eventos da interface ----------
    function enviarDoInput() {
        const texto = (input.value || '').trim();
        if (!texto) return;
        if (enviando) {
            // não engole mais a mensagem em silêncio: o texto fica no campo e o
            // usuário recebe um aviso explicando por que nada aconteceu
            avisarAguarde();
            return;
        }
        input.value = '';
        enviarParaConsultor(texto);
    }

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            enviarDoInput();
        }
    });

    document.querySelector('.btn-consultar').addEventListener('click', enviarDoInput);

    // rede de segurança contra o recarregamento clássico: se um <form> envolver o
    // composer no futuro, o envio não pode submeter a página
    document.addEventListener('submit', (e) => e.preventDefault(), true);

    // cards de sugestão e atalhos preenchem o campo e enviam
    document.querySelectorAll('.sugestao-card').forEach(card => {
        card.addEventListener('click', () => {
            const titulo = card.querySelector('h3')?.textContent?.trim();
            if (titulo) enviarParaConsultor(titulo);
        });
    });
    document.querySelectorAll('.atalho').forEach(btn => {
        btn.addEventListener('click', () => {
            const texto = btn.textContent.trim();
            if (texto) enviarParaConsultor(texto);
        });
    });

    // "Novo Chat" abre uma conversa em branco
    document.querySelector('.btn-novo-chat').addEventListener('click', novaConversa);

    // ---------- inicialização: dados dinâmicos do usuário ----------
    function aplicarDadosUsuario(nome, plano) {
        const iniciais = String(nome).trim().split(/\s+/).map(p => p[0]).slice(0, 2).join('').toUpperCase() || 'U';

        // perfil da sidebar
        document.getElementById('perfil-nome').textContent = nome;
        document.getElementById('perfil-avatar').textContent = iniciais;
        document.getElementById('perfil-plano').textContent = plano === 'premium' ? 'Plano Pro B3' : 'Plano Gratuito';

        // avatar da topbar
        const elAvatarTopo = document.getElementById('topbar-avatar');
 if (elAvatarTopo) elAvatarTopo.textContent = iniciais;
    }

    function aplicarVisitante() {
        document.getElementById('perfil-nome').textContent = 'Visitante';
        document.getElementById('perfil-avatar').textContent = '?';
        document.getElementById('perfil-plano').textContent = 'Conta Gratuita';

        const elAvatarTopo = document.getElementById('topbar-avatar');
        if (elAvatarTopo) elAvatarTopo.textContent = '?';

        const elLogin = document.getElementById('perfil-login-link');
        if (elLogin) elLogin.style.display = 'inline-flex';
    }

    // se a página foi recarregada durante uma resposta, a última pergunta ficou
    // pendente: reabre a conversa e já oferece o reenvio
    function recuperarPerguntaSemResposta() {
        if (recuperacaoFeita) return;
        recuperacaoFeita = true;

        const ultimaConversa = conversas[0];
        const ultimaMensagem = ultimaConversa && ultimaConversa.mensagens &&
            ultimaConversa.mensagens[ultimaConversa.mensagens.length - 1];
        if (!ultimaMensagem || ultimaMensagem.papel !== 'usuario') return;

        abrirConversa(ultimaConversa.id);
        chatAdicionarTexto(
            'ia',
            'Sua última pergunta ficou sem resposta (a página foi recarregada durante o envio).',
            null,
            ultimaMensagem.texto
        );
    }

    verificarLogin(async (usuario) => {
        if (usuario) {
            usuarioUid = usuario.uid;
            // histórico do usuário vem do Firestore e passa a acompanhá-lo. Vem antes
            // da leitura do perfil para a barra lateral não esperar essa viagem extra.
            conectarHistorico(usuario.uid);
            const dados = await buscarDadosUsuario(usuario.uid);
            const nome = dados?.nome || usuario.displayName || 'Usuário';
            const plano = dados?.plano || 'basico';
            aplicarDadosUsuario(nome, plano);
            // o que o assistente lembra do usuário, lido do próprio perfil
            memoriaDoUsuario = normalizarMemoria(dados?.memoria);
        } else {
            // visitante: saudação genérica + botão de login, sem dados fictícios
            aplicarVisitante();
            memoriaDoUsuario = { fatos: [] };
            // sem conta não há Firestore: o histórico dele fica só neste navegador
            encerrarObservacao();
            encerrarObservacaoSessao();
            aplicarConversas(lerEspelhoLocal(), false);
            recuperarPerguntaSemResposta();
            atualizarStatusHistorico('visitante');
        }
    });
    