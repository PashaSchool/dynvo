interface ImpactBadgeProps {
  level: string;
}

export default function ImpactBadge({ level }: ImpactBadgeProps) {
  return (
    <span className={`impact-badge impact-${level}`}>
      {level}
    </span>
  );
}
