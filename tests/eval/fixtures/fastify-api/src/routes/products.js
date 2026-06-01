module.exports = async function productRoutes(fastify) {
  fastify.get("/products", async () => {
    return [{ id: 1, name: "Widget" }];
  });

  fastify.post("/products", async (request) => {
    return { id: 2, ...request.body };
  });

  fastify.delete("/products/:id", async (request) => {
    return { deleted: request.params.id };
  });
};
