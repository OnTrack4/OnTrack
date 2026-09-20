
        import { verificarLogin, buscarDadosUsuario, normalizarMemoria, logout } from '../firebase.js';

        // ============================================================
        // CONFIGURAÇÃO DO BACK-END
        // A API fica na mesma origem em produção (o site e a função Python são
        // servidos pelo mesmo domínio), então a URL é relativa e não precisa de
        // configuração por ambiente. O backend local da porta 5000 só é usado
        // quando a página é servida por outro servidor.
        // ============================================================
        const API_BASE =
            (location.hostname === 'localhost' || location.hostname === '127.0.0.1') &&
            location.port !== '5000'
                ? 'http://localhost:5000'
                : '';
        const BACKEND_URL = `${API_BASE}/api/chat`;

        // Referência ao usuário logado (Firebase) para enviar uid ao back-end
        let usuarioLogado = null;

        // memória do assistente: vem do Firestore e vai em cada mensagem, porque o
        // backend em serverless não guarda estado
        let memoriaDoUsuario = { fatos: [] };

        // navbar dinamica
        verificarLogin(async (usuario) => {
            usuarioLogado = usuario;

            const navUsuario = document.getElementById('nav-usuario');
            if (usuario) {
                const dados = await buscarDadosUsuario(usuario.uid);
                memoriaDoUsuario = normalizarMemoria(dados?.memoria);
                const nome = dados?.nome || usuario.displayName || 'Usuário';
                navUsuario.innerHTML = `
                    <span class="nav-saudacao">Olá, ${nome}</span>
                    <a href="perfil.html" class="btn-perfil">Meu perfil</a>
                    <button id="btn-sair" class="btn-sair">Sair</button>
                `;
                document.getElementById('btn-sair').addEventListener('click', async () => {
                    await logout();
                    window.location.href = '../index.html';
                });
            }
        });

        // hora incial
        document.getElementById('hora-inicial').textContent = horaAtual();

        // enviar a msg
        // a flag evita envios simultâneos: desabilitar o botão não bloqueia o Enter
        let enviandoConsultor = false;

        window.enviarMensagem = async () => {
            const input = document.getElementById('input-mensagem');
            const texto = input.value.trim();

            if (!texto || enviandoConsultor) return;
            enviandoConsultor = true;

            adicionarMensagem(texto, 'usuario');
            input.value = '';
            input.style.height = 'auto';

            const indicador = mostrarIndicador();

            const btn = document.getElementById('btn-enviar');
            btn.disabled = true;
            document.getElementById('btn-enviar-texto').textContent = 'Analisando...';

            // teto de espera: o backend pode levar minutos; sem isso a tela trava
            const controlador = new AbortController();
            const temporizador = setTimeout(() => controlador.abort(), 180000);

            try {
                const resposta = await fetch(BACKEND_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        mensagem: texto,
                        uid: usuarioLogado?.uid ?? null,
                        memoria: memoriaDoUsuario
                    }),
                    signal: controlador.signal
                });

                const dados = await resposta.json();

                if (!resposta.ok) {
                    throw new Error(dados.erro || dados.message || 'Erro no servidor');
                }

                indicador.remove();
                renderizarRespostaIA(dados);

            } catch (erro) {
                indicador.remove();
                const expirou = erro && erro.name === 'AbortError';
                adicionarMensagem(
                    expirou
                        ? 'A resposta demorou mais que o normal e a espera foi interrompida. Tente novamente.'
                        : 'Desculpe, ocorreu um erro ao processar sua pergunta. Tente novamente.',
                    'ia'
                );
                console.error('Erro:', erro);
            } finally {
                clearTimeout(temporizador);
                btn.disabled = false;
                document.getElementById('btn-enviar-texto').textContent = 'Enviar';
                enviandoConsultor = false;
            }
        };

        // interpreta o JSON retornado pelo back-end (tipo: "texto" ou "imagem")
        function renderizarRespostaIA(dados) {
            if (dados.tipo === 'texto' && dados.resposta) {
                adicionarMensagem(dados.resposta, 'ia');
                return;
            }

            if (dados.tipo === 'imagem' && dados.url) {
                adicionarMensagemImagem(dados.url, dados.texto);
                return;
            }

            adicionarMensagem('Recebi uma resposta inesperada do servidor. Tente novamente.', 'ia');
        }

        // adicionar msg de texto
        function adicionarMensagem(texto, tipo) {
            const container = document.getElementById('chat-container');

            const div = document.createElement('div');
            div.classList.add('mensagem', `mensagem-${tipo}`);

            const avatar = tipo === 'ia' ? '🤖' : '👤';

            div.innerHTML = `
                <div class="mensagem-avatar">${avatar}</div>
                <div class="mensagem-conteudo">
                    <p>${formatarTexto(texto)}</p>
                    <span class="mensagem-hora">${horaAtual()}</span>
                </div>
            `;

            container.appendChild(div);
            container.scrollTop = container.scrollHeight;
        }

        // adicionar msg com imagem/gráfico retornado pelo back-end (appendChild — não altera o histórico)
        function adicionarMensagemImagem(url, texto = '') {
            const urlSegura = sanitizarUrlImagem(url);
            if (!urlSegura) {
                adicionarMensagem('Não foi possível exibir o gráfico retornado pelo servidor.', 'ia');
                return;
            }

            const container = document.getElementById('chat-container');

            const div = document.createElement('div');
            div.classList.add('mensagem', 'mensagem-ia');

            const avatar = document.createElement('div');
            avatar.classList.add('mensagem-avatar');
            avatar.textContent = '🤖';

            const conteudo = document.createElement('div');
            conteudo.classList.add('mensagem-conteudo');

            if (texto) {
                const paragrafo = document.createElement('p');
                paragrafo.insertAdjacentHTML('afterbegin', formatarTexto(texto));
                conteudo.appendChild(paragrafo);
            }

            const img = document.createElement('img');
            img.src = urlSegura;
            img.alt = 'Gráfico gerado pelo consultor';
            img.style.maxWidth = '100%';
            img.style.borderRadius = '8px';
            conteudo.appendChild(img);

            const hora = document.createElement('span');
            hora.classList.add('mensagem-hora');
            hora.textContent = horaAtual();
            conteudo.appendChild(hora);

            div.appendChild(avatar);
            div.appendChild(conteudo);
            container.appendChild(div);
            container.scrollTop = container.scrollHeight;
        }

        // barra de digitaçao
        function mostrarIndicador() {
            const container = document.getElementById('chat-container');

            const div = document.createElement('div');
            div.classList.add('mensagem', 'mensagem-ia', 'mensagem-digitando');
            div.innerHTML = `
                <div class="mensagem-avatar">🤖</div>
                <div class="mensagem-conteudo">
                    <div class="digitando">
                        <span></span>
                        <span></span>
                        <span></span>
                    </div>
                </div>
            `;

            container.appendChild(div);
            container.scrollTop = container.scrollHeight;
            return div;
        }

        // limpar a conversa
        window.limparConversa = () => {
            const container = document.getElementById('chat-container');
            container.innerHTML = `
                <div class="mensagem mensagem-ia">
                    <div class="mensagem-avatar">🤖</div>
                    <div class="mensagem-conteudo">
                        <p>Olá! Sou o consultor financeiro do OnTrack. Como posso ajudar você hoje?</p>
                        <span class="mensagem-hora">${horaAtual()}</span>
                    </div>
                </div>
            `;
        };

        // enviar msg com enter
        document.getElementById('input-mensagem').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                enviarMensagem();
            }
        });

        // rede de segurança: nenhum envio pode submeter/recarregar a página
        document.addEventListener('submit', (e) => e.preventDefault(), true);

        // redimensiona o textarea automaticamente
        document.getElementById('input-mensagem').addEventListener('input', (e) => {
            e.target.style.height = 'auto';
            e.target.style.height = Math.min(e.target.scrollHeight, 120) + 'px';
        });

        // funçoes extras
        function horaAtual() {
            return new Date().toLocaleTimeString('pt-BR', {
                hour: '2-digit',
                minute: '2-digit'
            });
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

    