import { forwardRef, memo } from 'react';

export const ComposedField = memo(
  forwardRef<HTMLInputElement, { hint: string }>(function ComposedField(props, ref) {
    return <input placeholder={props.hint} ref={ref} />;
  }),
);
