export function Banner({ title }: { title: string }) {
  return (
    <header>
      <h1>{title}</h1>
    </header>
  );
}

export default function LandingPage() {
  return <Banner title="Welcome" />;
}
