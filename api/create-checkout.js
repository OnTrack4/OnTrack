const stripe = require('stripe')(process.env.STRIPE_SECRET_KEY);

module.exports = async (req, res) => {

    // permite requisições do seu site
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

    if (req.method === 'OPTIONS') {
        return res.status(200).end();
    }

    if (req.method !== 'POST') {
        return res.status(405).json({ erro: 'Método não permitido' });
    }

    try {
        const { plano, usuarioId } = req.body;

        // define o price id baseado no plano escolhido
        const precos = {
            mensal: 'price_1TT7y8FyoPcBBaDHkJyvexAz',
            anual: 'price_1TT7zuFyoPcBBaDHw6fs49J6'
        };

        const priceId = precos[plano];

        if (!priceId) {
            return res.status(400).json({ erro: 'Plano inválido' });
        }

        // cria a sessão de pagamento na Stripe
        const session = await stripe.checkout.sessions.create({
            payment_method_types: ['card'],
            line_items: [{
                price: priceId,
                quantity: 1
            }],
            mode: 'subscription',
            success_url: `${process.env.SITE_URL}/paginas/pagamento-sucesso.html?session_id={CHECKOUT_SESSION_ID}`,
            cancel_url: `${process.env.SITE_URL}/paginas/pagamento.html`,
            metadata: {
                usuarioId: usuarioId
            }
        });

        return res.status(200).json({ url: session.url });

    } catch (erro) {
        console.error('Erro Stripe:', erro);
        return res.status(500).json({ erro: erro.message });
    }
};