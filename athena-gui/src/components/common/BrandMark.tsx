interface BrandMarkProps {
  size?: number;
  /** Show the "Athena" word next to the mark. */
  withName?: boolean;
  className?: string;
}

/** Athena logo: an "A" whose crossbar is a point of insight. */
export function BrandMark({ size = 24, withName = false, className }: BrandMarkProps) {
  return (
    <span
      className={className}
      style={{ display: "inline-flex", alignItems: "center", gap: 9 }}
    >
      <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden focusable="false">
        <rect width="32" height="32" rx="9" fill="var(--accent)" />
        <path
          d="M9.5 23 16 8.5 22.5 23"
          fill="none"
          stroke="var(--on-accent)"
          strokeWidth="2.9"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <circle cx="16" cy="18" r="2.1" fill="var(--on-accent)" />
      </svg>
      {withName && (
        <span
          style={{
            fontSize: Math.round(size * 0.66),
            fontWeight: 600,
            letterSpacing: "-0.015em",
            color: "var(--text-primary)",
          }}
        >
          Athena
        </span>
      )}
    </span>
  );
}
