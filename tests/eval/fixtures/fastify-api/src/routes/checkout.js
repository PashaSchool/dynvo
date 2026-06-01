module.exports = async function checkoutRoutes(fastify) {
  fastify.post("/checkout", async (request) => {
    return { ok: true, total: request.body.total };
  });

  fastify.get("/checkout/status", async () => {
    return { status: "pending" };
  });
};
