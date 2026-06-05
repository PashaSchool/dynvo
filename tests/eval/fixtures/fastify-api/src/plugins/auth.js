const fp = require("fastify-plugin");

module.exports = fp(async function authPlugin(fastify) {
  fastify.decorate("authenticate", async (request) => {
    request.user = { id: "anonymous" };
  });
});
