// configuraçao do firebase


import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import { getAuth, createUserWithEmailAndPassword, signInWithEmailAndPassword, signInWithPopup, GoogleAuthProvider, onAuthStateChanged, signOut } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";
import { getFirestore, doc, setDoc, getDoc } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js";

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

export { auth, db, registrarUsuario, loginEmailSenha, loginGoogle, logout, verificarLogin, buscarDadosUsuario, verificarPlano, exigirPremium };