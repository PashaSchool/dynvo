const Fastify = require("fastify");

const fastify = Fastify({ logger: true });

fastify.register(require("./routes/products"), { prefix: "/products" });
fastify.register(require("./routes/checkout"), { prefix: "/checkout" });

module.exports = fastify;
