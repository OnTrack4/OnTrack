// configuraçao do firebase


import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import { getAuth, createUserWithEmailAndPassword, signInWithEmailAndPassword, signInWithPopup, GoogleAuthProvider, onAuthStateChanged, signOut } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";
import { getFirestore, doc, setDoc, getDoc, collection, deleteDoc, query, orderBy, limit, onSnapshot } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js";

const firebaseConfig = {
    apiKey: "AIzaSyDi4vXisro4NLQ9bKkSuChfcuCRuXlVw3s",
    authDomain: "ontrack-4a547.firebaseapp.com",
    projectId: "ontrack-4a547",
    storageBucket: "ontrack-4a547.firebasestorage.app",
    messagingSenderId: "1056868077810",
    appId: "1:1056868077810:web:ac602c4da659639750006b"
};

// inicializa o firebase
const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);
const googleProvider = new GoogleAuthProvider();


// registrar o usuario 


async function registrarUsuario(nome, email, senha) {
    try {
        // cria o usuário no firebase auth
        const resultado = await createUserWithEmailAndPassword(auth, email, senha);
        const usuario = resultado.user;

        // salva os dados no firestore
        await setDoc(doc(db, "usuarios", usuario.uid), {
            nome: nome,
            email: email,
            plano: "basico",         // todo usuario começa no plano basico
            criadoEm: new Date()
        });

        return { sucesso: true, usuario };

    } catch (erro) {
        return { sucesso: false, erro: erro.message };
    }
}


// login com email e senha


async function loginEmailSenha(email, senha) {
    try {
        const resultado = await signInWithEmailAndPassword(auth, email, senha);
        return { sucesso: true, usuario: resultado.user };
    } catch (erro) {
        return { sucesso: false, erro: erro.message };
    }
}


// login como google


async function loginGoogle() {
    try {
        const resultado = await signInWithPopup(auth, googleProvider);
        const usuario = resultado.user;

        // verifica se o usuario ja existe no banco
        const docRef = doc(db, "usuarios", usuario.uid);
        const docSnap = await getDoc(docRef);

        // se nao existe, cria o documento
        if (!docSnap.exists()) {
            await setDoc(docRef, {
                nome: usuario.displayName,
                email: usuario.email,
                plano: "basico",
                criadoEm: new Date()
            });
        }

        return { sucesso: true, usuario };

    } catch (erro) {
        return { sucesso: false, erro: erro.message };
    }
}


// sair(lougout)


async function logout() {
    await signOut(auth);
    window.location.href = "../index.html";
}


// ver se esta logado


function verificarLogin(callback) {
    onAuthStateChanged(auth, callback);
}


// procurar dados do usuario no firebse


async function buscarDadosUsuario(uid) {
    const docRef = doc(db, "usuarios", uid);
    const docSnap = await getDoc(docRef);
    if (docSnap.exists()) {
        return docSnap.data();
    }
    return null;
}


// VERIFICAR PLANO DO USUARIO


async function verificarPlano(uid) {
    const dados = await buscarDadosUsuario(uid);
    return dados?.plano || 'basico';
}

async function exigirPremium(uid, elementoId, mensagem = null) {
    const plano = await verificarPlano(uid);

    if (plano !== 'premium') {
        const elemento = document.getElementById(elementoId);
        if (elemento) {
            elemento.innerHTML = `
                <div class="aviso-premium">
                    <span class="aviso-icone">⭐</span>
                    <h3>Funcionalidade Premium</h3>
                    <p>${mensagem || 'Esta funcionalidade é exclusiva para assinantes Premium.'}</p>
                    <a href="pagamento.html" class="btn-upgrade">Fazer upgrade →</a>
                </div>
            `;
        }
        return false;
    }
    return true;
}

