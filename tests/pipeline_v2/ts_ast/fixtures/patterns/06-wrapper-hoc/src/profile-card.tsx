import { withAuth } from './with-auth';

function ProfileCard({ name }: { name: string }) {
  return <section>{name}</section>;
}

export const GuardedProfileCard = withAuth(ProfileCard);
