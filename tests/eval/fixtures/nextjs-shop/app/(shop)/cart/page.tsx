"use client";

import { useState } from "react";

export default function CartPage() {
  const [items, setItems] = useState<string[]>([]);
  return (
    <div>
      <button onClick={() => setItems((i) => [...i, "item"])}>Add</button>
      <span>{items.length} items</span>
    </div>
  );
}