// ============================================================
// MEMÓRIA DO ASSISTENTE
//
// O backend rodando em serverless não guarda estado em disco, então a memória do
// usuário (os fatos que o assistente lembra entre conversas) vive no Firestore, no
// campo `memoria` do próprio documento do usuário: usuarios/{uid}.memoria.fatos.
// Quem lê e envia junto com cada mensagem é o frontend.
// ============================================================

function normalizarMemoria(valor) {
    const fatos = valor?.fatos;
    if (!Array.isArray(fatos)) return { fatos: [] };
    return {
        fatos: fatos
            .filter((fato) => typeof fato === 'string' && fato.trim())
            .slice(0, 100)
    };
}


// ============================================================
// HISTÓRICO DE CONVERSAS DO CHAT
//
// Cada conversa é um documento em usuarios/{uid}/conversas/{id}. Guardar no
// Firestore (e não no localStorage) é o que faz o histórico acompanhar o usuário
// entre dispositivos e faz a exclusão valer no servidor: apagar aqui apaga para
// todos os aparelhos, porque o servidor é a fonte da verdade.
// ============================================================

const LIMITE_CONVERSAS = 30;

// caminho do documento de sessão (qual conversa está aberta)
const HISTORICO_CHAT = "historico_chat";
const DOCUMENTO_SESSAO = "sessao";

function referenciaConversas(uid) {
    return collection(db, "usuarios", uid, "conversas");
}

// grava (ou atualiza) uma conversa. O merge preserva os campos já existentes no
// documento e troca o array de mensagens inteiro pelo mais recente
async function salvarConversa(uid, conversa) {
    await setDoc(doc(referenciaConversas(uid), String(conversa.id)), {
        titulo: String(conversa.titulo || 'Conversa').slice(0, 120),
        atualizadaEm: Number(conversa.atualizadaEm) || Date.now(),
        mensagens: conversa.mensagens || []
    }, { merge: true });
}

async function apagarConversa(uid, id) {
    await deleteDoc(doc(referenciaConversas(uid), String(id)));
}

// acompanha o histórico em tempo real: qualquer mudança feita em outro aparelho
// (ou em outra aba) chega aqui sem recarregar a página. Devolve a função para
// encerrar a observação quando o usuário trocar ou sair.
function observarConversas(uid, aoMudar, aoFalhar) {
    const consulta = query(
        referenciaConversas(uid),
        orderBy("atualizadaEm", "desc"),
        limit(LIMITE_CONVERSAS)
    );

    return onSnapshot(consulta, (foto) => {
        aoMudar(foto.docs.map((d) => Object.assign({ id: d.id }, d.data())));
    }, aoFalhar);
}

// ---------- sessão do chat (qual conversa está aberta) ----------
//
// Um documento único por usuário, em usuarios/{uid}/historico_chat/sessao. Existe
// para o outro aparelho abrir na mesma conversa em que você estava.
//
// Por que não guardar o histórico inteiro aqui dentro: um documento tem teto de 1MB
// e a exclusão de uma conversa reescreveria o documento de todas, com dois aparelhos
// podendo se sobrescrever. As mensagens ficam em conversas/{id} — uma por conversa.

function referenciaSessao(uid) {
    return doc(db, "usuarios", uid, HISTORICO_CHAT, DOCUMENTO_SESSAO);
}

async function salvarSessao(uid, conversaId) {
    await setDoc(referenciaSessao(uid), {
        conversa_atual: conversaId ? String(conversaId) : null,
        atualizadaEm: Date.now()
    }, { merge: true });
}

// devolve a função que encerra a observação
function observarSessao(uid, aoMudar, aoFalhar) {
    return onSnapshot(referenciaSessao(uid), (foto) => {
        const dados = foto.data() || {};
        aoMudar(dados.conversa_atual || null);
    }, aoFalhar);
}


export { auth, db, registrarUsuario, loginEmailSenha, loginGoogle, logout, verificarLogin, buscarDadosUsuario, verificarPlano, exigirPremium, normalizarMemoria, LIMITE_CONVERSAS, salvarConversa, apagarConversa, observarConversas, salvarSessao, observarSessao };