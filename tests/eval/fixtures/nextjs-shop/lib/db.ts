export interface Product {
  id: string;
  name: string;
}

export async function getProducts(): Promise<Product[]> {
  return [
    { id: "1", name: "Widget" },
    { id: "2", name: "Gadget" },
  ];
}
