import type { ComponentType } from 'react';

export function withAuth<P extends object>(Wrapped: ComponentType<P>) {
  return function AuthGuard(props: P) {
    return <Wrapped {...props} />;
  };
}
