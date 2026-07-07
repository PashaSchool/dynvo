import { memo } from 'react';

export const ListItem = memo(function ListItem({ label }: { label: string }) {
  return <li>{label}</li>;
});

export function plainHelper(): number {
  return 3;
}
