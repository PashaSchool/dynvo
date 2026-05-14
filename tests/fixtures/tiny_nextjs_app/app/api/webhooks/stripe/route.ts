export async function POST(req: Request) {
  return new Response("ok");
}

export async function GET(req: Request) {
  return new Response("health");
}
