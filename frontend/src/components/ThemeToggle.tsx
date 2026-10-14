import { Moon, Sun } from "lucide-react";
import { useTheme } from "../store/theme";

type Props = {
  className?: string;
  /** compact = icon button · switch = labeled dual control */
  variant?: "compact" | "switch";
};

export default function ThemeToggle({ className = "", variant = "compact" }: Props) {
  const theme = useTheme((s) => s.theme);
  const setTheme = useTheme((s) => s.setTheme);
  const toggle = useTheme((s) => s.toggle);
  const isLight = theme === "light";

  if (variant === "switch") {
    return (
      <div className={`theme-switch ${className}`.trim()} role="group" aria-label="حالت نمایش">
        <button
          type="button"
          className={`theme-switch-btn${isLight ? " active" : ""}`}
          onClick={() => setTheme("light")}
          aria-pressed={isLight}
        >
          <Sun size={15} strokeWidth={2.2} aria-hidden />
          <span>روشن</span>
        </button>
        <button
          type="button"
          className={`theme-switch-btn${!isLight ? " active" : ""}`}
          onClick={() => setTheme("dark")}
          aria-pressed={!isLight}
        >
          <Moon size={15} strokeWidth={2.2} aria-hidden />
          <span>تیره</span>
        </button>
      </div>
    );
  }

  return (
    <button
      type="button"
      className={`theme-toggle ${className}`.trim()}
      onClick={toggle}
      aria-label={isLight ? "حالت تیره" : "حالت روشن"}
      title={isLight ? "حالت تیره" : "حالت روشن"}
    >
      {isLight ? <Moon size={18} strokeWidth={2} aria-hidden /> : <Sun size={18} strokeWidth={2} aria-hidden />}
    </button>
  );
}
