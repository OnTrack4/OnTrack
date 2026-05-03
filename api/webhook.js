const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);
const admin = require('firebase-admin');

// inicializa o firebase admin se ainda nao foi inicializado
if (!admin.apps.length) {
    admin.initializeApp({
        credential: admin.credential.cert({
            projectId: process.env.FIREBASE_PROJECT_ID,
            clientEmail: process.env.FIREBASE_CLIENT_EMAIL,
            privateKey: process.env.FIREBASE_PRIVATE_KEY?.replace(/\\n/g, '\n')
        })
    });
}

const db = admin.firestore();

module.exports = async (req, res) => {

    if (req.method !== 'POST') {
        return res.status(405).end();
    }

    const sig = req.headers['stripe-signature'];
    let event;

    try {
        event = stripe.webhooks.constructEvent(
            req.body,
            sig,
            process.env.STRIPE_WEBHOOK_SECRET
        );
    } catch (erro) {
        return res.status(400).send(`Webhook Error: ${erro.message}`);
    }

    // pagamento confirmado — atualiza o plano no Firebase
    if (event.type === 'checkout.session.completed') {
        const session = event.data.object;
        const usuarioId = session.metadata.usuarioId;

        if (usuarioId) {
            await db.collection('usuarios').doc(usuarioId).update({
                plano: 'premium',
                stripeCustomerId: session.customer,
                stripeSubscriptionId: session.subscription,
                planoAtualizadoEm: new Date()
            });
        }
    }

    // assinatura cancelada — volta para basico
    if (event.type === 'customer.subscription.deleted') {
        const subscription = event.data.object;
        const usuariosRef = db.collection('usuarios');
        const query = await usuariosRef
            .where('stripeSubscriptionId', '==', subscription.id)
            .get();

        query.forEach(async (doc) => {
            await doc.ref.update({ plano: 'basico' });
        });
    }

    return res.status(200).json({ recebido: true });
};