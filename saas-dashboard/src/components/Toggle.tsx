interface ToggleProps {
  title: string;
  description?: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}

export default function Toggle({ title, description, checked, onChange }: ToggleProps) {
  return (
    <div className="toggle">
      <div className="toggle-info">
        <div className="toggle-title">{title}</div>
        {description && <div className="toggle-desc">{description}</div>}
      </div>
      <label className="toggle-switch">
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="toggle-slider" />
      </label>
    </div>
  );
}
