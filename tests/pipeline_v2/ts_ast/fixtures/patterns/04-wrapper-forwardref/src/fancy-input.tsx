import { forwardRef } from 'react';

export const FancyInput = forwardRef<HTMLInputElement, { label: string }>(
  function FancyInput(props, ref) {
    return <input aria-label={props.label} ref={ref} />;
  },
);
